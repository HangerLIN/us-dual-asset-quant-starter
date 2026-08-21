from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from platform_core.schemas import (
    AccountSnapshot,
    BrokerOrderUpdate,
    ExecutionFill,
    ExecutionRequest,
    MarketQuote,
    OrderStatus,
    PositionSnapshot,
    RuntimeMode,
)

from .contracts import BrokerEvent


class SimulatedBroker:
    """供回测和平台契约测试使用的确定性模拟经纪商。"""

    mode = RuntimeMode.BACKTEST

    def __init__(self, *, initial_cash: Decimal = Decimal(1_000_000)) -> None:
        self.account_id = "SIMULATED"
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self._connected = False
        self._orders: dict[str, BrokerOrderUpdate] = {}
        self._positions: dict[str, PositionSnapshot] = {}
        self._events: list[BrokerEvent] = []

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def submit_order(self, request: ExecutionRequest) -> BrokerOrderUpdate:
        self._require_connected()
        if not request.client_order_id:
            raise ValueError("client_order_id is required before broker submission")
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            return existing

        now = request.created_at or datetime.now(UTC)
        update = BrokerOrderUpdate(
            client_order_id=request.client_order_id,
            broker_order_id=f"SIM-{len(self._orders) + 1}",
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
            remaining_quantity=Decimal(0),
            average_fill_price=request.limit_price,
            updated_at=now,
        )
        fill = ExecutionFill(
            strategy_code=request.strategy_code,
            instrument=request.instrument,
            side=request.side,
            quantity=request.quantity,
            fill_price=request.limit_price,
            filled_at=now,
            fees=self._fee(request),
            execution_id=f"SIM-EXEC-{len(self._orders) + 1}",
            client_order_id=request.client_order_id,
            broker_order_id=update.broker_order_id,
        )
        self._orders[request.client_order_id] = update
        self._apply_fill(fill)
        self._events.extend([update, fill])
        return update

    def cancel_order(self, client_order_id: str) -> BrokerOrderUpdate:
        self._require_connected()
        existing = self._orders.get(client_order_id)
        if existing is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        if existing.status == OrderStatus.FILLED:
            return existing
        update = existing.model_copy(
            update={"status": OrderStatus.CANCELLED, "updated_at": datetime.now(UTC)}
        )
        self._orders[client_order_id] = update
        self._events.append(update)
        return update

    def drain_events(self) -> list[BrokerEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def open_orders(self) -> list[BrokerOrderUpdate]:
        terminal = {OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.REJECTED}
        return [order for order in self._orders.values() if order.status not in terminal]

    def positions(self) -> list[PositionSnapshot]:
        return list(self._positions.values())

    def account_snapshot(self) -> AccountSnapshot:
        market_value = sum(position.notional for position in self._positions.values())
        return AccountSnapshot(
            account_id=self.account_id,
            mode=self.mode,
            captured_at=datetime.now(UTC),
            cash=self.cash,
            net_liquidation=self.cash + market_value,
            buying_power=self.cash,
        )

    def update_quote(self, quote: MarketQuote) -> None:
        position = self._positions.get(quote.instrument.key)
        mark = quote.mid or quote.last or quote.bid or quote.ask
        if position is None or mark is None:
            return
        self._positions[quote.instrument.key] = position.model_copy(
            update={
                "mark_price": mark,
                "notional": position.quantity * mark * position.instrument.multiplier,
                "updated_at": quote.quote_ts,
            }
        )

    def _apply_fill(self, fill: ExecutionFill) -> None:
        signed_quantity = fill.quantity if fill.side == "BUY" else -fill.quantity
        cash_delta = signed_quantity * fill.fill_price * fill.instrument.multiplier
        self.cash -= cash_delta + fill.fees
        key = fill.instrument.key
        existing = self._positions.get(key)
        old_quantity = existing.quantity if existing else Decimal(0)
        new_quantity = old_quantity + signed_quantity
        if new_quantity == 0:
            self._positions.pop(key, None)
            return
        if existing is None or old_quantity == 0 or old_quantity * signed_quantity > 0:
            old_cost = (existing.avg_open_price * abs(old_quantity)) if existing else Decimal(0)
            average = (old_cost + fill.fill_price * abs(signed_quantity)) / abs(new_quantity)
        else:
            average = existing.avg_open_price
        notional = new_quantity * fill.fill_price * fill.instrument.multiplier
        self._positions[key] = PositionSnapshot(
            strategy_code=fill.strategy_code,
            instrument=fill.instrument,
            quantity=new_quantity,
            avg_open_price=average,
            mark_price=fill.fill_price,
            notional=notional,
            opened_at=existing.opened_at if existing else fill.filled_at,
            updated_at=fill.filled_at,
        )

    @staticmethod
    def _fee(request: ExecutionRequest) -> Decimal:
        if request.instrument.asset_type.value == "OPTION":
            return abs(request.quantity) * Decimal("0.65")
        return Decimal(0)

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("broker is not connected")
