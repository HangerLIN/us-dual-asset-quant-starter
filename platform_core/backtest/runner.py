from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from examples.dual_asset_momentum import DualAssetMomentumStrategy
from platform_core.db.models import (
    BacktestMetricTotal,
    BacktestOrderEvent,
    BacktestRun,
    Bar1mEquity,
    Bar1mOption,
    OptionChainMeta,
)
from platform_core.execution import QuoteAwareExecutionSelector
from platform_core.risk import BasicRiskEngine
from platform_core.schemas import BarEvent, MarketQuote, RiskCheckRequest
from platform_core.schemas.assets import AssetType, InstrumentRef

Track = Literal["equity", "option", "dual"]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: int
    status: str
    trade_count: int
    signal_count: int
    rejected_count: int
    gross_notional: Decimal
    fees: Decimal
    report: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["gross_notional"] = str(self.gross_notional)
        payload["fees"] = str(self.fees)
        return payload


class DBBacktestRunner:
    def __init__(
        self,
        *,
        session: Session,
        strategy: DualAssetMomentumStrategy | None = None,
        risk: BasicRiskEngine | None = None,
        execution_selector: QuoteAwareExecutionSelector | None = None,
    ) -> None:
        self.session = session
        self.strategy = strategy or DualAssetMomentumStrategy()
        self.risk = risk or BasicRiskEngine(notional_cap=Decimal("100000"))
        self.execution_selector = execution_selector or QuoteAwareExecutionSelector()

    def run(
        self,
        *,
        start: datetime,
        end: datetime,
        symbols: Sequence[str],
        track: Track = "dual",
        strategy_version: str = "starter",
        calibration_version: str = "local-dev",
    ) -> BacktestResult:
        started_at = datetime.now(UTC)
        run = BacktestRun(
            strategy_code=self.strategy.strategy_code,
            strategy_version=strategy_version,
            calibration_version=calibration_version,
            started_at=started_at,
            status="RUNNING",
            parameters={"track": track, "symbols": [symbol.upper() for symbol in symbols]},
        )
        self.session.add(run)
        self.session.flush()
        run_id = int(run.run_id)

        signal_count = 0
        rejected_count = 0
        trade_count = 0
        gross_notional = Decimal("0")
        fees = Decimal("0")
        traded_keys: set[tuple[str, str]] = set()

        bars = self._load_equity_bars(start=start, end=end, symbols=symbols)
        for bar_row in bars:
            bar = self._bar_event(bar_row)
            option_quote = self._latest_option_quote(symbol=bar.instrument.symbol, ts=bar.bar_end)
            selected_option = option_quote.instrument if option_quote else self._chain_option(bar.instrument.symbol, start)
            option_spread_pct = option_quote.spread_pct if option_quote else None
            features = {
                "vwap": bar.vwap,
                "rvol": Decimal("2.0"),
            }
            signals = self.strategy.process_bar(
                bar,
                features=features,
                context={"selected_option": selected_option, "option_spread_pct": option_spread_pct or Decimal("0")},
            )
            signals = [signal for signal in signals if _track_allows(track, signal.instrument.asset_type)]
            for signal in signals:
                key = (signal.instrument.asset_type.value, signal.instrument.symbol)
                if key in traded_keys:
                    continue
                signal_count += 1
                self._event(
                    run_id=run_id,
                    instrument=signal.instrument,
                    event_type="SIGNAL",
                    reason_code=signal.signal_code,
                    event_time=signal.generated_at,
                    payload={"confidence": str(signal.confidence), "reason": _json_safe(signal.reason)},
                )
                price = option_quote.mid if signal.instrument.asset_type == AssetType.OPTION and option_quote else bar.close
                decisions = self.strategy.construct_portfolio([signal], prices={signal.instrument.symbol: price or bar.close})
                for decision in decisions:
                    quote = option_quote if decision.instrument.asset_type == AssetType.OPTION else _equity_quote(bar)
                    risk_result = self.risk.evaluate(
                        RiskCheckRequest(
                            strategy_code=decision.strategy_code,
                            instrument=decision.instrument,
                            side=decision.side,
                            quantity=decision.quantity,
                            notional=decision.target_notional,
                            timestamp=bar.bar_end,
                            signal_code=decision.signal_code,
                            quote={"spread_pct": quote.spread_pct},
                        )
                    )
                    if not risk_result.approved:
                        rejected_count += 1
                        self._event(
                            run_id=run_id,
                            instrument=decision.instrument,
                            event_type="RISK_REJECTED",
                            reason_code=risk_result.code,
                            event_time=bar.bar_end,
                            payload=_json_safe(risk_result.model_dump()),
                        )
                        continue
                    request = self.execution_selector.build_request(
                        decision,
                        quote=quote,
                        trace_id=f"bt-{run_id}-{decision.instrument.asset_type.value}-{decision.instrument.symbol}",
                    )
                    self._event(
                        run_id=run_id,
                        instrument=request.instrument,
                        event_type="ORDER_SUBMITTED",
                        reason_code=request.signal_code,
                        event_time=bar.bar_end,
                        payload=_json_safe(request.model_dump()),
                    )
                    fill_price = request.limit_price
                    fill_fee = _fee(request.instrument.asset_type, request.quantity)
                    notional = _notional(request.instrument.asset_type, request.quantity, fill_price)
                    trade_count += 1
                    gross_notional += notional
                    fees += fill_fee
                    traded_keys.add(key)
                    self._event(
                        run_id=run_id,
                        instrument=request.instrument,
                        event_type="FILL",
                        reason_code="SIMULATED_QUOTE_AWARE_FILL",
                        event_time=bar.bar_end,
                        payload={
                            "side": request.side,
                            "quantity": str(request.quantity),
                            "fill_price": str(fill_price),
                            "fees": str(fill_fee),
                            "notional": str(notional),
                            "quote": _json_safe(quote.model_dump()),
                        },
                    )

        completed_at = datetime.now(UTC)
        run.completed_at = completed_at
        run.status = "COMPLETED"
        metrics = {
            "signal_count": Decimal(signal_count),
            "trade_count": Decimal(trade_count),
            "rejected_count": Decimal(rejected_count),
            "gross_notional": gross_notional,
            "fees": fees,
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
        report = {
            "run_id": run_id,
            "track": track,
            "symbols": [symbol.upper() for symbol in symbols],
            "signal_count": signal_count,
            "trade_count": trade_count,
            "rejected_count": rejected_count,
            "gross_notional": str(gross_notional),
            "fees": str(fees),
            "status": "COMPLETED",
        }
        return BacktestResult(
            run_id=run_id,
            status="COMPLETED",
            trade_count=trade_count,
            signal_count=signal_count,
            rejected_count=rejected_count,
            gross_notional=gross_notional,
            fees=fees,
            report=report,
        )

    def _load_equity_bars(self, *, start: datetime, end: datetime, symbols: Sequence[str]) -> list[Bar1mEquity]:
        return list(
            self.session.scalars(
                select(Bar1mEquity)
                .where(
                    Bar1mEquity.symbol.in_([symbol.upper() for symbol in symbols]),
                    Bar1mEquity.ts_end >= start,
                    Bar1mEquity.ts_end <= end,
                )
                .order_by(Bar1mEquity.ts_end.asc(), Bar1mEquity.symbol.asc())
            )
        )

    def _bar_event(self, row: Bar1mEquity) -> BarEvent:
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

    def _chain_option(self, symbol: str, start: datetime) -> InstrumentRef | None:
        row = self.session.scalar(
            select(OptionChainMeta)
            .where(OptionChainMeta.underlying_symbol == symbol.upper(), OptionChainMeta.trade_date == start.date())
            .order_by(OptionChainMeta.expiry.asc(), OptionChainMeta.strike.asc())
            .limit(1)
        )
        if row is None:
            return None
        return _option_instrument_from_chain(row)

    def _latest_option_quote(self, *, symbol: str, ts: datetime) -> MarketQuote | None:
        row = self.session.scalar(
            select(Bar1mOption)
            .where(Bar1mOption.underlying_symbol == symbol.upper(), Bar1mOption.ts_end <= ts)
            .order_by(Bar1mOption.ts_end.desc())
            .limit(1)
        )
        if row is None:
            return None
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

    def _event(
        self,
        *,
        run_id: int,
        instrument: InstrumentRef,
        event_type: str,
        reason_code: str | None,
        event_time: datetime,
        payload: dict[str, Any],
    ) -> None:
        self.session.add(
            BacktestOrderEvent(
                run_id=run_id,
                asset_type=instrument.asset_type.value,
                symbol=instrument.symbol.upper(),
                event_type=event_type,
                reason_code=reason_code,
                event_time=event_time,
                payload=_json_safe(payload),
            )
        )


def _track_allows(track: Track, asset_type: AssetType) -> bool:
    if track == "dual":
        return True
    if track == "equity":
        return asset_type in {AssetType.EQUITY, AssetType.ETF}
    return asset_type == AssetType.OPTION


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


def _option_instrument_from_chain(row: OptionChainMeta) -> InstrumentRef:
    return InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol=row.underlying_symbol,
        conid=row.conid,
        option_right="CALL" if row.right == "CALL" else "PUT",
        strike=row.strike,
        expiry=row.expiry,
        metadata={"dte": row.dte},
    )


def _fee(asset_type: AssetType, quantity: Decimal) -> Decimal:
    if asset_type == AssetType.OPTION:
        return (abs(quantity) * Decimal("0.65")).quantize(Decimal("0.000001"))
    return Decimal("0")


def _notional(asset_type: AssetType, quantity: Decimal, price: Decimal) -> Decimal:
    multiplier = Decimal("100") if asset_type == AssetType.OPTION else Decimal("1")
    return abs(quantity * price * multiplier)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value
