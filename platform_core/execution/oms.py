from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_core.broker import BrokerAdapter, BrokerEvent
from platform_core.db.models import Fill, Order, Position
from platform_core.schemas import (
    BrokerOrderUpdate,
    ExecutionFill,
    ExecutionRequest,
    OrderStatus,
)


class OrderManager:
    """回测运行时与模拟经纪商之间的持久化幂等边界。"""

    def __init__(self, *, session: Session, broker: BrokerAdapter) -> None:
        self.session = session
        self.broker = broker

    def submit(self, request: ExecutionRequest) -> BrokerOrderUpdate:
        request = self._normalize_request(request)
        assert request.client_order_id is not None
        existing = self.session.scalar(
            select(Order).where(Order.client_order_id == request.client_order_id)
        )
        if existing is not None:
            return _update_from_order(existing)
        row = _order_from_request(
            request,
            mode=self.broker.mode.value,
            account_id=self.broker.account_id,
        )
        self.session.add(row)
        self.session.flush()
        try:
            update = self.broker.submit_order(request)
        except Exception as exc:
            row.status = OrderStatus.REJECTED.value
            row.error_message = str(exc)
            row.updated_at = datetime.now(UTC)
            self.session.flush()
            raise
        self._apply_order_update(update)
        return update

    def cancel(self, client_order_id: str) -> BrokerOrderUpdate:
        update = self.broker.cancel_order(client_order_id)
        self._apply_order_update(update)
        return update

    def poll(self) -> list[BrokerEvent]:
        events = self.broker.drain_events()
        for event in events:
            if isinstance(event, BrokerOrderUpdate):
                self._apply_order_update(event)
            else:
                self._apply_fill(event)
        return events

    def cancel_expired(self, *, now: datetime | None = None) -> list[BrokerOrderUpdate]:
        now = now or datetime.now(UTC)
        active = {
            OrderStatus.PENDING_SUBMIT.value,
            OrderStatus.SUBMITTED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        }
        rows = self.session.scalars(
            select(Order).where(
                Order.status.in_(active),
                Order.expires_at.is_not(None),
                Order.expires_at <= now,
            )
        )
        updates = []
        for row in rows:
            updates.append(self.cancel(row.client_order_id))
        return updates

    def _apply_order_update(self, update: BrokerOrderUpdate) -> None:
        row = self.session.scalar(
            select(Order).where(Order.client_order_id == update.client_order_id)
        )
        if row is None:
            return
        row.broker_order_id = update.broker_order_id or row.broker_order_id
        row.status = update.status.value
        row.error_message = update.message
        row.updated_at = update.updated_at
        self.session.flush()

    def _apply_fill(self, fill: ExecutionFill) -> None:
        if not fill.client_order_id:
            return
        order = self.session.scalar(
            select(Order).where(Order.client_order_id == fill.client_order_id)
        )
        if order is None:
            return
        execution_id = fill.execution_id or f"{fill.client_order_id}:{fill.filled_at.isoformat()}"
        duplicate = self.session.scalar(
            select(Fill).where(Fill.execution_id == execution_id)
        )
        if duplicate is not None:
            return
        self.session.add(
            Fill(
                order_id=order.order_id,
                execution_id=execution_id,
                instrument_key=fill.instrument.key,
                asset_type=fill.instrument.asset_type.value,
                symbol=fill.instrument.symbol.upper(),
                conid=fill.instrument.conid,
                side=fill.side,
                quantity=fill.quantity,
                fill_price=fill.fill_price,
                filled_at=fill.filled_at,
                fees=fill.fees,
            )
        )
        self._update_position(fill)
        self.session.flush()

    def _update_position(self, fill: ExecutionFill) -> None:
        key = fill.instrument.key
        position = self.session.get(Position, (fill.strategy_code, key))
        signed_quantity = fill.quantity if fill.side == "BUY" else -fill.quantity
        if position is None:
            position = Position(
                strategy_code=fill.strategy_code,
                instrument_key=key,
                asset_type=fill.instrument.asset_type.value,
                symbol=fill.instrument.symbol.upper(),
                conid=fill.instrument.conid,
                expiry=_expiry_date(fill.instrument.expiry),
                option_right=fill.instrument.option_right,
                strike=fill.instrument.strike,
                multiplier=fill.instrument.multiplier,
                quantity=Decimal(0),
                avg_price=fill.fill_price,
                mark_price=fill.fill_price,
                updated_at=fill.filled_at,
            )
            self.session.add(position)
        previous = position.quantity
        resulting = previous + signed_quantity
        if resulting == 0:
            self.session.delete(position)
            return
        if previous == 0 or previous * signed_quantity > 0:
            total_cost = position.avg_price * abs(previous) + fill.fill_price * abs(signed_quantity)
            position.avg_price = total_cost / abs(resulting)
        position.quantity = resulting
        position.mark_price = fill.fill_price
        position.updated_at = fill.filled_at

    @staticmethod
    def _normalize_request(request: ExecutionRequest) -> ExecutionRequest:
        now = request.created_at or datetime.now(UTC)
        trace_id = request.trace_id or uuid4().hex
        client_order_id = request.client_order_id or f"{request.strategy_code}-{uuid4().hex}"
        return request.model_copy(
            update={
                "created_at": now,
                "trace_id": trace_id,
                "client_order_id": client_order_id,
            }
        )


def _order_from_request(
    request: ExecutionRequest,
    *,
    mode: str,
    account_id: str,
) -> Order:
    return Order(
        client_order_id=request.client_order_id,
        trace_id=request.trace_id,
        runtime_mode=mode,
        account_id=account_id,
        strategy_code=request.strategy_code,
        instrument_key=request.instrument.key,
        asset_type=request.instrument.asset_type.value,
        symbol=request.instrument.symbol.upper(),
        conid=request.instrument.conid,
        expiry=_expiry_date(request.instrument.expiry),
        option_right=request.instrument.option_right,
        strike=request.instrument.strike,
        multiplier=request.instrument.multiplier,
        side=request.side,
        quantity=request.quantity,
        limit_price=request.limit_price,
        tif=request.tif,
        status=OrderStatus.PENDING_SUBMIT.value,
        created_at=request.created_at,
        expires_at=request.expires_at,
        updated_at=request.created_at,
    )


def _update_from_order(order: Order) -> BrokerOrderUpdate:
    return BrokerOrderUpdate(
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        status=OrderStatus(order.status),
        updated_at=order.updated_at or order.created_at,
        remaining_quantity=Decimal(0) if order.status == OrderStatus.FILLED.value else order.quantity,
        message=order.error_message,
    )


def _expiry_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value
