from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_core.broker import SimulatedBroker
from platform_core.db.models import (
    BacktestMetricTotal,
    BacktestOrderEvent,
    BacktestRun,
    Bar1mEquity,
    Bar1mOption,
)
from platform_core.execution import OrderManager
from platform_core.risk import BasicRiskEngine
from platform_core.runtime import InMemoryEventBus, TradingEngine
from platform_core.schemas import BarEvent, MarketQuote, PlatformEvent
from platform_core.schemas.assets import AssetType, InstrumentRef
from platform_core.strategy import StrategyPlugin

Track = Literal["equity", "option", "dual"]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: int
    status: str
    trade_count: int
    decision_count: int
    rejected_count: int
    gross_notional: Decimal
    fees: Decimal
    pnl: Decimal
    ending_equity: Decimal
    report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for name in ("gross_notional", "fees", "pnl", "ending_equity"):
            payload[name] = str(payload[name])
        return payload


class DBBacktestRunner:
    """使用通用运行时和模拟 OMS 完成与策略无关的历史回放。"""

    def __init__(
        self,
        *,
        session: Session,
        strategy: StrategyPlugin,
        risk: BasicRiskEngine | None = None,
        initial_cash: Decimal = Decimal(1_000_000),
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self.strategy = strategy
        self.risk = risk or BasicRiskEngine(notional_cap=Decimal(100_000))
        self.initial_cash = initial_cash
        self.parameters = parameters or {}

    def run(
        self,
        *,
        start: datetime,
        end: datetime,
        symbols: Sequence[str],
        track: Track = "dual",
    ) -> BacktestResult:
        run = BacktestRun(
            strategy_code=self.strategy.strategy_code,
            strategy_version=self.strategy.strategy_version,
            calibration_version=None,
            started_at=datetime.now(UTC),
            status="RUNNING",
            parameters={
                "track": track,
                "symbols": [symbol.upper() for symbol in symbols],
                "strategy_parameters": self.parameters,
                "initial_cash": str(self.initial_cash),
            },
        )
        self.session.add(run)
        self.session.flush()
        run_id = int(run.run_id)

        broker = SimulatedBroker(initial_cash=self.initial_cash)
        event_bus = InMemoryEventBus()
        event_bus.subscribe(lambda event: self._persist_event(run_id, event))
        engine = TradingEngine(
            strategy=self.strategy,
            broker=broker,
            order_manager=OrderManager(session=self.session, broker=broker),
            risk=self.risk,
            event_bus=event_bus,
            parameters=self.parameters,
        )
        engine.start()
        try:
            for _, event in self._load_events(start=start, end=end, symbols=symbols, track=track):
                if isinstance(event, MarketQuote):
                    engine.process_quote(event)
                else:
                    engine.process_bar(event, quote=_equity_quote(event))
            engine.poll_broker()
            account = broker.account_snapshot()
        finally:
            engine.stop()

        fills = [event for event in event_bus.events if event.event_type == "FILL"]
        decisions = [
            event for event in event_bus.events if event.event_type == "PORTFOLIO_DECISION"
        ]
        rejections = [event for event in event_bus.events if event.event_type == "RISK_REJECTED"]
        gross_notional = sum((_fill_notional(event) for event in fills), Decimal(0))
        fees = sum((Decimal(str(event.payload.get("fees", 0))) for event in fills), Decimal(0))
        ending_equity = account.net_liquidation or Decimal(0)
        pnl = ending_equity - self.initial_cash
        metrics = {
            "decision_count": Decimal(len(decisions)),
            "trade_count": Decimal(len(fills)),
            "rejected_count": Decimal(len(rejections)),
            "gross_notional": gross_notional,
            "fees": fees,
            "pnl": pnl,
            "ending_equity": ending_equity,
        }
        for name, value in metrics.items():
            self.session.add(
                BacktestMetricTotal(
                    run_id=run_id,
                    metric_name=name,
                    metric_value=value,
                    payload={"track": track},
                )
            )
        run.completed_at = datetime.now(UTC)
        run.status = "COMPLETED"
        report = {
            "run_id": run_id,
            "strategy_code": self.strategy.strategy_code,
            "strategy_version": self.strategy.strategy_version,
            "track": track,
            "symbols": [symbol.upper() for symbol in symbols],
            **{name: str(value) for name, value in metrics.items()},
            "status": "COMPLETED",
        }
        return BacktestResult(
            run_id=run_id,
            status="COMPLETED",
            trade_count=len(fills),
            decision_count=len(decisions),
            rejected_count=len(rejections),
            gross_notional=gross_notional,
            fees=fees,
            pnl=pnl,
            ending_equity=ending_equity,
            report=report,
        )

    def _load_events(
        self,
        *,
        start: datetime,
        end: datetime,
        symbols: Sequence[str],
        track: Track,
    ) -> list[tuple[datetime, BarEvent | MarketQuote]]:
        normalized_symbols = [symbol.upper() for symbol in symbols]
        events: list[tuple[datetime, BarEvent | MarketQuote]] = []
        if track in {"equity", "dual"}:
            rows = self.session.scalars(
                select(Bar1mEquity)
                .where(
                    Bar1mEquity.symbol.in_(normalized_symbols),
                    Bar1mEquity.ts_end >= start,
                    Bar1mEquity.ts_end < end,
                )
                .order_by(Bar1mEquity.ts_end.asc(), Bar1mEquity.symbol.asc())
            )
            events.extend((row.ts_end, _bar_event(row)) for row in rows)
        if track in {"option", "dual"}:
            rows = self.session.scalars(
                select(Bar1mOption)
                .where(
                    Bar1mOption.underlying_symbol.in_(normalized_symbols),
                    Bar1mOption.ts_end >= start,
                    Bar1mOption.ts_end <= end,
                )
                .order_by(Bar1mOption.ts_end.asc(), Bar1mOption.conid.asc())
            )
            events.extend((row.ts_end, _option_quote(row)) for row in rows)
        events.sort(key=lambda item: (item[0], 0 if isinstance(item[1], MarketQuote) else 1))
        return events

    def _persist_event(self, run_id: int, event: PlatformEvent) -> None:
        if event.instrument is None:
            return
        self.session.add(
            BacktestOrderEvent(
                run_id=run_id,
                asset_type=event.instrument.asset_type.value,
                symbol=event.instrument.symbol.upper(),
                event_type=event.event_type,
                reason_code=None,
                event_time=event.occurred_at,
                payload=event.model_dump(mode="json"),
            )
        )


def _bar_event(row: Bar1mEquity) -> BarEvent:
    asset_type = AssetType.ETF if row.symbol in {"SPY", "QQQ", "IWM"} else AssetType.EQUITY
    return BarEvent(
        instrument=InstrumentRef(asset_type=asset_type, symbol=row.symbol),
        bar_start=row.ts_end,
        bar_end=row.ts_end,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        vwap=row.vwap,
    )


def _option_quote(row: Bar1mOption) -> MarketQuote:
    instrument = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol=row.underlying_symbol,
        conid=row.conid,
        option_right="CALL" if row.right == "CALL" else "PUT",
        strike=row.strike,
        expiry=row.expiry,
    )
    return MarketQuote(
        instrument=instrument,
        quote_ts=row.ts_end,
        bid=row.bid,
        ask=row.ask,
        mid=row.mid,
        last=row.last,
        volume=row.volume,
        open_interest=row.open_interest,
        source="db",
    )


def _equity_quote(bar: BarEvent) -> MarketQuote:
    half_spread = Decimal("0.01")
    return MarketQuote(
        instrument=bar.instrument,
        quote_ts=bar.bar_end,
        bid=max(Decimal("0.01"), bar.close - half_spread),
        ask=bar.close + half_spread,
        last=bar.close,
        source="db-derived",
    )


def _fill_notional(event: PlatformEvent) -> Decimal:
    instrument = event.instrument
    if instrument is None:
        return Decimal(0)
    return (
        abs(Decimal(str(event.payload.get("quantity", 0))))
        * Decimal(str(event.payload.get("fill_price", 0)))
        * instrument.multiplier
    )
