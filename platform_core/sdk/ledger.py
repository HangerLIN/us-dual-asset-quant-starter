from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from platform_core.db.models import (
    BrokerAccountSnapshotRecord,
    BrokerExecutionRecord,
    BrokerOrderEventRecord,
    BrokerOrderRecord,
    BrokerPositionRecord,
    ReconciliationRunRecord,
    StatementReconciliationRecord,
    RiskDecisionRecord,
    TradingControlRecord,
    ExecutionLeaseRecord,
)
from platform_core.schemas import (
    BrokerExecution,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
)

from .models import (
    AccountRiskSnapshot,
    BrokerEvent,
    LiveOrderIntent,
    OrderLifecycleState,
    ReconciliationReport,
    RiskAuthorization,
    StrategyOrderEvent,
    TERMINAL_ORDER_STATES,
)


class IdempotencyConflictError(RuntimeError):
    pass


class InvalidOrderTransitionError(RuntimeError):
    pass


class _SubmissionClaimConflict(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[OrderLifecycleState, set[OrderLifecycleState]] = {
    OrderLifecycleState.INTENT_PERSISTED: {
        OrderLifecycleState.RISK_REJECTED,
        OrderLifecycleState.AUTHORIZED,
        OrderLifecycleState.UNKNOWN,
        OrderLifecycleState.EXPIRED,
    },
    OrderLifecycleState.AUTHORIZED: {
        OrderLifecycleState.SUBMITTING,
        OrderLifecycleState.RISK_REJECTED,
        OrderLifecycleState.UNKNOWN,
        OrderLifecycleState.EXPIRED,
    },
    OrderLifecycleState.SUBMITTING: {
        OrderLifecycleState.ACKNOWLEDGED,
        OrderLifecycleState.PARTIAL_FILL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCEL_PENDING,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.UNKNOWN,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.VALIDATED,
    },
    OrderLifecycleState.ACKNOWLEDGED: {
        OrderLifecycleState.PARTIAL_FILL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCEL_PENDING,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.UNKNOWN,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.REPLACE_PENDING,
    },
    OrderLifecycleState.PARTIAL_FILL: {
        OrderLifecycleState.PARTIAL_FILL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCEL_PENDING,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.UNKNOWN,
        OrderLifecycleState.EXPIRED,
        OrderLifecycleState.REPLACE_PENDING,
    },
    OrderLifecycleState.CANCEL_PENDING: {
        OrderLifecycleState.PARTIAL_FILL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.UNKNOWN,
        OrderLifecycleState.EXPIRED,
    },
    OrderLifecycleState.REPLACE_PENDING: {
        OrderLifecycleState.ACKNOWLEDGED,
        OrderLifecycleState.PARTIAL_FILL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCEL_PENDING,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.UNKNOWN,
    },
    OrderLifecycleState.UNKNOWN: {
        OrderLifecycleState.ACKNOWLEDGED,
        OrderLifecycleState.PARTIAL_FILL,
        OrderLifecycleState.FILLED,
        OrderLifecycleState.CANCEL_PENDING,
        OrderLifecycleState.CANCELLED,
        OrderLifecycleState.REJECTED,
        OrderLifecycleState.EXPIRED,
    },
}


def canonical_order_hash(intent: LiveOrderIntent) -> str:
    """仅哈希调用方可控的订单语义，保证重试幂等。"""

    payload = {
        "strategy_code": intent.strategy_code,
        "request": intent.request.model_dump(mode="json"),
        "expires_at": intent.expires_at.isoformat() if intent.expires_at else None,
        "metadata": intent.metadata,
    }
    return _hash_payload(payload)


class SQLAlchemyOrderLedger:
    """供执行、恢复和盈亏 SDK 使用的事务型订单日志。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def healthcheck(self) -> bool:
        try:
            with self._session_factory() as session:
                return session.scalar(select(literal(1))) == 1
        except Exception:  # noqa: BLE001 - readiness 必须报告数据库故障。
            return False

    def latest_account_snapshot(
        self, account: str
    ) -> BrokerAccountSnapshotRecord | None:
        with self._session_factory() as session:
            return session.scalar(
                select(BrokerAccountSnapshotRecord)
                .where(BrokerAccountSnapshotRecord.account == account)
                .order_by(BrokerAccountSnapshotRecord.captured_at.desc())
                .limit(1)
            )

    def execution_lease_is_owned(
        self,
        *,
        account: str,
        holder_id: str,
        now: datetime | None = None,
    ) -> bool:
        current = _as_utc(now or _utcnow())
        with self._session_factory() as session:
            return (
                session.scalar(
                    select(ExecutionLeaseRecord).where(
                        ExecutionLeaseRecord.account == account,
                        ExecutionLeaseRecord.holder_id == holder_id,
                        ExecutionLeaseRecord.expires_at > current,
                    )
                )
                is not None
            )

    def create_or_get_intent(
        self,
        intent: LiveOrderIntent,
        *,
        expected_broker_client_id: int | None = None,
    ) -> tuple[BrokerOrderRecord, bool]:
        account = intent.request.account
        if not account:
            raise ValueError("intent.request.account must be resolved before persistence")
        intent_hash = canonical_order_hash(intent)
        with self._session_factory() as session:
            existing = self._by_client_id(session, intent.client_order_id)
            if existing is not None:
                self._assert_same_intent(existing, intent_hash)
                if (
                    expected_broker_client_id is not None
                    and existing.broker_client_id not in {
                        None,
                        expected_broker_client_id,
                    }
                ):
                    raise IdempotencyConflictError(
                        "client order belongs to another broker client ID"
                    )
                if (
                    expected_broker_client_id is not None
                    and existing.broker_client_id is None
                    and OrderLifecycleState(existing.state)
                    in {
                        OrderLifecycleState.INTENT_PERSISTED,
                        OrderLifecycleState.AUTHORIZED,
                    }
                ):
                    existing.broker_client_id = expected_broker_client_id
                    session.commit()
                return existing, True
            instrument = intent.request.instrument
            expiry = instrument.expiry
            if isinstance(expiry, datetime):
                expiry = expiry.date()
            now = _utcnow()
            row = BrokerOrderRecord(
                client_order_id=intent.client_order_id,
                intent_hash=intent_hash,
                current_request_hash=intent_hash,
                account=account,
                strategy_code=intent.strategy_code,
                asset_type=instrument.asset_type.value,
                symbol=instrument.symbol,
                conid=instrument.conid,
                currency=instrument.currency,
                venue=instrument.venue,
                expiry=expiry,
                option_right=instrument.option_right,
                strike=instrument.strike,
                side=intent.request.side,
                order_type=intent.request.order_type,
                quantity=intent.request.quantity,
                limit_price=intent.request.limit_price,
                stop_price=intent.request.stop_price,
                tif=intent.request.tif,
                order_ref=intent.request.order_ref or intent.client_order_id,
                transmit=intent.request.transmit,
                what_if=intent.request.what_if,
                outside_rth=intent.request.outside_rth,
                good_after_time=(
                    _as_utc(intent.request.good_after_time)
                    if intent.request.good_after_time
                    else None
                ),
                good_till_date=(
                    _as_utc(intent.request.good_till_date)
                    if intent.request.good_till_date
                    else None
                ),
                oca_group=intent.request.oca_group,
                oca_type=intent.request.oca_type,
                reduce_only=intent.request.reduce_only,
                request_payload=intent.request.model_dump(mode="json"),
                state=OrderLifecycleState.INTENT_PERSISTED.value,
                broker_client_id=expected_broker_client_id,
                remaining=intent.request.quantity,
                created_at=_as_utc(intent.created_at),
                updated_at=now,
                expires_at=_as_utc(intent.expires_at) if intent.expires_at else None,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._by_client_id(session, intent.client_order_id)
                if existing is None:
                    raise
                self._assert_same_intent(existing, intent_hash)
                if (
                    expected_broker_client_id is not None
                    and existing.broker_client_id != expected_broker_client_id
                ):
                    raise IdempotencyConflictError(
                        "client order belongs to another broker client ID"
                    )
                return existing, True
            return row, False

    def get(self, client_order_id: str) -> BrokerOrderRecord | None:
        with self._session_factory() as session:
            return self._by_client_id(session, client_order_id)

    def find_for_broker_event(
        self,
        *,
        account: str | None,
        broker_order_id: int | None,
        broker_client_id: int | None = None,
        permanent_id: int | None = None,
        order_ref: str | None = None,
    ) -> BrokerOrderRecord | None:
        with self._session_factory() as session:
            return _match_broker_order(
                session,
                account=account,
                broker_order_id=broker_order_id,
                broker_client_id=broker_client_id,
                permanent_id=permanent_id,
                order_ref=order_ref,
            )

    def transition(
        self,
        client_order_id: str,
        state: OrderLifecycleState,
        *,
        broker_status: BrokerOrderStatus | None = None,
        risk_decision_id: str | None = None,
        reason: str | None = None,
    ) -> BrokerOrderRecord:
        with self._session_factory() as session, session.begin():
            row = self._by_client_id(session, client_order_id, for_update=True)
            if row is None:
                raise KeyError(f"unknown client_order_id {client_order_id!r}")
            current = OrderLifecycleState(row.state)
            if current != state:
                if current in TERMINAL_ORDER_STATES:
                    raise InvalidOrderTransitionError(f"terminal order cannot move {current} -> {state}")
                if state not in _ALLOWED_TRANSITIONS.get(current, set()):
                    raise InvalidOrderTransitionError(f"invalid order transition {current} -> {state}")
            row.state = state.value
            row.updated_at = _utcnow()
            row.revision += 1
            if risk_decision_id is not None:
                row.risk_decision_id = risk_decision_id
            if reason is not None:
                row.last_error = reason
            if broker_status is not None:
                row.broker_status = broker_status.status
                row.broker_order_id = broker_status.order_id
                row.broker_client_id = broker_status.client_id
                row.permanent_id = broker_status.permanent_id
                row.parent_order_id = broker_status.parent_id
                row.filled = broker_status.filled
                row.remaining = broker_status.remaining
                row.avg_fill_price = broker_status.avg_fill_price
                row.last_event_at = _as_utc(broker_status.updated_at)
            return row

    def claim_submission(self, client_order_id: str) -> str | None:
        """以原子方式只允许一个进程调用 broker.placeOrder。"""

        attempt_id = str(uuid4())
        now = _utcnow()
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(BrokerOrderRecord)
                .where(
                    BrokerOrderRecord.client_order_id == client_order_id,
                    BrokerOrderRecord.state == OrderLifecycleState.AUTHORIZED.value,
                )
                .values(
                    state=OrderLifecycleState.SUBMITTING.value,
                    submission_attempt_id=attempt_id,
                    submission_started_at=now,
                    updated_at=now,
                    revision=BrokerOrderRecord.revision + 1,
                )
            )
            return attempt_id if result.rowcount == 1 else None

    def claim_submission_group(self, client_order_ids: list[str]) -> str | None:
        if not client_order_ids or len(client_order_ids) != len(set(client_order_ids)):
            raise ValueError("submission group requires unique client order IDs")
        attempt_id = str(uuid4())
        now = _utcnow()
        try:
            with self._session_factory() as session, session.begin():
                result = session.execute(
                    update(BrokerOrderRecord)
                    .where(
                        BrokerOrderRecord.client_order_id.in_(client_order_ids),
                        BrokerOrderRecord.state == OrderLifecycleState.AUTHORIZED.value,
                    )
                    .values(
                        state=OrderLifecycleState.SUBMITTING.value,
                        submission_attempt_id=attempt_id,
                        submission_started_at=now,
                        updated_at=now,
                        revision=BrokerOrderRecord.revision + 1,
                    )
                )
                if result.rowcount != len(client_order_ids):
                    raise _SubmissionClaimConflict
        except _SubmissionClaimConflict:
            return None
        return attempt_id

    def claim_replacement(
        self,
        client_order_id: str,
        *,
        expected_revision: int,
        request: Any,
        request_hash: str,
        risk_decision_id: str,
    ) -> str | None:
        attempt_id = str(uuid4())
        now = _utcnow()
        active_states = {
            OrderLifecycleState.ACKNOWLEDGED.value,
            OrderLifecycleState.PARTIAL_FILL.value,
        }
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(BrokerOrderRecord)
                .where(
                    BrokerOrderRecord.client_order_id == client_order_id,
                    BrokerOrderRecord.revision == expected_revision,
                    BrokerOrderRecord.state.in_(active_states),
                    BrokerOrderRecord.filled <= request.quantity,
                )
                .values(
                    state=OrderLifecycleState.REPLACE_PENDING.value,
                    pending_request_hash=request_hash,
                    pending_request_payload=request.model_dump(mode="json"),
                    risk_decision_id=risk_decision_id,
                    submission_attempt_id=attempt_id,
                    submission_started_at=now,
                    updated_at=now,
                    revision=BrokerOrderRecord.revision + 1,
                )
            )
            return attempt_id if result.rowcount == 1 else None

    def claim_cancellation(
        self, client_order_id: str, *, expected_revision: int
    ) -> str | None:
        attempt_id = str(uuid4())
        now = _utcnow()
        cancellable = {
            OrderLifecycleState.SUBMITTING.value,
            OrderLifecycleState.ACKNOWLEDGED.value,
            OrderLifecycleState.PARTIAL_FILL.value,
            OrderLifecycleState.REPLACE_PENDING.value,
            OrderLifecycleState.UNKNOWN.value,
        }
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(BrokerOrderRecord)
                .where(
                    BrokerOrderRecord.client_order_id == client_order_id,
                    BrokerOrderRecord.revision == expected_revision,
                    BrokerOrderRecord.state.in_(cancellable),
                    BrokerOrderRecord.broker_order_id.is_not(None),
                )
                .values(
                    state=OrderLifecycleState.CANCEL_PENDING.value,
                    submission_attempt_id=attempt_id,
                    submission_started_at=now,
                    updated_at=now,
                    revision=BrokerOrderRecord.revision + 1,
                )
            )
            return attempt_id if result.rowcount == 1 else None

    def record_event(self, event: BrokerEvent, *, dedupe_key: str | None = None) -> bool:
        key = dedupe_key or _event_dedupe_key(event)
        with self._session_factory() as session:
            linked = self._resolve_event_order(session, event)
            row = BrokerOrderEventRecord(
                dedupe_key=key,
                order_record_id=linked.order_record_id if linked else None,
                event_type=event.event_type.value,
                event_time=_as_utc(event.event_time),
                account=event.account,
                client_order_id=event.client_order_id,
                broker_order_id=event.broker_order_id,
                permanent_id=event.permanent_id,
                execution_id=event.execution_id,
                payload=_json_safe(event.payload),
            )
            session.add(row)
            if linked is not None:
                linked.last_event_at = _as_utc(event.event_time)
                linked.updated_at = _utcnow()
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
            return True

    def strategy_order_events(
        self,
        strategy_code: str,
        *,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> list[StrategyOrderEvent]:
        """通过单调递增且按策略隔离的游标读取关联经纪商事件。"""

        normalized = strategy_code.strip()
        if not normalized:
            raise ValueError("strategy_code is required")
        if after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")
        if not 1 <= limit <= 500:
            raise ValueError("event page limit must be between 1 and 500")
        with self._session_factory() as session:
            rows = session.execute(
                select(BrokerOrderEventRecord, BrokerOrderRecord.client_order_id)
                .join(
                    BrokerOrderRecord,
                    BrokerOrderRecord.order_record_id
                    == BrokerOrderEventRecord.order_record_id,
                )
                .where(
                    BrokerOrderRecord.strategy_code == normalized,
                    BrokerOrderEventRecord.event_id > after_event_id,
                )
                .order_by(BrokerOrderEventRecord.event_id.asc())
                .limit(limit)
            )
            return [
                StrategyOrderEvent(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    event_time=_as_utc(event.event_time),
                    received_at=_as_utc(event.received_at),
                    client_order_id=client_order_id,
                    broker_order_id=event.broker_order_id,
                    permanent_id=event.permanent_id,
                    execution_id=event.execution_id,
                    payload=dict(event.payload or {}),
                )
                for event, client_order_id in rows
            ]

    def record_risk_decision(self, decision: RiskAuthorization) -> None:
        with self._session_factory() as session:
            if session.get(RiskDecisionRecord, decision.decision_id) is not None:
                return
            session.add(
                RiskDecisionRecord(
                    decision_id=decision.decision_id,
                    client_order_id=decision.client_order_id,
                    account=decision.account,
                    approved=decision.approved,
                    code=decision.code,
                    detail=decision.detail,
                    order_hash=decision.order_hash,
                    decided_at=_as_utc(decision.decided_at),
                    expires_at=_as_utc(decision.expires_at),
                    computed_notional=decision.computed_notional,
                    projected_symbol_notional=decision.projected_symbol_notional,
                    reasons=_json_safe(decision.reasons),
                )
            )
            session.commit()

    def upsert_execution(self, execution: BrokerExecution) -> BrokerExecutionRecord:
        now = _utcnow()
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(BrokerExecutionRecord).where(
                    BrokerExecutionRecord.execution_id == execution.execution_id
                )
            )
            order = self._find_order_for_execution(session, execution)
            execution_root = _execution_root_id(execution.execution_id)
            prior_versions = list(
                session.scalars(
                    select(BrokerExecutionRecord).where(
                        BrokerExecutionRecord.execution_root_id == execution_root,
                        BrokerExecutionRecord.execution_id != execution.execution_id,
                        BrokerExecutionRecord.superseded.is_(False),
                    )
                )
            )
            pending_commission = session.scalar(
                select(BrokerOrderEventRecord)
                .where(
                    BrokerOrderEventRecord.execution_id == execution.execution_id,
                    BrokerOrderEventRecord.event_type == "COMMISSION",
                )
                .order_by(BrokerOrderEventRecord.event_id.desc())
            )
            commission_payload = pending_commission.payload if pending_commission else {}
            instrument = execution.instrument
            expiry = instrument.expiry
            if isinstance(expiry, datetime):
                expiry = expiry.date()
            values = {
                "order_record_id": order.order_record_id if order else None,
                "execution_root_id": execution_root,
                "is_correction": bool(prior_versions),
                "superseded": False,
                "broker_order_id": execution.order_id,
                "permanent_id": execution.permanent_id,
                "broker_client_id": execution.client_id,
                "account": execution.account,
                "order_ref": execution.order_ref,
                "asset_type": instrument.asset_type.value,
                "symbol": instrument.symbol,
                "conid": instrument.conid,
                "currency": instrument.currency,
                "venue": execution.exchange or instrument.venue,
                "expiry": expiry,
                "option_right": instrument.option_right,
                "strike": instrument.strike,
                "side": execution.side,
                "quantity": execution.quantity,
                "price": execution.price,
                "executed_at": _as_utc(execution.executed_at),
                "commission": execution.commission
                if execution.commission is not None
                else _decimal_or_none(commission_payload.get("commission")),
                "commission_currency": execution.commission_currency
                or commission_payload.get("currency"),
                "realized_pnl": execution.realized_pnl
                if execution.realized_pnl is not None
                else _decimal_or_none(commission_payload.get("realized_pnl")),
                "raw_payload": execution.model_dump(mode="json"),
                "updated_at": now,
            }
            is_new = row is None
            if is_new:
                for prior in prior_versions:
                    prior.superseded = True
                    prior.updated_at = now
                row = BrokerExecutionRecord(execution_id=execution.execution_id, **values)
                session.add(row)
            else:
                for key, value in values.items():
                    if value is not None or getattr(row, key) is None:
                        setattr(row, key, value)
            session.flush()
            if order is not None and is_new:
                total_quantity, total_value = session.execute(
                    select(
                        func.sum(BrokerExecutionRecord.quantity),
                        func.sum(BrokerExecutionRecord.quantity * BrokerExecutionRecord.price),
                    ).where(BrokerExecutionRecord.order_record_id == order.order_record_id)
                    .where(BrokerExecutionRecord.superseded.is_(False))
                ).one()
                aggregate_quantity = Decimal(str(total_quantity or 0))
                aggregate_value = Decimal(str(total_value or 0))
                if prior_versions or aggregate_quantity >= order.filled:
                    order.filled = min(order.quantity, aggregate_quantity)
                    order.remaining = max(Decimal("0"), order.quantity - order.filled)
                    if aggregate_quantity > 0:
                        order.avg_fill_price = aggregate_value / aggregate_quantity
                if order.remaining == 0 and order.filled > 0:
                    order.state = OrderLifecycleState.FILLED.value
                elif order.filled > 0 and OrderLifecycleState(order.state) not in TERMINAL_ORDER_STATES:
                    order.state = OrderLifecycleState.PARTIAL_FILL.value
                order.updated_at = now
                order.last_event_at = _as_utc(execution.executed_at)
                order.revision += 1
            return row

    def attach_commission(
        self,
        execution_id: str,
        *,
        commission: Decimal | None,
        currency: str | None,
        realized_pnl: Decimal | None,
    ) -> bool:
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(BrokerExecutionRecord).where(
                    BrokerExecutionRecord.execution_id == execution_id
                )
            )
            if row is None:
                return False
            row.commission = commission
            row.commission_currency = currency
            row.realized_pnl = realized_pnl
            row.updated_at = _utcnow()
            return True

    def replace_positions(self, account: str, positions: Iterable[BrokerPosition]) -> None:
        captured_at = _utcnow()
        with self._session_factory() as session, session.begin():
            session.execute(delete(BrokerPositionRecord).where(BrokerPositionRecord.account == account))
            for position in positions:
                if position.account != account:
                    continue
                instrument = position.instrument
                expiry = instrument.expiry
                if isinstance(expiry, datetime):
                    expiry = expiry.date()
                session.add(
                    BrokerPositionRecord(
                        position_key=_position_key(account, instrument),
                        account=account,
                        strategy_code=None,
                        asset_type=instrument.asset_type.value,
                        symbol=instrument.symbol,
                        conid=instrument.conid,
                        currency=instrument.currency,
                        venue=instrument.venue,
                        expiry=expiry,
                        option_right=instrument.option_right,
                        strike=instrument.strike,
                        quantity=position.quantity,
                        avg_cost=position.avg_cost,
                        captured_at=captured_at,
                    )
                )

    def record_account_snapshot(self, snapshot: AccountRiskSnapshot) -> None:
        payload = snapshot.model_dump(mode="json")
        with self._session_factory() as session, session.begin():
            session.add(
                BrokerAccountSnapshotRecord(
                    account=snapshot.account,
                    captured_at=_as_utc(snapshot.captured_at),
                    net_liquidation=snapshot.net_liquidation,
                    available_funds=snapshot.available_funds,
                    buying_power=snapshot.buying_power,
                    maintenance_margin=snapshot.maintenance_margin,
                    daily_pnl=snapshot.daily_pnl,
                    realized_pnl=snapshot.realized_pnl,
                    unrealized_pnl=snapshot.unrealized_pnl,
                    gross_position_notional=snapshot.gross_position_notional,
                    open_order_notional=snapshot.open_order_notional,
                    daily_order_count=snapshot.daily_order_count,
                    daily_traded_notional=snapshot.daily_traded_notional,
                    market_data_type=snapshot.market_data_type,
                    payload=payload,
                )
            )

    def daily_activity(
        self, account: str, *, now: datetime | None = None
    ) -> tuple[int, Decimal]:
        current = _as_utc(now or _utcnow())
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._session_factory() as session:
            order_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(BrokerOrderRecord)
                    .where(
                        BrokerOrderRecord.account == account,
                        BrokerOrderRecord.submission_started_at >= start,
                    )
                )
                or 0
            )
            executions = list(
                session.scalars(
                    select(BrokerExecutionRecord).where(
                        BrokerExecutionRecord.account == account,
                        BrokerExecutionRecord.executed_at >= start,
                        BrokerExecutionRecord.superseded.is_(False),
                    )
                )
            )
        traded_notional = Decimal("0")
        for execution in executions:
            instrument = (execution.raw_payload or {}).get("instrument", {})
            metadata = instrument.get("metadata", {}) if isinstance(instrument, dict) else {}
            default_multiplier = (
                "100" if execution.asset_type in {"OPTION", "COMBO"} else "1"
            )
            multiplier = Decimal(str(metadata.get("multiplier", default_multiplier)))
            traded_notional += abs(execution.quantity * execution.price * multiplier)
        return order_count, traded_notional

    def open_order_risk_notional(self, account: str) -> Decimal:
        """保守重建所有可能仍存活的本地订单敞口。"""

        total = Decimal("0")
        for order in self.nonterminal_orders(account):
            if order.submission_started_at is None or order.remaining <= 0:
                continue
            instrument = (order.request_payload or {}).get("instrument", {})
            metadata = instrument.get("metadata", {}) if isinstance(instrument, dict) else {}
            if order.asset_type == "COMBO":
                configured = metadata.get("max_loss_per_unit")
                if configured is None or Decimal(str(configured)) <= 0:
                    raise RuntimeError(
                        f"cannot reconstruct BAG risk for {order.client_order_id}"
                    )
                total += order.remaining * Decimal(str(configured))
                continue
            price = order.limit_price or order.stop_price or order.avg_fill_price
            if price is None or price <= 0:
                raise RuntimeError(
                    f"cannot reconstruct open-order price for {order.client_order_id}"
                )
            multiplier = Decimal("100") if order.asset_type == "OPTION" else Decimal("1")
            total += order.remaining * abs(price) * multiplier
        return total

    def nonterminal_orders(self, account: str) -> list[BrokerOrderRecord]:
        terminal = [state.value for state in TERMINAL_ORDER_STATES]
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(BrokerOrderRecord)
                    .where(
                        BrokerOrderRecord.account == account,
                        BrokerOrderRecord.state.not_in(terminal),
                    )
                    .order_by(BrokerOrderRecord.created_at)
                )
            )

    def current_positions(self, account: str) -> list[BrokerPositionRecord]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(BrokerPositionRecord).where(
                        BrokerPositionRecord.account == account
                    )
                )
            )

    def execution_records(
        self,
        account: str,
        *,
        period_start: datetime,
        period_end: datetime,
    ) -> list[BrokerExecutionRecord]:
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(BrokerExecutionRecord).where(
                        BrokerExecutionRecord.account == account,
                        BrokerExecutionRecord.executed_at >= _as_utc(period_start),
                        BrokerExecutionRecord.executed_at < _as_utc(period_end),
                        BrokerExecutionRecord.superseded.is_(False),
                    )
                )
            )

    def record_statement_reconciliation(
        self,
        *,
        reconciliation_id: str,
        account: str,
        provider: str,
        period_start: datetime,
        period_end: datetime,
        ok: bool,
        issues: list[dict[str, Any]],
        statement_payload: dict[str, Any],
        actor: str,
        reconciled_at: datetime,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.add(
                StatementReconciliationRecord(
                    statement_reconciliation_id=reconciliation_id,
                    account=account,
                    provider=provider,
                    period_start=_as_utc(period_start),
                    period_end=_as_utc(period_end),
                    status="MATCHED" if ok else "BLOCKED",
                    issues=_json_safe(issues),
                    statement_payload=_json_safe(statement_payload),
                    reconciled_by=actor,
                    reconciled_at=_as_utc(reconciled_at),
                )
            )

    def apply_reconciled_status(
        self,
        client_order_id: str,
        *,
        state: OrderLifecycleState,
        broker_status: BrokerOrderStatus,
        settle_pending_replacement: bool = False,
    ) -> BrokerOrderRecord:
        """应用经纪商权威状态，同时禁止重新打开本地终态订单。"""

        with self._session_factory() as session, session.begin():
            row = self._by_client_id(session, client_order_id, for_update=True)
            if row is None:
                raise KeyError(f"unknown client_order_id {client_order_id!r}")
            revision_fields_before = _broker_revision_fields(row)
            request_mismatch = False
            if row.pending_request_payload is not None:
                pending_matches = broker_status_matches_request(
                    broker_status, row.pending_request_payload
                )
                if pending_matches:
                    _promote_pending_request(row)
                elif settle_pending_replacement:
                    if broker_status_matches_request(
                        broker_status, row.request_payload
                    ):
                        row.pending_request_hash = None
                        row.pending_request_payload = None
                        row.last_error = (
                            "broker reconciliation confirmed the prior order terms; "
                            "replacement was not applied"
                        )
                    else:
                        request_mismatch = True
                        row.last_error = (
                            "broker order terms match neither the confirmed request nor "
                            "the pending replacement"
                        )
            current = OrderLifecycleState(row.state)
            if request_mismatch:
                if current not in TERMINAL_ORDER_STATES:
                    row.state = OrderLifecycleState.UNKNOWN.value
            elif current in TERMINAL_ORDER_STATES and current != state:
                row.last_error = (
                    f"reconciliation conflict: local terminal {current.value}, "
                    f"broker {state.value}"
                )
            elif (
                current == OrderLifecycleState.REPLACE_PENDING
                and row.pending_request_payload is not None
                and state not in TERMINAL_ORDER_STATES
            ):
                # 仅含状态的回调可能仍对应改单前的工作订单；在 openOrder 快照确认实际参数前，
                # 必须保持改单待确认状态。
                pass
            elif not _is_order_state_regression(current, state):
                row.state = state.value
            if state in TERMINAL_ORDER_STATES:
                row.pending_request_hash = None
                row.pending_request_payload = None
            row.broker_status = broker_status.status
            row.broker_order_id = broker_status.order_id
            row.broker_client_id = broker_status.client_id
            row.permanent_id = broker_status.permanent_id
            row.parent_order_id = broker_status.parent_id
            row.filled = broker_status.filled
            row.remaining = broker_status.remaining
            row.avg_fill_price = broker_status.avg_fill_price
            row.last_event_at = _as_utc(broker_status.updated_at)
            row.updated_at = _utcnow()
            # IBKR 可能重复发送 openOrder 或 orderStatus 回调；事件时间仍写入日志，但语义上
            # 没有变化的回调不得使操作员在读取与改单或撤单之间取得的乐观版本失效。
            if _broker_revision_fields(row) != revision_fields_before:
                row.revision += 1
            return row

    def mark_expired_after_cancel(
        self, client_order_id: str, *, expired_at: datetime
    ) -> BrokerOrderRecord:
        """重新标记由本地 TTL 触发且经纪商已确认的撤单。"""

        with self._session_factory() as session, session.begin():
            row = self._by_client_id(session, client_order_id, for_update=True)
            if row is None:
                raise KeyError(f"unknown client_order_id {client_order_id!r}")
            if OrderLifecycleState(row.state) != OrderLifecycleState.CANCELLED:
                raise InvalidOrderTransitionError(
                    f"cannot mark {row.state} order as TTL-expired"
                )
            row.state = OrderLifecycleState.EXPIRED.value
            row.last_error = f"order TTL elapsed at {_as_utc(expired_at).isoformat()}"
            row.updated_at = _utcnow()
            row.revision += 1
            return row

    def record_reconciliation(self, report: ReconciliationReport, *, trigger: str) -> None:
        with self._session_factory() as session, session.begin():
            session.add(
                ReconciliationRunRecord(
                    account=report.account,
                    trigger=trigger,
                    started_at=_as_utc(report.started_at),
                    completed_at=_as_utc(report.completed_at),
                    status="READY" if report.ok else "BLOCKED",
                    open_order_count=report.open_order_count,
                    execution_count=report.execution_count,
                    position_count=report.position_count,
                    issues=[issue.model_dump(mode="json") for issue in report.issues],
                )
            )

    def latest_successful_reconciliation_at(self, account: str) -> datetime | None:
        """返回账户的持久化持仓基线水位。"""

        with self._session_factory() as session:
            completed_at = session.scalar(
                select(ReconciliationRunRecord.completed_at)
                .where(
                    ReconciliationRunRecord.account == account,
                    ReconciliationRunRecord.status == "READY",
                    ReconciliationRunRecord.completed_at.is_not(None),
                )
                .order_by(ReconciliationRunRecord.completed_at.desc())
                .limit(1)
            )
        return _as_utc(completed_at) if completed_at is not None else None

    def set_kill_switch(self, scope: str, *, reason: str, changed_by: str) -> None:
        if not reason.strip():
            raise ValueError("kill-switch reason is required")
        self._upsert_control(scope, killed=True, reason=reason.strip(), changed_by=changed_by)

    def clear_kill_switch(self, scope: str, *, changed_by: str) -> None:
        self._upsert_control(scope, killed=False, reason=None, changed_by=changed_by)

    def kill_switch_reason(self, scope: str) -> str | None:
        with self._session_factory() as session:
            row = session.get(TradingControlRecord, scope)
            return row.reason if row is not None and row.killed else None

    def acquire_execution_lease(
        self,
        *,
        account: str,
        holder_id: str,
        ttl_seconds: int = 45,
        now: datetime | None = None,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("execution lease TTL must be positive")
        current = _as_utc(now or _utcnow())
        try:
            with self._session_factory() as session, session.begin():
                row = session.scalar(
                    select(ExecutionLeaseRecord)
                    .where(ExecutionLeaseRecord.account == account)
                    .with_for_update()
                )
                if row is None:
                    session.add(
                        ExecutionLeaseRecord(
                            account=account,
                            holder_id=holder_id,
                            acquired_at=current,
                            renewed_at=current,
                            expires_at=current + timedelta(seconds=ttl_seconds),
                        )
                    )
                    session.flush()
                    return True
                if row.holder_id != holder_id and _as_utc(row.expires_at) > current:
                    return False
                if row.holder_id != holder_id:
                    row.acquired_at = current
                row.holder_id = holder_id
                row.renewed_at = current
                row.expires_at = current + timedelta(seconds=ttl_seconds)
                return True
        except IntegrityError:
            return False

    def renew_execution_lease(
        self,
        *,
        account: str,
        holder_id: str,
        ttl_seconds: int = 45,
        now: datetime | None = None,
    ) -> bool:
        current = _as_utc(now or _utcnow())
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(ExecutionLeaseRecord)
                .where(
                    ExecutionLeaseRecord.account == account,
                    ExecutionLeaseRecord.holder_id == holder_id,
                    ExecutionLeaseRecord.expires_at > current,
                )
                .values(
                    renewed_at=current,
                    expires_at=current + timedelta(seconds=ttl_seconds),
                )
            )
            return result.rowcount == 1

    def release_execution_lease(self, *, account: str, holder_id: str) -> bool:
        with self._session_factory() as session, session.begin():
            result = session.execute(
                delete(ExecutionLeaseRecord).where(
                    ExecutionLeaseRecord.account == account,
                    ExecutionLeaseRecord.holder_id == holder_id,
                )
            )
            return result.rowcount == 1

    def _upsert_control(
        self, scope: str, *, killed: bool, reason: str | None, changed_by: str
    ) -> None:
        with self._session_factory() as session, session.begin():
            row = session.get(TradingControlRecord, scope)
            if row is None:
                session.add(
                    TradingControlRecord(
                        scope=scope,
                        killed=killed,
                        reason=reason,
                        changed_by=changed_by,
                        updated_at=_utcnow(),
                    )
                )
            else:
                row.killed = killed
                row.reason = reason
                row.changed_by = changed_by
                row.updated_at = _utcnow()

    @staticmethod
    def _by_client_id(
        session: Session, client_order_id: str, *, for_update: bool = False
    ) -> BrokerOrderRecord | None:
        statement = select(BrokerOrderRecord).where(
            BrokerOrderRecord.client_order_id == client_order_id
        )
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)

    @staticmethod
    def _assert_same_intent(row: BrokerOrderRecord, intent_hash: str) -> None:
        if row.intent_hash != intent_hash:
            raise IdempotencyConflictError(
                f"client_order_id {row.client_order_id!r} was already used for another payload"
            )

    @staticmethod
    def _resolve_event_order(session: Session, event: BrokerEvent) -> BrokerOrderRecord | None:
        linked = _match_broker_order(
            session,
            account=event.account,
            broker_order_id=event.broker_order_id,
            broker_client_id=_int_or_none(event.payload.get("client_id")),
            permanent_id=event.permanent_id,
            order_ref=event.client_order_id,
        )
        if linked is not None or not event.execution_id:
            return linked
        # IBKR 的 commissionReport 仅包含 execId；从成交记录恢复策略归属，使佣金回调继续进入
        # 同一策略事件流。
        execution = session.scalar(
            select(BrokerExecutionRecord).where(
                BrokerExecutionRecord.execution_id == event.execution_id
            )
        )
        if execution is None or execution.order_record_id is None:
            return None
        return session.get(BrokerOrderRecord, execution.order_record_id)

    @staticmethod
    def _find_order_for_execution(
        session: Session, execution: BrokerExecution
    ) -> BrokerOrderRecord | None:
        return _match_broker_order(
            session,
            account=execution.account,
            broker_order_id=execution.order_id,
            broker_client_id=execution.client_id,
            permanent_id=execution.permanent_id,
            order_ref=execution.order_ref,
        )


def _position_key(account: str, instrument: Any) -> str:
    identity = instrument.conid or ":".join(
        str(value or "")
        for value in (
            instrument.asset_type.value,
            instrument.symbol,
            instrument.currency,
            instrument.venue,
            instrument.expiry,
            instrument.option_right,
            instrument.strike,
        )
    )
    return f"{account}:{identity}"


def broker_status_matches_request(
    status: BrokerOrderStatus, request_payload: dict[str, Any]
) -> bool:
    """不依赖本地身份字段，比较经纪商可见的订单参数。"""

    try:
        request = BrokerOrderRequest.model_validate(request_payload)
    except Exception:  # noqa: BLE001 - 持久化数据损坏时必须关闭交易。
        return False
    if status.quantity is None or status.side is None or status.order_type is None:
        return False
    if Decimal(str(status.quantity)) != request.quantity:
        return False
    if status.side.upper() != request.side:
        return False
    if status.order_type.replace(" ", "_").upper() != request.order_type:
        return False
    if status.tif is None or status.tif.upper() != request.tif:
        return False
    if _decimal_or_none(status.limit_price) != request.limit_price:
        return False
    if _decimal_or_none(status.stop_price) != request.stop_price:
        return False
    if status.instrument is not None:
        broker_instrument = status.instrument
        if (
            broker_instrument.conid is not None
            and request.instrument.conid is not None
            and broker_instrument.conid != request.instrument.conid
        ):
            return False
        if broker_instrument.symbol.upper() != request.instrument.symbol.upper():
            return False
        if broker_instrument.currency.upper() != request.instrument.currency.upper():
            return False
    return True


def _promote_pending_request(row: BrokerOrderRecord) -> None:
    payload = row.pending_request_payload
    request_hash = row.pending_request_hash
    if payload is None or request_hash is None:
        raise RuntimeError("pending replacement is incomplete")
    request = BrokerOrderRequest.model_validate(payload)
    row.current_request_hash = request_hash
    row.request_payload = payload
    row.quantity = request.quantity
    row.remaining = max(Decimal("0"), request.quantity - row.filled)
    row.limit_price = request.limit_price
    row.stop_price = request.stop_price
    row.tif = request.tif
    row.transmit = request.transmit
    row.what_if = request.what_if
    row.outside_rth = request.outside_rth
    row.good_after_time = (
        _as_utc(request.good_after_time) if request.good_after_time else None
    )
    row.good_till_date = (
        _as_utc(request.good_till_date) if request.good_till_date else None
    )
    row.reduce_only = request.reduce_only
    row.pending_request_hash = None
    row.pending_request_payload = None


def _match_broker_order(
    session: Session,
    *,
    account: str | None,
    broker_order_id: int | None,
    broker_client_id: int | None,
    permanent_id: int | None,
    order_ref: str | None,
) -> BrokerOrderRecord | None:
    clauses = []
    if permanent_id:
        clauses.append(BrokerOrderRecord.permanent_id == permanent_id)
    if broker_order_id is not None:
        identity = BrokerOrderRecord.broker_order_id == broker_order_id
        if account:
            identity &= BrokerOrderRecord.account == account
        clauses.append(identity)
    if order_ref:
        reference = or_(
            BrokerOrderRecord.client_order_id == order_ref,
            BrokerOrderRecord.order_ref == order_ref,
        )
        if account:
            reference &= BrokerOrderRecord.account == account
        clauses.append(reference)
    if not clauses:
        return None
    candidates = list(session.scalars(select(BrokerOrderRecord).where(or_(*clauses))))
    compatible = [
        row
        for row in candidates
        if _broker_identity_is_compatible(
            row,
            account=account,
            broker_order_id=broker_order_id,
            broker_client_id=broker_client_id,
            permanent_id=permanent_id,
            order_ref=order_ref,
        )
    ]
    if len(compatible) != 1:
        return None
    return compatible[0]


def _broker_identity_is_compatible(
    row: BrokerOrderRecord,
    *,
    account: str | None,
    broker_order_id: int | None,
    broker_client_id: int | None,
    permanent_id: int | None,
    order_ref: str | None,
) -> bool:
    if account is not None and row.account != account:
        return False
    if (
        row.broker_order_id is not None
        and broker_order_id is not None
        and row.broker_order_id != broker_order_id
    ):
        return False
    if (
        row.broker_client_id is not None
        and broker_client_id is not None
        and row.broker_client_id != broker_client_id
    ):
        return False
    if (
        row.permanent_id is not None
        and permanent_id is not None
        and row.permanent_id != permanent_id
    ):
        return False
    if order_ref is not None and row.order_ref != order_ref:
        return False
    broker_id_match = (
        row.broker_order_id is not None
        and broker_order_id is not None
        and row.broker_order_id == broker_order_id
        and row.broker_client_id is not None
        and broker_client_id is not None
        and row.broker_client_id == broker_client_id
    )
    permanent_id_match = (
        row.permanent_id is not None
        and permanent_id is not None
        and row.permanent_id == permanent_id
    )
    expected_client_ref_match = (
        order_ref is not None
        and row.order_ref == order_ref
        and row.broker_client_id is not None
        and broker_client_id is not None
        and row.broker_client_id == broker_client_id
    )
    return broker_id_match or permanent_id_match or expected_client_ref_match


def _broker_revision_fields(row: BrokerOrderRecord) -> tuple[Any, ...]:
    """变化后会影响调用方改单或撤单决策的字段。"""

    return (
        row.state,
        row.broker_order_id,
        row.broker_client_id,
        row.permanent_id,
        row.parent_order_id,
        row.filled,
        row.remaining,
        row.avg_fill_price,
        row.current_request_hash,
        row.request_payload,
        row.pending_request_hash,
        row.pending_request_payload,
        row.last_error,
    )


def _event_dedupe_key(event: BrokerEvent) -> str:
    identity = {
        "event_type": event.event_type.value,
        "account": event.account,
        "client_order_id": event.client_order_id,
        "broker_order_id": event.broker_order_id,
        "permanent_id": event.permanent_id,
        "execution_id": event.execution_id,
        "payload": event.payload,
    }
    return f"{event.event_type.value}:{_hash_payload(identity)}"


def _execution_root_id(execution_id: str) -> str:
    head, separator, suffix = execution_id.rpartition(".")
    if separator and suffix.isdigit():
        return head
    return execution_id


def _is_order_state_regression(
    current: OrderLifecycleState, target: OrderLifecycleState
) -> bool:
    if current == target or current == OrderLifecycleState.UNKNOWN:
        return False
    if target == OrderLifecycleState.UNKNOWN:
        return True
    rank = {
        OrderLifecycleState.INTENT_PERSISTED: 0,
        OrderLifecycleState.AUTHORIZED: 1,
        OrderLifecycleState.SUBMITTING: 2,
        OrderLifecycleState.ACKNOWLEDGED: 3,
        OrderLifecycleState.PARTIAL_FILL: 4,
        OrderLifecycleState.CANCEL_PENDING: 5,
    }
    return target in rank and current in rank and rank[target] < rank[current]


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(_json_safe(payload), sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return value


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)
