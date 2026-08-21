from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel

from platform_core.schemas import BrokerOrderRequest, BrokerPosition
from platform_core.schemas.assets import AssetType, InstrumentRef

from .execution import ExecutionSDK, ReconciliationBlockedError
from .ledger import InvalidOrderTransitionError, SQLAlchemyOrderLedger
from .models import (
    ExecutionResult,
    LiveOrderIntent,
    OrderCancelCommand,
    OrderLifecycleState,
)
from .safety import TradingSafetyController


class ExpiringOptionPosition(BaseModel):
    account: str
    instrument: InstrumentRef
    quantity: Decimal
    avg_cost: Decimal
    days_to_expiry: int
    action: Literal["MONITOR", "CLOSE", "EXPIRED"]


class OrderSupervisorSDK:
    """订单 TTL 与显式紧急平仓流程。"""

    def __init__(
        self,
        *,
        execution: ExecutionSDK,
        ledger: SQLAlchemyOrderLedger,
        safety: TradingSafetyController,
    ) -> None:
        self.execution = execution
        self.ledger = ledger
        self.safety = safety

    def expire_due_orders(
        self, *, account: str, now: datetime | None = None
    ) -> list[ExecutionResult]:
        current = _as_utc(now or datetime.now(UTC))
        results: list[ExecutionResult] = []
        for row in self.ledger.nonterminal_orders(account):
            if row.expires_at is None or _as_utc(row.expires_at) > current:
                continue
            if row.broker_order_id is None:
                expired = self.ledger.transition(
                    row.client_order_id,
                    OrderLifecycleState.EXPIRED,
                    reason="order intent TTL elapsed before broker submission",
                )
                results.append(_result(expired))
                continue
            try:
                cancellation = self.execution.cancel(
                    OrderCancelCommand(
                        client_order_id=row.client_order_id,
                        expected_revision=row.revision,
                    )
                )
                updated = self.ledger.get(row.client_order_id)
                if cancellation.state == OrderLifecycleState.CANCELLED:
                    updated = self.ledger.mark_expired_after_cancel(
                        row.client_order_id, expired_at=current
                    )
            except Exception as exc:
                try:
                    updated = self.ledger.transition(
                        row.client_order_id,
                        OrderLifecycleState.UNKNOWN,
                        reason=f"TTL cancellation outcome unknown: {type(exc).__name__}: {exc}",
                    )
                except InvalidOrderTransitionError:
                    updated = self.ledger.get(row.client_order_id)
            if updated is not None:
                results.append(_result(updated))
        return results

    def flatten_account(
        self,
        *,
        account: str,
        strategy_code: str,
        operation_id: str,
        confirmation: str,
        ttl_seconds: int = 30,
    ) -> list[ExecutionResult]:
        if confirmation != f"LIQUIDATE:{account}":
            raise PermissionError("flatten confirmation does not match account")
        if self.execution.broker.session_state.value != "READY":
            report = self.execution.reconciliation.run(account=account, trigger="LIQUIDATION")
            if not report.ok:
                raise ReconciliationBlockedError(report)
        self.safety.arm_liquidation(account=account, confirmation=confirmation)
        try:
            positions = self.execution.broker.positions(account=account)
            intents = self.build_flatten_intents(
                positions,
                account=account,
                strategy_code=strategy_code,
                operation_id=operation_id,
                ttl_seconds=ttl_seconds,
            )
            return [self.execution.submit(intent) for intent in intents]
        finally:
            self.safety.disarm_liquidation()

    def build_flatten_intents(
        self,
        positions: list[BrokerPosition],
        *,
        account: str,
        strategy_code: str,
        operation_id: str,
        ttl_seconds: int = 30,
    ) -> list[LiveOrderIntent]:
        if not operation_id.strip():
            raise ValueError("operation_id is required for idempotent flattening")
        now = datetime.now(UTC)
        intents: list[LiveOrderIntent] = []
        for position in positions:
            if position.account != account or position.quantity == 0:
                continue
            quote = self.execution.market_quote(position.instrument)
            side = "SELL" if position.quantity > 0 else "BUY"
            limit_price = quote.bid if side == "SELL" else quote.ask
            if limit_price is None or limit_price <= 0:
                raise RuntimeError(
                    f"cannot construct protected close for {position.instrument.symbol}"
                )
            identity = position.instrument.conid or position.instrument.model_dump_json()
            digest = sha256(f"{operation_id}:{account}:{identity}".encode()).hexdigest()[:24]
            client_order_id = f"close-{digest}"
            intents.append(
                LiveOrderIntent(
                    client_order_id=client_order_id,
                    strategy_code=strategy_code,
                    request=BrokerOrderRequest(
                        instrument=position.instrument,
                        side=side,
                        quantity=abs(position.quantity),
                        order_type="LMT",
                        limit_price=limit_price,
                        tif="IOC",
                        account=account,
                        order_ref=client_order_id,
                        reduce_only=True,
                    ),
                    created_at=now,
                    expires_at=now + timedelta(seconds=ttl_seconds),
                    metadata={"operation": "EMERGENCY_FLATTEN", "operation_id": operation_id},
                )
            )
        return intents


class OptionLifecycleSDK:
    """检测到期风险并路由显式平仓、行权或放弃行权操作。"""

    def __init__(
        self,
        *,
        broker: Any,
        ledger: SQLAlchemyOrderLedger,
        execution: ExecutionSDK | None = None,
    ) -> None:
        self.broker = broker
        self.ledger = ledger
        self.execution = execution

    def expiring_positions(
        self,
        *,
        account: str,
        as_of: date | None = None,
        close_days: int = 1,
        warning_days: int = 5,
    ) -> list[ExpiringOptionPosition]:
        current = as_of or datetime.now(UTC).date()
        output: list[ExpiringOptionPosition] = []
        for position in self.broker.positions(account=account):
            instrument = position.instrument
            if instrument.asset_type != AssetType.OPTION:
                continue
            expiry = instrument.expiry
            if isinstance(expiry, datetime):
                expiry = expiry.date()
            if expiry is None:
                continue
            dte = (expiry - current).days
            if dte > warning_days:
                continue
            action: Literal["MONITOR", "CLOSE", "EXPIRED"]
            if dte < 0:
                action = "EXPIRED"
            elif dte <= close_days:
                action = "CLOSE"
            else:
                action = "MONITOR"
            output.append(
                ExpiringOptionPosition(
                    account=account,
                    instrument=instrument,
                    quantity=position.quantity,
                    avg_cost=position.avg_cost,
                    days_to_expiry=dte,
                    action=action,
                )
            )
        return output

    def request_exercise_or_lapse(
        self,
        *,
        account: str,
        instrument: InstrumentRef,
        quantity: Decimal,
        action: Literal["EXERCISE", "LAPSE"],
        confirmation: str,
        override: bool = False,
    ) -> int:
        if instrument.asset_type != AssetType.OPTION or not instrument.conid:
            raise ValueError("exercise/lapse requires a qualified option conid")
        expected = f"{action}:{account}:{instrument.conid}:{quantity}"
        if confirmation != expected:
            raise PermissionError("option lifecycle confirmation does not match request")
        if self.execution is None:
            raise PermissionError("option actions require the ExecutionSDK capability boundary")
        return self.execution._broker_exercise_option(
            instrument=instrument,
            action=action,
            quantity=quantity,
            account=account,
            override=override,
            confirmation=confirmation,
        )


def _result(row: Any) -> ExecutionResult:
    return ExecutionResult(
        client_order_id=row.client_order_id,
        state=OrderLifecycleState(row.state),
        detail=row.last_error,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
