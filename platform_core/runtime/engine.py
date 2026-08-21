from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from platform_core.broker import BrokerAdapter, BrokerEvent
from platform_core.execution import OrderManager, QuoteAwareExecutionSelector
from platform_core.risk import BasicRiskEngine
from platform_core.schemas import (
    BarEvent,
    BrokerOrderUpdate,
    ExecutionFill,
    MarketQuote,
    PlatformEvent,
    PortfolioDecision,
    PositionSnapshot,
    RiskCheckRequest,
    RiskCheckResult,
)
from platform_core.strategy import StrategyContext, StrategyPlugin

from .events import EventBus, InMemoryEventBus


@dataclass(slots=True)
class EngineCycleResult:
    decisions: list[PortfolioDecision] = field(default_factory=list)
    risk_results: list[RiskCheckResult] = field(default_factory=list)
    order_updates: list[BrokerOrderUpdate] = field(default_factory=list)
    broker_events: list[BrokerEvent] = field(default_factory=list)


class TradingEngine:
    """One data flow for backtest, paper, and live strategy execution."""

    def __init__(
        self,
        *,
        strategy: StrategyPlugin,
        broker: BrokerAdapter,
        order_manager: OrderManager,
        risk: BasicRiskEngine,
        execution_selector: QuoteAwareExecutionSelector | None = None,
        event_bus: EventBus | None = None,
        parameters: dict[str, Any] | None = None,
        order_ttl_seconds: int = 90,
    ) -> None:
        if order_manager.broker is not broker:
            raise ValueError("order_manager and engine must share the same broker instance")
        self.strategy = strategy
        self.broker = broker
        self.orders = order_manager
        self.risk = risk
        self.execution_selector = execution_selector or QuoteAwareExecutionSelector()
        self.event_bus = event_bus or InMemoryEventBus()
        self.parameters = parameters or {}
        if order_ttl_seconds <= 0:
            raise ValueError("order_ttl_seconds must be positive")
        self.order_ttl_seconds = order_ttl_seconds
        self._started = False
        self._positions: dict[str, PositionSnapshot] = {}
        self._quotes: dict[str, MarketQuote] = {}
        self._account = None

    def start(self) -> None:
        if self._started:
            return
        self.broker.connect()
        self.reconcile()
        self.strategy.on_start(self._context())
        self._started = True
        self._publish("RUNTIME_STARTED", uuid4().hex, payload={"mode": self.broker.mode.value})

    def stop(self) -> None:
        if not self._started:
            return
        self.strategy.on_stop(self._context())
        self.broker.disconnect()
        self._started = False
        self._publish("RUNTIME_STOPPED", uuid4().hex, payload={"mode": self.broker.mode.value})

    def process_bar(
        self,
        bar: BarEvent,
        *,
        quote: MarketQuote | None = None,
    ) -> EngineCycleResult:
        if not self._started:
            raise RuntimeError("trading engine must be started before processing data")
        trace_id = uuid4().hex
        self._publish(
            "BAR_RECEIVED",
            trace_id,
            occurred_at=bar.bar_end,
            instrument=bar.instrument,
            payload=bar.model_dump(mode="json"),
        )
        if quote is not None:
            self._quotes[quote.instrument.key] = quote
            self._mark_broker(quote)
        decisions = list(self.strategy.on_bar(bar, self._context()))
        return self._execute_decisions(decisions, trace_id=trace_id, timestamp=bar.bar_end)

    def process_quote(self, quote: MarketQuote) -> EngineCycleResult:
        if not self._started:
            raise RuntimeError("trading engine must be started before processing data")
        trace_id = uuid4().hex
        self._quotes[quote.instrument.key] = quote
        self._mark_broker(quote)
        self._publish(
            "QUOTE_RECEIVED",
            trace_id,
            occurred_at=quote.quote_ts,
            instrument=quote.instrument,
            payload=quote.model_dump(mode="json"),
        )
        decisions = list(self.strategy.on_quote(quote, self._context()))
        return self._execute_decisions(
            decisions,
            trace_id=trace_id,
            timestamp=quote.quote_ts,
        )

    def _execute_decisions(
        self,
        decisions: list[PortfolioDecision],
        *,
        trace_id: str,
        timestamp: datetime,
    ) -> EngineCycleResult:
        result = EngineCycleResult(decisions=decisions)
        for index, decision in enumerate(decisions):
            self._validate_decision(decision)
            decision_trace = f"{trace_id}-{index}"
            self._publish(
                "PORTFOLIO_DECISION",
                decision_trace,
                occurred_at=timestamp,
                strategy_code=decision.strategy_code,
                instrument=decision.instrument,
                payload=decision.model_dump(mode="json"),
            )
            effective_quote = self._quotes.get(decision.instrument.key)
            reference_price = _executable_price(decision, effective_quote)
            if reference_price is None:
                risk_result = RiskCheckResult(
                    approved=False,
                    code="BLOCK:NO_EXECUTABLE_QUOTE",
                    detail="no executable quote is available for the decision instrument",
                )
                result.risk_results.append(risk_result)
                self._publish(
                    "RISK_REJECTED",
                    decision_trace,
                    occurred_at=timestamp,
                    strategy_code=decision.strategy_code,
                    instrument=decision.instrument,
                    payload=risk_result.model_dump(mode="json"),
                )
                continue
            calculated_notional = (
                abs(decision.quantity) * reference_price * decision.instrument.multiplier
            )
            risk_request = RiskCheckRequest(
                strategy_code=decision.strategy_code,
                instrument=decision.instrument,
                side=decision.side,
                quantity=decision.quantity,
                notional=calculated_notional,
                timestamp=timestamp,
                signal_code=decision.signal_code,
                quote={
                    "spread_pct": effective_quote.spread_pct,
                    "quote_ts": effective_quote.quote_ts.isoformat(),
                    "reference_price": str(reference_price),
                },
            )
            risk_result = self.risk.evaluate(risk_request, context=self._risk_context())
            result.risk_results.append(risk_result)
            if not risk_result.approved:
                self._publish(
                    "RISK_REJECTED",
                    decision_trace,
                    occurred_at=timestamp,
                    strategy_code=decision.strategy_code,
                    instrument=decision.instrument,
                    payload=risk_result.model_dump(mode="json"),
                )
                continue
            request = self.execution_selector.build_request(
                decision,
                quote=effective_quote,
                trace_id=decision_trace,
            ).model_copy(
                update={
                    "client_order_id": f"{decision.strategy_code}-{uuid4().hex}",
                    "created_at": datetime.now(UTC),
                    "expires_at": datetime.now(UTC)
                    + timedelta(seconds=self.order_ttl_seconds),
                }
            )
            update = self.orders.submit(request)
            result.order_updates.append(update)
            self._publish(
                "ORDER_SUBMITTED",
                decision_trace,
                occurred_at=timestamp,
                strategy_code=decision.strategy_code,
                instrument=decision.instrument,
                payload=update.model_dump(mode="json"),
            )
        result.broker_events = self.poll_broker()
        return result

    def poll_broker(self) -> list[BrokerEvent]:
        self.orders.cancel_expired()
        events = self.orders.poll()
        fills: list[ExecutionFill] = []
        for event in events:
            if isinstance(event, BrokerOrderUpdate):
                request_strategy = _strategy_from_client_order_id(event.client_order_id)
                self.strategy.on_order_update(event, self._context())
                self._publish(
                    "ORDER_UPDATE",
                    event.client_order_id,
                    occurred_at=event.updated_at,
                    strategy_code=request_strategy,
                    payload=event.model_dump(mode="json"),
                )
            elif isinstance(event, ExecutionFill):
                fills.append(event)
                self._publish(
                    "FILL",
                    event.client_order_id or event.execution_id or uuid4().hex,
                    occurred_at=event.filled_at,
                    strategy_code=event.strategy_code,
                    instrument=event.instrument,
                    payload=event.model_dump(mode="json"),
                )
        if fills:
            self.reconcile()
            for fill in fills:
                self.strategy.on_fill(fill, self._context())
        return events

    def reconcile(self) -> None:
        """Refresh broker-authoritative account and position state."""
        self._positions = {
            position.instrument.key: position for position in self.broker.positions()
        }
        self._account = self.broker.account_snapshot()

    def _context(self) -> StrategyContext:
        return StrategyContext(
            mode=self.broker.mode,
            parameters=self.parameters,
            positions=self._positions,
            quotes=self._quotes,
            account=self._account,
        )

    def _risk_context(self) -> dict[str, Decimal | bool]:
        account = self._account or self.broker.account_snapshot()
        daily_pnl = (account.realized_pnl or Decimal(0)) + (
            account.unrealized_pnl or Decimal(0)
        )
        gross_exposure = sum(abs(position.notional) for position in self._positions.values())
        return {
            "daily_pnl": daily_pnl,
            "gross_exposure": gross_exposure,
            "kill_switch": bool(self.parameters.get("kill_switch", False)),
        }

    def _validate_decision(self, decision: PortfolioDecision) -> None:
        if decision.strategy_code != self.strategy.strategy_code:
            raise ValueError(
                f"decision strategy_code {decision.strategy_code!r} does not match loaded "
                f"strategy {self.strategy.strategy_code!r}"
            )

    def _mark_broker(self, quote: MarketQuote) -> None:
        updater = getattr(self.broker, "update_quote", None)
        if callable(updater):
            updater(quote)
            self.reconcile()

    def _publish(
        self,
        event_type: str,
        trace_id: str,
        *,
        occurred_at: datetime | None = None,
        strategy_code: str | None = None,
        instrument=None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.event_bus.publish(
            PlatformEvent(
                event_type=event_type,
                occurred_at=occurred_at or datetime.now(UTC),
                trace_id=trace_id,
                strategy_code=strategy_code,
                instrument=instrument,
                payload=payload or {},
            )
        )


def _strategy_from_client_order_id(client_order_id: str) -> str:
    return client_order_id.rsplit("-", 1)[0]


def _executable_price(
    decision: PortfolioDecision,
    quote: MarketQuote | None,
) -> Decimal | None:
    if quote is None:
        return None
    if decision.side == "BUY":
        price = quote.ask or quote.mid or quote.last or quote.bid
    else:
        price = quote.bid or quote.mid or quote.last or quote.ask
    return price if price is not None and price > 0 else None
