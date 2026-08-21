from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from platform_core.schemas import (
    BrokerExecution,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPosition,
    MarketQuote,
)

from .contracts import ContractRulesSDK
from .ledger import (
    InvalidOrderTransitionError,
    SQLAlchemyOrderLedger,
    broker_status_matches_request,
    canonical_order_hash,
)
from .metrics import ExecutionMetrics
from .models import (
    TERMINAL_ORDER_STATES,
    AccountRiskSnapshot,
    BracketOrderIntent,
    BrokerEvent,
    BrokerEventType,
    BrokerSessionState,
    ExecutionResult,
    LiveOrderIntent,
    OCAOrderIntentGroup,
    OrderCancelCommand,
    OrderLifecycleState,
    OrderReplaceCommand,
    ReconciliationIssue,
    ReconciliationReport,
    TradingMode,
)
from .pacing import OrderPacingSDK
from .risk import LiveRiskGateway, instrument_risk_key
from .safety import TradingSafetyController


class BrokerTradingPort(Protocol):
    @property
    def session_state(self) -> BrokerSessionState: ...

    def connect(self) -> None: ...

    def resolve_account(self, account: str | None = None) -> str: ...

    def snapshot_quote(self, instrument: Any) -> MarketQuote: ...

    def account_risk_snapshot(self, *, account: str | None = None) -> AccountRiskSnapshot: ...

    def place_order(
        self, request: BrokerOrderRequest, *, order_id: int | None = None
    ) -> BrokerOrderStatus: ...

    def open_orders(self, *, all_clients: bool = False) -> list[BrokerOrderStatus]: ...

    def completed_orders(self, *, api_only: bool = True) -> list[BrokerOrderStatus]: ...

    def executions(
        self,
        *,
        account: str | None = None,
        since: datetime | None = None,
        symbol: str | None = None,
        all_clients: bool = True,
    ) -> list[BrokerExecution]: ...

    def positions(self, *, account: str | None = None) -> list[BrokerPosition]: ...

    def mark_reconciling(self) -> None: ...

    def mark_reconciled(self) -> None: ...

    def mark_degraded(self) -> None: ...

    def mark_killed(self) -> None: ...

    def add_event_handler(self, handler: Any) -> None: ...

    def cancel_all_orders(
        self,
        *,
        account: str | None,
        include_other_clients: bool,
        confirmation: str,
    ) -> list[BrokerOrderStatus]: ...

    def place_bracket(
        self,
        *,
        entry: BrokerOrderRequest,
        take_profit: BrokerOrderRequest,
        stop_loss: BrokerOrderRequest,
    ) -> list[BrokerOrderStatus]: ...

    def place_oca(
        self,
        requests: list[BrokerOrderRequest],
        *,
        oca_group: str,
        oca_type: int = 1,
    ) -> list[BrokerOrderStatus]: ...

    def replace_order(
        self,
        order_id: int,
        request: BrokerOrderRequest,
        *,
        expected_permanent_id: int | None = None,
    ) -> BrokerOrderStatus: ...

    def cancel_order(
        self,
        order_id: int,
        *,
        account: str | None = None,
        permanent_id: int | None = None,
        order_ref: str | None = None,
    ) -> BrokerOrderStatus: ...


class ReconciliationBlockedError(RuntimeError):
    def __init__(self, report: ReconciliationReport) -> None:
        self.report = report
        codes = ", ".join(issue.code for issue in report.issues if issue.blocking)
        super().__init__(f"broker reconciliation blocked trading: {codes}")


class IBKRReconciliationSDK:
    """任何订单离开进程前先重建经纪商事实状态。"""

    def __init__(
        self,
        *,
        broker: BrokerTradingPort,
        ledger: SQLAlchemyOrderLedger,
        allow_unmanaged_open_orders: bool = False,
        require_position_adoption: bool = True,
    ) -> None:
        self.broker = broker
        self.ledger = ledger
        self.allow_unmanaged_open_orders = allow_unmanaged_open_orders
        self.require_position_adoption = require_position_adoption
        self._lock = RLock()

    def run(self, *, account: str, trigger: str = "STARTUP") -> ReconciliationReport:
        with self._lock:
            started_at = datetime.now(UTC)
            self.broker.mark_reconciling()
            issues: list[ReconciliationIssue] = []
            try:
                open_orders = self.broker.open_orders(all_clients=True)
                completed = self.broker.completed_orders(api_only=False)
                executions = self.broker.executions(account=account, all_clients=True)
                positions = self.broker.positions(account=account)
                positions_snapshot_at = datetime.now(UTC)
            except Exception as exc:
                report = ReconciliationReport(
                    account=account,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    open_order_count=0,
                    execution_count=0,
                    position_count=0,
                    issues=[
                        ReconciliationIssue(
                            code="BROKER_SNAPSHOT_FAILED",
                            detail=str(exc),
                            payload={"exception_type": type(exc).__name__},
                        )
                    ],
                )
                self.ledger.record_reconciliation(report, trigger=trigger)
                self.broker.mark_degraded()
                return report

            previous_positions = self.ledger.current_positions(account)
            previous_success_at = self.ledger.latest_successful_reconciliation_at(account)
            matched_local_ids: set[str] = set()
            broker_statuses = _latest_statuses(open_orders + completed)
            for status in broker_statuses:
                if status.account != account:
                    continue
                local = self.ledger.find_for_broker_event(
                    account=account,
                    broker_order_id=status.order_id,
                    broker_client_id=status.client_id,
                    permanent_id=status.permanent_id,
                    order_ref=status.order_ref,
                )
                if local is None:
                    is_open = status in open_orders
                    if is_open:
                        issues.append(
                            ReconciliationIssue(
                                code="UNMANAGED_OPEN_ORDER",
                                detail="open broker order has no durable local intent",
                                blocking=not self.allow_unmanaged_open_orders,
                                broker_order_id=status.order_id,
                                instrument=status.instrument,
                                payload=status.model_dump(mode="json"),
                            )
                        )
                    continue
                matched_local_ids.add(local.client_order_id)
                confirmed_terms_match = broker_status_matches_request(status, local.request_payload)
                pending_terms_match = bool(
                    local.pending_request_payload
                    and broker_status_matches_request(status, local.pending_request_payload)
                )
                if not confirmed_terms_match and not pending_terms_match:
                    issues.append(
                        ReconciliationIssue(
                            code="ORDER_REQUEST_MISMATCH",
                            detail=(
                                "broker order terms match neither the confirmed local "
                                "request nor its pending replacement"
                            ),
                            local_client_order_id=local.client_order_id,
                            broker_order_id=status.order_id,
                            instrument=status.instrument,
                            payload=status.model_dump(mode="json"),
                        )
                    )
                target = lifecycle_from_broker_status(status)
                local_state = OrderLifecycleState(local.state)
                if local_state in TERMINAL_ORDER_STATES and local_state != target:
                    issues.append(
                        ReconciliationIssue(
                            code="TERMINAL_STATE_CONFLICT",
                            detail=(
                                f"local state {local_state.value} conflicts with "
                                f"broker state {target.value}"
                            ),
                            local_client_order_id=local.client_order_id,
                            broker_order_id=status.order_id,
                        )
                    )
                self.ledger.apply_reconciled_status(
                    local.client_order_id,
                    state=target,
                    broker_status=status,
                    settle_pending_replacement=True,
                )

            for execution in executions:
                persisted = self.ledger.upsert_execution(execution)
                if persisted.order_record_id is None:
                    issues.append(
                        ReconciliationIssue(
                            code="UNMANAGED_EXECUTION",
                            detail="broker execution cannot be linked to a durable local order",
                            broker_order_id=execution.order_id,
                            instrument=execution.instrument,
                            payload={"execution_id": execution.execution_id},
                        )
                    )

            for local in self.ledger.nonterminal_orders(account):
                state = OrderLifecycleState(local.state)
                if state in {
                    OrderLifecycleState.INTENT_PERSISTED,
                    OrderLifecycleState.AUTHORIZED,
                }:
                    continue
                if local.client_order_id not in matched_local_ids:
                    refreshed = self.ledger.get(local.client_order_id)
                    if refreshed and OrderLifecycleState(refreshed.state) in TERMINAL_ORDER_STATES:
                        continue
                    issues.append(
                        ReconciliationIssue(
                            code="LOCAL_ORDER_MISSING_AT_BROKER",
                            detail="non-terminal local order is absent from broker snapshots",
                            local_client_order_id=local.client_order_id,
                            broker_order_id=local.broker_order_id,
                        )
                    )

            position_issues: list[ReconciliationIssue] = []
            baseline_exists = bool(previous_positions) or previous_success_at is not None
            if baseline_exists:
                baseline_at = (
                    max(_as_utc(row.captured_at) for row in previous_positions)
                    if previous_positions
                    else previous_success_at
                )
                assert baseline_at is not None
                managed_executions = self.ledger.execution_records(
                    account,
                    period_start=baseline_at,
                    period_end=positions_snapshot_at,
                )
                position_issues = _position_differences(
                    previous_positions,
                    positions,
                    managed_executions=[
                        execution
                        for execution in managed_executions
                        if execution.order_record_id is not None
                    ],
                )
            elif positions and self.require_position_adoption:
                position_issues = [
                    ReconciliationIssue(
                        code="POSITION_BASELINE_REQUIRED",
                        detail=(
                            "broker account has positions but the durable ledger has no "
                            "approved baseline"
                        ),
                        payload={
                            "position_count": len(positions),
                            "conids": sorted(
                                position.instrument.conid
                                for position in positions
                                if position.instrument.conid is not None
                            ),
                        },
                    )
                ]
            issues.extend(position_issues)
            # 不得通过重复对账让差异自动消失；必须由操作员先显式接管经纪商快照。
            if not position_issues:
                self.ledger.replace_positions(account, positions)
            report = ReconciliationReport(
                account=account,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                open_order_count=len(open_orders),
                execution_count=len(executions),
                position_count=len(positions),
                issues=issues,
            )
            self.ledger.record_reconciliation(report, trigger=trigger)
            if report.ok:
                self.broker.mark_reconciled()
            else:
                self.broker.mark_degraded()
            return report


class ExecutionSDK:
    """模拟盘与实盘唯一受支持的订单入口。"""

    def __init__(
        self,
        *,
        broker: BrokerTradingPort,
        ledger: SQLAlchemyOrderLedger,
        risk: LiveRiskGateway,
        safety: TradingSafetyController,
        reconciliation: IBKRReconciliationSDK | None = None,
        metrics: ExecutionMetrics | None = None,
        contract_rules: ContractRulesSDK | None = None,
        instance_id: str | None = None,
        execution_lease_ttl_seconds: int = 45,
        pacing: OrderPacingSDK | None = None,
        require_active_risk_policy_for_live: bool = True,
    ) -> None:
        self.broker = broker
        self.ledger = ledger
        self.risk = risk
        self.safety = safety
        self.reconciliation = reconciliation or IBKRReconciliationSDK(broker=broker, ledger=ledger)
        self._submit_lock = RLock()
        self.instance_id = instance_id or str(uuid4())
        self.execution_lease_ttl_seconds = execution_lease_ttl_seconds
        self._lease_account: str | None = None
        self.pacing = pacing or OrderPacingSDK()
        self.require_active_risk_policy_for_live = require_active_risk_policy_for_live
        self.metrics = metrics or ExecutionMetrics()
        self._supervisor_wakeup: Any | None = None
        self.contract_rules = contract_rules
        if self.contract_rules is None and hasattr(broker, "qualify_contract"):
            self.contract_rules = ContractRulesSDK(broker)
        configure_safety = getattr(self.broker, "configure_safety_controller", None)
        if configure_safety is not None:
            configure_safety(self.safety)
        self._broker_execution_token: object | None = None
        configure_boundary = getattr(self.broker, "configure_execution_boundary", None)
        if configure_boundary is not None:
            self._broker_execution_token = object()
            configure_boundary(self._broker_execution_token)
        self.broker.add_event_handler(self.on_broker_event)

    def register_supervisor_wakeup(self, wakeup: Any) -> None:
        """注册非阻塞回调，用于加速恢复与撤单。"""

        if not callable(wakeup):
            raise TypeError("supervisor wakeup must be callable")
        self._supervisor_wakeup = wakeup

    def start(self, *, account: str | None = None) -> ReconciliationReport:
        configured_account = account or getattr(
            getattr(self.broker, "config", None), "account", None
        )
        if configured_account is not None:
            self._acquire_lease(configured_account)
        self.broker.connect()
        selected = self.broker.resolve_account(account)
        if self._lease_account is None:
            self._acquire_lease(selected)
        elif self._lease_account != selected:
            raise PermissionError("execution lease account differs from managed broker account")
        persistent_kill = self.ledger.kill_switch_reason(f"account:{selected}")
        if persistent_kill:
            self.safety.kill(persistent_kill)
            self.broker.mark_killed()
            cancelled: list[BrokerOrderStatus] = []
            recovery_error: Exception | None = None
            try:
                cancelled = self._broker_cancel_all_orders(
                    account=selected,
                    include_other_clients=False,
                    confirmation=f"CANCEL-OWNED:{selected}",
                )
                self.reconciliation.run(account=selected, trigger="KILL_RECOVERY")
            except Exception as exc:  # noqa: BLE001 - 尽力清理后仍须保留熔断状态。
                recovery_error = exc
            finally:
                self.broker.mark_killed()
            detail = (
                f"persistent kill switch is active: {persistent_kill}; "
                f"cancelled {len(cancelled)} owned orders"
            )
            if recovery_error is not None:
                detail += f"; recovery error: {recovery_error}"
            raise PermissionError(detail)
        report = self.reconciliation.run(account=selected, trigger="STARTUP")
        self.metrics.increment(
            "trading_reconciliations_total", result="ok" if report.ok else "blocked"
        )
        if not report.ok:
            raise ReconciliationBlockedError(report)
        self.refresh_account_snapshot(selected)
        return report

    def submit(self, intent: LiveOrderIntent) -> ExecutionResult:
        with self._submit_lock:
            normalized = self._normalize_intent(intent)
            self.metrics.increment("trading_order_intents_total", kind="single")
            # 必须先持久化幂等意图，再读取行情或调用 IBKR；进程崩溃后才能可靠恢复。
            row, replay = self.ledger.create_or_get_intent(
                normalized,
                expected_broker_client_id=self._expected_broker_client_id(),
            )
            current = OrderLifecycleState(row.state)
            if replay and current not in {
                OrderLifecycleState.INTENT_PERSISTED,
                OrderLifecycleState.AUTHORIZED,
            }:
                return _execution_result(row, idempotent_replay=True)
            killed_liquidation = (
                normalized.request.reduce_only
                and self.broker.session_state == BrokerSessionState.KILLED
            )
            if self.broker.session_state != BrokerSessionState.READY and not killed_liquidation:
                raise PermissionError(
                    f"execution is gated until reconciliation is READY; got "
                    f"{self.broker.session_state.value}"
                )
            account = normalized.request.account
            assert account is not None
            self._require_lease(account)
            if not normalized.request.reduce_only:
                self._require_governed_risk_policy(account)
            self.sync_persistent_control(
                account,
                allow_reduce_only=normalized.request.reduce_only,
            )
            self.safety.assert_can_transmit(
                account=account,
                what_if=normalized.request.what_if,
                reduce_only=normalized.request.reduce_only,
            )
            quote = self.market_quote(normalized.request.instrument)
            snapshot = self.refresh_account_snapshot(account)
            authorization = self.risk.authorize(
                normalized,
                account=snapshot,
                quote=quote,
                require_live_market_data=self.safety.config.mode == TradingMode.LIVE,
            )
            self.ledger.record_risk_decision(authorization)
            if not authorization.approved:
                self.metrics.increment("trading_risk_decisions_total", result="rejected")
                rejected = self.ledger.transition(
                    normalized.client_order_id,
                    OrderLifecycleState.RISK_REJECTED,
                    risk_decision_id=authorization.decision_id,
                    reason=f"{authorization.code}: {authorization.detail}",
                )
                return _execution_result(rejected, idempotent_replay=replay)
            self.metrics.increment("trading_risk_decisions_total", result="approved")
            if current == OrderLifecycleState.INTENT_PERSISTED:
                self.ledger.transition(
                    normalized.client_order_id,
                    OrderLifecycleState.AUTHORIZED,
                    risk_decision_id=authorization.decision_id,
                )
            self.risk.validate_authorization(authorization, normalized)
            self.safety.assert_can_transmit(
                account=account,
                what_if=normalized.request.what_if,
                reduce_only=normalized.request.reduce_only,
            )
            if not normalized.request.reduce_only:
                self.pacing.check_new_orders_allowed()
            # 数据库 CAS 是真正送单前的最后一道门，避免多进程或超时重试产生重复订单。
            submission_attempt = self.ledger.claim_submission(normalized.client_order_id)
            if submission_attempt is None:
                claimed_elsewhere = self.ledger.get(normalized.client_order_id)
                assert claimed_elsewhere is not None
                return _execution_result(claimed_elsewhere, idempotent_replay=True)
            self.pacing.acquire()
            try:
                status = self._broker_place_order(normalized.request)
            except Exception as exc:
                self.metrics.increment("trading_order_submissions_total", result="unknown")
                uncertain = self._mark_unknown_unless_terminal(
                    normalized.client_order_id,
                    reason=f"submission outcome unknown: {type(exc).__name__}: {exc}",
                )
                return ExecutionResult(
                    client_order_id=uncertain.client_order_id,
                    state=OrderLifecycleState(uncertain.state),
                    idempotent_replay=replay,
                    detail=uncertain.last_error,
                )
            target = (
                OrderLifecycleState.VALIDATED
                if normalized.request.what_if
                else lifecycle_from_broker_status(status)
            )
            updated = self.ledger.apply_reconciled_status(
                normalized.client_order_id,
                state=target,
                broker_status=status,
            )
            self.metrics.increment("trading_order_submissions_total", result="acknowledged")
            return ExecutionResult(
                client_order_id=normalized.client_order_id,
                state=OrderLifecycleState(updated.state),
                broker_status=status,
                idempotent_replay=replay,
            )

    def replace(self, command: OrderReplaceCommand) -> ExecutionResult:
        with self._submit_lock:
            row = self.ledger.get(command.client_order_id)
            if row is None:
                raise LookupError(f"unknown client_order_id {command.client_order_id!r}")
            if row.broker_order_id is None:
                raise ValueError("order has no confirmed broker identity")
            if row.parent_order_id or row.oca_group:
                raise ValueError("linked orders must be modified through their group workflow")
            if row.asset_type == "COMBO":
                raise ValueError(
                    "BAG orders are immutable in this SDK; cancel and submit a new combo intent"
                )
            original = BrokerOrderRequest.model_validate(row.request_payload)
            candidate = command.request.model_copy(
                update={
                    "account": row.account,
                    "order_ref": row.client_order_id,
                    "parent_order_id": row.parent_order_id,
                }
            )
            intent = self._normalize_intent(
                LiveOrderIntent(
                    client_order_id=row.client_order_id,
                    strategy_code=row.strategy_code,
                    request=candidate,
                    expires_at=row.expires_at,
                    metadata={"operation": "REPLACE"},
                )
            )
            candidate = intent.request
            immutable_changed = any(
                (
                    candidate.instrument != original.instrument,
                    candidate.side != original.side,
                    candidate.order_type != original.order_type,
                    candidate.transmit != original.transmit,
                    candidate.what_if != original.what_if,
                    candidate.oca_group != original.oca_group,
                    candidate.oca_type != original.oca_type,
                    candidate.reduce_only != original.reduce_only,
                )
            )
            if immutable_changed:
                raise ValueError(
                    "replace may only change quantity, prices, TIF, time, and RTH attributes"
                )
            request_hash = canonical_order_hash(intent)
            if row.pending_request_hash == request_hash:
                return _execution_result(row, idempotent_replay=True)
            if row.revision != command.expected_revision:
                if row.current_request_hash == request_hash:
                    return _execution_result(row, idempotent_replay=True)
                raise ValueError(
                    f"order revision conflict: expected {command.expected_revision}, "
                    f"current {row.revision}"
                )
            account = intent.request.account
            assert account is not None
            self._require_lease(account)
            self._require_governed_risk_policy(account)
            self.sync_persistent_control(account)
            self._assert_ready()
            self.safety.assert_can_transmit(
                account=account,
                what_if=intent.request.what_if,
                reduce_only=intent.request.reduce_only,
            )
            quote = self.market_quote(intent.request.instrument)
            snapshot = self.refresh_account_snapshot(account)
            old_price = row.limit_price or row.stop_price or row.avg_fill_price
            if row.asset_type == "COMBO":
                old_instrument = row.request_payload.get("instrument", {})
                old_metadata = old_instrument.get("metadata", {})
                old_notional = row.remaining * Decimal(str(old_metadata["max_loss_per_unit"]))
            else:
                old_multiplier = Decimal(100) if row.asset_type == "OPTION" else Decimal(1)
                old_notional = row.remaining * abs(old_price) * old_multiplier
            adjusted_snapshot = snapshot.model_copy(
                update={
                    "open_order_notional": max(
                        Decimal(0), snapshot.open_order_notional - old_notional
                    )
                }
            )
            authorization = self.risk.authorize(
                intent,
                account=adjusted_snapshot,
                quote=quote,
                require_live_market_data=self.safety.config.mode == TradingMode.LIVE,
            )
            self.ledger.record_risk_decision(authorization)
            if not authorization.approved:
                return ExecutionResult(
                    client_order_id=row.client_order_id,
                    state=OrderLifecycleState(row.state),
                    detail=f"replacement rejected: {authorization.code}: {authorization.detail}",
                )
            self.risk.validate_authorization(authorization, intent)
            self.pacing.check_new_orders_allowed()
            attempt = self.ledger.claim_replacement(
                row.client_order_id,
                expected_revision=command.expected_revision,
                request=intent.request,
                request_hash=request_hash,
                risk_decision_id=authorization.decision_id,
            )
            if attempt is None:
                refreshed = self.ledger.get(row.client_order_id)
                assert refreshed is not None
                if request_hash in {
                    refreshed.current_request_hash,
                    refreshed.pending_request_hash,
                }:
                    return _execution_result(refreshed, idempotent_replay=True)
                raise ValueError("order changed before replacement claim")
            try:
                self.pacing.acquire()
                status = self._broker_replace_order(
                    row.broker_order_id,
                    intent.request,
                    expected_permanent_id=row.permanent_id,
                )
            except Exception as exc:
                unknown = self._mark_unknown_unless_terminal(
                    row.client_order_id,
                    reason=f"replacement outcome unknown: {type(exc).__name__}: {exc}",
                )
                return _execution_result(unknown, idempotent_replay=False)
            updated = self.ledger.apply_reconciled_status(
                row.client_order_id,
                state=lifecycle_from_broker_status(status),
                broker_status=status,
                settle_pending_replacement=True,
            )
            return ExecutionResult(
                client_order_id=row.client_order_id,
                state=OrderLifecycleState(updated.state),
                broker_status=status,
            )

    def cancel(self, command: OrderCancelCommand) -> ExecutionResult:
        with self._submit_lock:
            row = self.ledger.get(command.client_order_id)
            if row is None:
                raise LookupError(f"unknown client_order_id {command.client_order_id!r}")
            self._require_lease(row.account)
            state = OrderLifecycleState(row.state)
            if state in TERMINAL_ORDER_STATES:
                return _execution_result(row, idempotent_replay=True)
            attempt = self.ledger.claim_cancellation(
                row.client_order_id, expected_revision=command.expected_revision
            )
            if attempt is None:
                refreshed = self.ledger.get(row.client_order_id)
                assert refreshed is not None
                if OrderLifecycleState(refreshed.state) in {
                    OrderLifecycleState.CANCEL_PENDING,
                    OrderLifecycleState.UNKNOWN,
                    *TERMINAL_ORDER_STATES,
                }:
                    return _execution_result(refreshed, idempotent_replay=True)
                raise ValueError(
                    f"order revision conflict: expected {command.expected_revision}, "
                    f"current {refreshed.revision}"
                )
            assert row.broker_order_id is not None
            try:
                self.pacing.acquire()
                status = self._broker_cancel_order(
                    row.broker_order_id,
                    account=row.account,
                    permanent_id=row.permanent_id,
                    order_ref=row.order_ref,
                )
            except Exception as exc:
                unknown = self._mark_unknown_unless_terminal(
                    row.client_order_id,
                    reason=f"cancellation outcome unknown: {type(exc).__name__}: {exc}",
                )
                return _execution_result(unknown, idempotent_replay=False)
            updated = self.ledger.apply_reconciled_status(
                row.client_order_id,
                state=lifecycle_from_broker_status(status),
                broker_status=status,
            )
            return ExecutionResult(
                client_order_id=row.client_order_id,
                state=OrderLifecycleState(updated.state),
                broker_status=status,
            )

    def submit_bracket(self, bracket: BracketOrderIntent) -> list[ExecutionResult]:
        with self._submit_lock:
            intents = [
                self._normalize_intent(bracket.entry),
                self._normalize_intent(bracket.take_profit),
                self._normalize_intent(bracket.stop_loss),
            ]
            rows_and_replays = [
                self.ledger.create_or_get_intent(
                    intent,
                    expected_broker_client_id=self._expected_broker_client_id(),
                )
                for intent in intents
            ]
            if any(
                replay
                and OrderLifecycleState(row.state)
                not in {OrderLifecycleState.INTENT_PERSISTED, OrderLifecycleState.AUTHORIZED}
                for row, replay in rows_and_replays
            ):
                return [
                    _execution_result(row, idempotent_replay=replay)
                    for row, replay in rows_and_replays
                ]
            self._assert_ready()
            account = intents[0].request.account
            assert account is not None
            self._require_lease(account)
            self._require_governed_risk_policy(account)
            self.sync_persistent_control(account)
            if any(intent.request.account != account for intent in intents):
                raise ValueError("bracket legs must use one account")
            for intent in intents:
                self.safety.assert_can_transmit(
                    account=account,
                    what_if=intent.request.what_if,
                    reduce_only=intent.request.reduce_only,
                )
            quote = self.market_quote(intents[0].request.instrument)
            snapshot = self.refresh_account_snapshot(account)
            parent_auth = self.risk.authorize(
                intents[0],
                account=snapshot,
                quote=quote,
                require_live_market_data=self.safety.config.mode == TradingMode.LIVE,
                submission_order_count=3,
                worst_case_fill_count=2,
            )
            authorizations = [
                parent_auth,
                self.risk.authorize_contingent_exit(
                    intents[1], entry=intents[0], entry_authorization=parent_auth, quote=quote
                ),
                self.risk.authorize_contingent_exit(
                    intents[2], entry=intents[0], entry_authorization=parent_auth, quote=quote
                ),
            ]
            for authorization in authorizations:
                self.ledger.record_risk_decision(authorization)
            if not all(authorization.approved for authorization in authorizations):
                return self._reject_group(intents, authorizations, rows_and_replays)
            for intent, authorization, (row, _) in zip(
                intents, authorizations, rows_and_replays, strict=True
            ):
                if OrderLifecycleState(row.state) == OrderLifecycleState.INTENT_PERSISTED:
                    self.ledger.transition(
                        intent.client_order_id,
                        OrderLifecycleState.AUTHORIZED,
                        risk_decision_id=authorization.decision_id,
                    )
                self.risk.validate_authorization(authorization, intent)
            self.pacing.check_new_orders_allowed(messages=3)
            submission_attempt = self.ledger.claim_submission_group(
                [intent.client_order_id for intent in intents]
            )
            if submission_attempt is None:
                return [
                    _execution_result(
                        self.ledger.get(intent.client_order_id), idempotent_replay=True
                    )
                    for intent in intents
                ]
            try:
                self.pacing.acquire(messages=3)
                statuses = self._broker_place_bracket(
                    entry=intents[0].request,
                    take_profit=intents[1].request,
                    stop_loss=intents[2].request,
                )
            except Exception as exc:
                return self._unknown_group(intents, rows_and_replays, exc)
            return self._apply_batch_statuses(intents, statuses, rows_and_replays)

    def submit_oca(self, group: OCAOrderIntentGroup) -> list[ExecutionResult]:
        with self._submit_lock:
            intents = []
            for raw in group.orders:
                normalized = self._normalize_intent(raw)
                request = normalized.request.model_copy(
                    update={"oca_group": group.group_id, "oca_type": group.oca_type}
                )
                intents.append(normalized.model_copy(update={"request": request}))
            rows_and_replays = [
                self.ledger.create_or_get_intent(
                    intent,
                    expected_broker_client_id=self._expected_broker_client_id(),
                )
                for intent in intents
            ]
            if any(
                replay
                and OrderLifecycleState(row.state)
                not in {OrderLifecycleState.INTENT_PERSISTED, OrderLifecycleState.AUTHORIZED}
                for row, replay in rows_and_replays
            ):
                return [
                    _execution_result(row, idempotent_replay=replay)
                    for row, replay in rows_and_replays
                ]
            self._assert_ready()
            account = intents[0].request.account
            assert account is not None
            self._require_lease(account)
            self._require_governed_risk_policy(account)
            self.sync_persistent_control(account)
            snapshot = self.refresh_account_snapshot(account)
            authorizations = []
            working_snapshot = snapshot
            for intent in intents:
                if intent.request.account != account:
                    raise ValueError("OCA orders must use one account")
                self.safety.assert_can_transmit(
                    account=account,
                    what_if=intent.request.what_if,
                    reduce_only=intent.request.reduce_only,
                )
                quote = self.market_quote(intent.request.instrument)
                authorization = self.risk.authorize(
                    intent,
                    account=working_snapshot,
                    quote=quote,
                    require_live_market_data=self.safety.config.mode == TradingMode.LIVE,
                    submission_order_count=len(intents),
                )
                self.ledger.record_risk_decision(authorization)
                authorizations.append(authorization)
                symbol_notional = dict(working_snapshot.symbol_position_notional)
                symbol_notional[intent.request.instrument.symbol] = (
                    authorization.projected_symbol_notional
                )
                instrument_notional = dict(working_snapshot.instrument_position_notional)
                instrument_key = instrument_risk_key(intent.request.instrument)
                signed_notional = (
                    authorization.computed_notional
                    if intent.request.side == "BUY"
                    else -authorization.computed_notional
                )
                instrument_notional[instrument_key] = (
                    instrument_notional.get(instrument_key, Decimal(0)) + signed_notional
                )
                working_snapshot = working_snapshot.model_copy(
                    update={
                        "daily_traded_notional": (
                            working_snapshot.daily_traded_notional + authorization.computed_notional
                        ),
                        "open_order_notional": (
                            working_snapshot.open_order_notional + authorization.computed_notional
                        ),
                        "available_funds": (
                            max(
                                Decimal(0),
                                working_snapshot.available_funds - authorization.computed_notional,
                            )
                            if intent.request.side == "BUY" and not intent.request.reduce_only
                            else working_snapshot.available_funds
                        ),
                        "symbol_position_notional": symbol_notional,
                        "instrument_position_notional": instrument_notional,
                    }
                )
            if not all(authorization.approved for authorization in authorizations):
                return self._reject_group(intents, authorizations, rows_and_replays)
            for intent, authorization, (row, _) in zip(
                intents, authorizations, rows_and_replays, strict=True
            ):
                if OrderLifecycleState(row.state) == OrderLifecycleState.INTENT_PERSISTED:
                    self.ledger.transition(
                        intent.client_order_id,
                        OrderLifecycleState.AUTHORIZED,
                        risk_decision_id=authorization.decision_id,
                    )
                self.risk.validate_authorization(authorization, intent)
            self.pacing.check_new_orders_allowed(messages=len(intents))
            submission_attempt = self.ledger.claim_submission_group(
                [intent.client_order_id for intent in intents]
            )
            if submission_attempt is None:
                return [
                    _execution_result(
                        self.ledger.get(intent.client_order_id), idempotent_replay=True
                    )
                    for intent in intents
                ]
            try:
                self.pacing.acquire(messages=len(intents))
                statuses = self._broker_place_oca(
                    [intent.request for intent in intents],
                    oca_group=group.group_id,
                    oca_type=group.oca_type,
                )
            except Exception as exc:
                return self._unknown_group(intents, rows_and_replays, exc)
            return self._apply_batch_statuses(intents, statuses, rows_and_replays)

    def kill(
        self,
        *,
        account: str,
        reason: str,
        actor: str,
        include_other_clients: bool = False,
    ) -> list[BrokerOrderStatus]:
        self.safety.kill(reason)
        self.metrics.increment("trading_kill_switch_total", action="activate")
        persistence_error: Exception | None = None
        try:
            self.ledger.set_kill_switch(f"account:{account}", reason=reason, changed_by=actor)
        except Exception as exc:  # noqa: BLE001 - 撤单仍具有最高优先级。
            persistence_error = exc
        self.broker.mark_killed()
        confirmation = (
            f"GLOBAL-CANCEL:{account}" if include_other_clients else f"CANCEL-OWNED:{account}"
        )
        cancelled = self._broker_cancel_all_orders(
            account=account,
            include_other_clients=include_other_clients,
            confirmation=confirmation,
        )
        if persistence_error is not None:
            raise RuntimeError(
                f"orders were cancelled but kill-switch persistence failed: {persistence_error}"
            ) from persistence_error
        return cancelled

    def clear_kill(self, *, account: str, actor: str, confirmation: str) -> None:
        self.safety.clear_kill(account=account, confirmation=confirmation)
        self.metrics.increment("trading_kill_switch_total", action="clear")
        self.ledger.clear_kill_switch(f"account:{account}", changed_by=actor)
        # 清除熔断不会直接恢复交易，必须重新完成对账。
        self.broker.mark_degraded()

    def recover(self, *, account: str) -> ReconciliationReport:
        self._require_lease(account)
        self.safety.disarm()
        resubscribe = getattr(self.broker, "resubscribe_streams", None)
        if resubscribe is not None:
            resubscribe()
        report = self.reconciliation.run(account=account, trigger="RECONNECT")
        if not report.ok:
            raise ReconciliationBlockedError(report)
        self.refresh_account_snapshot(account)
        return report

    def audit_broker_state(self, *, account: str) -> ReconciliationReport:
        """订单入口运行期间持续核验经纪商事实状态。"""

        with self._submit_lock:
            self._require_lease(account)
            self.sync_persistent_control(account)
            report = self.reconciliation.run(account=account, trigger="PERIODIC")
            self.metrics.increment(
                "trading_reconciliations_total",
                result="ok" if report.ok else "blocked",
                trigger="periodic",
            )
            if not report.ok:
                codes = ",".join(issue.code for issue in report.issues if issue.blocking)
                reason = f"periodic broker reconciliation blocked: {codes}"
                try:
                    self.kill(
                        account=account,
                        reason=reason,
                        actor="periodic-reconciliation",
                        include_other_clients=False,
                    )
                except Exception as exc:
                    raise ReconciliationBlockedError(report) from exc
                raise ReconciliationBlockedError(report)
            self.refresh_account_snapshot(account)
            return report

    def adopt_positions(
        self,
        *,
        account: str,
        actor: str,
        confirmation: str,
    ) -> ReconciliationReport:
        """显式接管当前经纪商持仓作为对账基线。"""

        with self._submit_lock:
            if not actor.strip():
                raise ValueError("position adoption actor is required")
            if confirmation != f"ADOPT-POSITIONS:{account}":
                raise PermissionError("position adoption confirmation does not match account")
            self._require_lease(account)
            positions = self.broker.positions(account=account)
            self.ledger.replace_positions(account, positions)
            self.ledger.record_event(
                BrokerEvent(
                    event_type=BrokerEventType.POSITION,
                    account=account,
                    payload={
                        "action": "BASELINE_ADOPTED",
                        "actor": actor.strip(),
                        "positions": [position.model_dump(mode="json") for position in positions],
                    },
                )
            )
            report = self.reconciliation.run(account=account, trigger="POSITION_ADOPTION")
            if not report.ok:
                raise ReconciliationBlockedError(report)
            self.refresh_account_snapshot(account)
            return report

    def renew_execution_lease(self) -> bool:
        account = self._lease_account
        if account is None:
            return False
        return self.ledger.renew_execution_lease(
            account=account,
            holder_id=self.instance_id,
            ttl_seconds=self.execution_lease_ttl_seconds,
        )

    def release_execution_lease(self) -> bool:
        account = self._lease_account
        if account is None:
            return False
        released = self.ledger.release_execution_lease(account=account, holder_id=self.instance_id)
        if released:
            self._lease_account = None
        return released

    def readiness(self) -> dict[str, Any]:
        account = self._lease_account
        database_ready = self.ledger.healthcheck()
        lease_ready = bool(
            account
            and database_ready
            and self.ledger.execution_lease_is_owned(
                account=account,
                holder_id=self.instance_id,
            )
        )
        kill_reason = (
            self.ledger.kill_switch_reason(f"account:{account}")
            if account and database_ready
            else None
        )
        snapshot = (
            self.ledger.latest_account_snapshot(account) if account and database_ready else None
        )
        snapshot_age = None
        snapshot_fresh = False
        if snapshot is not None:
            snapshot_age = max(
                0.0,
                (datetime.now(UTC) - _as_utc(snapshot.captured_at)).total_seconds(),
            )
            snapshot_fresh = (
                snapshot_age <= self.risk.policy_for(account).max_account_snapshot_age_seconds
            )
        broker_ready = self.broker.session_state == BrokerSessionState.READY
        risk_policy_ready = True
        if (
            account
            and database_ready
            and self.safety.config.mode == TradingMode.LIVE
            and self.require_active_risk_policy_for_live
        ):
            try:
                self._require_governed_risk_policy(account)
            except Exception:  # noqa: BLE001 - readiness 异常时必须关闭交易。
                risk_policy_ready = False
        ready = all(
            (
                broker_ready,
                database_ready,
                lease_ready,
                snapshot_fresh,
                kill_reason is None,
                risk_policy_ready,
            )
        )
        return {
            "ready": ready,
            "account": account,
            "broker_state": self.broker.session_state.value,
            "database_ready": database_ready,
            "lease_ready": lease_ready,
            "account_snapshot_fresh": snapshot_fresh,
            "account_snapshot_age_seconds": snapshot_age,
            "kill_reason": kill_reason,
            "risk_policy_ready": risk_policy_ready,
        }

    def sync_persistent_control(
        self, account: str, *, allow_reduce_only: bool = False
    ) -> str | None:
        reason = self.ledger.kill_switch_reason(f"account:{account}")
        if reason is None:
            return None
        first_observation = (
            self.safety.killed_reason != reason
            or self.broker.session_state != BrokerSessionState.KILLED
        )
        self.safety.kill(reason)
        self.broker.mark_killed()
        if first_observation:
            self._broker_cancel_all_orders(
                account=account,
                include_other_clients=False,
                confirmation=f"CANCEL-OWNED:{account}",
            )
        if not allow_reduce_only:
            raise PermissionError(f"persistent kill switch is active: {reason}")
        return reason

    def on_broker_event(self, event: BrokerEvent) -> None:
        try:
            self._process_broker_event(event)
        except Exception as exc:  # noqa: BLE001 - 事件丢失时必须关闭交易。
            reason = f"broker event persistence failed: {type(exc).__name__}: {exc}"
            self.metrics.increment("trading_event_pipeline_failures_total")
            self.safety.kill(reason)
            self.broker.mark_degraded()
            account = (
                event.account
                or getattr(self.broker, "config", None)
                and getattr(self.broker.config, "account", None)
            )
            if account:
                try:
                    self.ledger.set_kill_switch(
                        f"account:{account}", reason=reason, changed_by="event-pipeline"
                    )
                except Exception:
                    pass
            self._notify_supervisor()

    def _process_broker_event(self, event: BrokerEvent) -> None:
        if not self.ledger.record_event(event):
            self.metrics.increment("trading_broker_events_total", type="duplicate")
            return
        if event.event_type == BrokerEventType.EXECUTION:
            self.pacing.record_execution()
            self.metrics.increment("trading_broker_events_total", type="execution")
            execution = self.ledger.upsert_execution(BrokerExecution.model_validate(event.payload))
            if execution.order_record_id is None:
                self._activate_integrity_kill(
                    event.account,
                    f"unmanaged broker execution {event.execution_id or event.broker_order_id}",
                )
        elif event.event_type == BrokerEventType.COMMISSION and event.execution_id:
            self.metrics.increment("trading_broker_events_total", type="commission")
            self.ledger.attach_commission(
                event.execution_id,
                commission=_decimal_or_none(event.payload.get("commission")),
                currency=event.payload.get("currency"),
                realized_pnl=_decimal_or_none(event.payload.get("realized_pnl")),
            )
        elif event.event_type in {BrokerEventType.OPEN_ORDER, BrokerEventType.ORDER_STATUS}:
            self.metrics.increment("trading_broker_events_total", type="order")
            self._apply_order_event(event)
        elif event.event_type == BrokerEventType.REJECTION:
            self.metrics.increment("trading_broker_events_total", type="rejection")
            self._apply_rejection_event(event)
        elif event.event_type == BrokerEventType.CONNECTION:
            self.metrics.increment("trading_broker_events_total", type="connection")
            if event.payload.get("state") in {
                "LOST",
                "RESTORED_DATA_LOST",
                "RESTORED",
                "SOCKET_CLOSED",
                "DISCONNECTED",
            }:
                self.safety.disarm()
                self._notify_supervisor()

    def _apply_order_event(self, event: BrokerEvent) -> None:
        local = self.ledger.find_for_broker_event(
            account=event.account,
            broker_order_id=event.broker_order_id,
            broker_client_id=_int_or_none(event.payload.get("client_id")),
            permanent_id=event.permanent_id,
            order_ref=event.client_order_id,
        )
        if local is None:
            if event.event_type == BrokerEventType.OPEN_ORDER:
                self._activate_integrity_kill(
                    event.account,
                    f"unmanaged open broker order {event.broker_order_id}",
                )
            return
        if event.broker_order_id is None:
            return
        payload = dict(event.payload)
        payload["order_id"] = event.broker_order_id
        payload["updated_at"] = event.event_time
        status = BrokerOrderStatus.model_validate(payload)
        self.ledger.apply_reconciled_status(
            local.client_order_id,
            state=lifecycle_from_broker_status(status),
            broker_status=status,
        )

    def _apply_rejection_event(self, event: BrokerEvent) -> None:
        code = _int_or_none(event.payload.get("code"))
        if code not in {201, 202}:
            return
        local = self.ledger.find_for_broker_event(
            account=event.account,
            broker_order_id=event.broker_order_id,
            broker_client_id=_int_or_none(event.payload.get("client_id")),
            permanent_id=event.permanent_id,
            order_ref=event.client_order_id,
        )
        if local is None:
            return
        current = OrderLifecycleState(local.state)
        if current in TERMINAL_ORDER_STATES:
            return
        target: OrderLifecycleState | None = None
        if code == 202:
            target = OrderLifecycleState.CANCELLED
        elif current in {OrderLifecycleState.SUBMITTING, OrderLifecycleState.UNKNOWN}:
            target = OrderLifecycleState.REJECTED
        if target is None:
            return
        message = str(event.payload.get("message") or "order rejected by IBKR")
        try:
            self.ledger.transition(
                local.client_order_id,
                target,
                reason=f"IBKR {code}: {message}",
            )
        except InvalidOrderTransitionError:
            # 并发到达的成交或状态回调比错误回调更具权威性；对账会保留两类事件。
            return

    def _activate_integrity_kill(self, account: str | None, reason: str) -> None:
        """经纪商回调失败时关闭交易，但不阻塞读取线程。"""

        self.metrics.increment("trading_broker_integrity_failures_total")
        self.safety.kill(reason)
        self.broker.mark_degraded()
        selected_account = account or getattr(getattr(self.broker, "config", None), "account", None)
        if selected_account:
            self.ledger.set_kill_switch(
                f"account:{selected_account}",
                reason=reason,
                changed_by="broker-integrity",
            )
        self._notify_supervisor()

    def _notify_supervisor(self) -> None:
        wakeup = self._supervisor_wakeup
        if wakeup is not None:
            wakeup()

    def _expected_broker_client_id(self) -> int | None:
        return _int_or_none(getattr(getattr(self.broker, "config", None), "client_id", None))

    def _broker_place_order(self, request: BrokerOrderRequest) -> BrokerOrderStatus:
        if self._broker_execution_token is None:
            return self.broker.place_order(request)
        return self.broker.place_order(
            request,
            execution_token=self._broker_execution_token,
        )

    def _broker_replace_order(
        self,
        order_id: int,
        request: BrokerOrderRequest,
        *,
        expected_permanent_id: int | None,
    ) -> BrokerOrderStatus:
        kwargs: dict[str, Any] = {"expected_permanent_id": expected_permanent_id}
        if self._broker_execution_token is not None:
            kwargs["execution_token"] = self._broker_execution_token
        return self.broker.replace_order(order_id, request, **kwargs)

    def _broker_place_bracket(
        self,
        *,
        entry: BrokerOrderRequest,
        take_profit: BrokerOrderRequest,
        stop_loss: BrokerOrderRequest,
    ) -> list[BrokerOrderStatus]:
        kwargs: dict[str, Any] = {
            "entry": entry,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
        }
        if self._broker_execution_token is not None:
            kwargs["execution_token"] = self._broker_execution_token
        return self.broker.place_bracket(**kwargs)

    def _broker_place_oca(
        self,
        requests: list[BrokerOrderRequest],
        *,
        oca_group: str,
        oca_type: int,
    ) -> list[BrokerOrderStatus]:
        kwargs: dict[str, Any] = {"oca_group": oca_group, "oca_type": oca_type}
        if self._broker_execution_token is not None:
            kwargs["execution_token"] = self._broker_execution_token
        return self.broker.place_oca(requests, **kwargs)

    def _broker_cancel_order(
        self,
        order_id: int,
        *,
        account: str,
        permanent_id: int | None,
        order_ref: str | None,
    ) -> BrokerOrderStatus:
        kwargs: dict[str, Any] = {
            "account": account,
            "permanent_id": permanent_id,
            "order_ref": order_ref,
        }
        if self._broker_execution_token is not None:
            kwargs["execution_token"] = self._broker_execution_token
        return self.broker.cancel_order(order_id, **kwargs)

    def _broker_cancel_all_orders(
        self,
        *,
        account: str,
        include_other_clients: bool,
        confirmation: str,
    ) -> list[BrokerOrderStatus]:
        kwargs: dict[str, Any] = {
            "account": account,
            "include_other_clients": include_other_clients,
            "confirmation": confirmation,
        }
        if self._broker_execution_token is not None:
            kwargs["execution_token"] = self._broker_execution_token
        return self.broker.cancel_all_orders(**kwargs)

    def _broker_exercise_option(
        self,
        *,
        instrument: Any,
        action: str,
        quantity: Decimal,
        account: str,
        override: bool,
        confirmation: str,
    ) -> int:
        """通过同一私有能力边界路由不可逆的期权操作。"""

        self._require_lease(account)
        self._assert_ready()
        kwargs: dict[str, Any] = {
            "instrument": instrument,
            "action": action,
            "quantity": quantity,
            "account": account,
            "override": override,
            "confirmation": confirmation,
        }
        if self._broker_execution_token is not None:
            kwargs["execution_token"] = self._broker_execution_token
        return self.broker.exercise_option(**kwargs)

    def _mark_unknown_unless_terminal(self, client_order_id: str, *, reason: str) -> Any:
        row = self.ledger.get(client_order_id)
        if row is None:
            raise LookupError(f"unknown client_order_id {client_order_id!r}")
        if OrderLifecycleState(row.state) in TERMINAL_ORDER_STATES:
            return row
        try:
            return self.ledger.transition(
                client_order_id,
                OrderLifecycleState.UNKNOWN,
                reason=reason,
            )
        except InvalidOrderTransitionError:
            refreshed = self.ledger.get(client_order_id)
            if refreshed is None:
                raise
            return refreshed

    def _normalize_intent(self, intent: LiveOrderIntent) -> LiveOrderIntent:
        account = self.broker.resolve_account(intent.request.account)
        if intent.request.order_ref not in {None, intent.client_order_id}:
            raise ValueError("order_ref must be empty or equal to client_order_id")
        request = intent.request.model_copy(
            update={"account": account, "order_ref": intent.client_order_id}
        )
        if request.instrument.asset_type.value == "COMBO":
            from .combos import validate_prepared_combo_intent

            validate_prepared_combo_intent(intent.model_copy(update={"request": request}))
        if self.contract_rules is not None:
            request = self.contract_rules.qualify_and_validate(
                request,
                require_complete=self.safety.config.mode == TradingMode.LIVE,
                require_open_session=(
                    self.safety.config.mode == TradingMode.LIVE
                    and not self.risk.policy_for(account).allow_market_closed_orders
                ),
            )
        elif self.safety.config.mode == TradingMode.LIVE:
            raise RuntimeError("live routing requires ContractRulesSDK")
        normalized = intent.model_copy(update={"request": request})
        if request.instrument.asset_type.value == "COMBO":
            validate_prepared_combo_intent(normalized)
        return normalized

    def _assert_ready(self) -> None:
        if self.broker.session_state != BrokerSessionState.READY:
            raise PermissionError(
                f"execution is gated until reconciliation is READY; got "
                f"{self.broker.session_state.value}"
            )

    def _acquire_lease(self, account: str) -> None:
        acquired = self.ledger.acquire_execution_lease(
            account=account,
            holder_id=self.instance_id,
            ttl_seconds=self.execution_lease_ttl_seconds,
        )
        if not acquired:
            raise PermissionError(
                f"another execution instance holds the active lease for {account}"
            )
        self._lease_account = account

    def _require_lease(self, account: str) -> None:
        if self._lease_account != account or not self.renew_execution_lease():
            self.safety.disarm()
            raise PermissionError("execution lease is missing or expired")

    def _require_governed_risk_policy(self, account: str) -> None:
        if self.safety.config.mode != TradingMode.LIVE:
            return
        if not self.require_active_risk_policy_for_live:
            return
        policy = self.risk.explicit_policy_for(account)
        if policy is None:
            raise PermissionError("live entry requires an approved and active database risk policy")
        unsafe = []
        if policy.live_market_data_types != frozenset({1}):
            unsafe.append("real-time market data type must be exactly {1}")
        configured_market_data_type = getattr(
            getattr(self.broker, "config", None), "market_data_type", None
        )
        if configured_market_data_type != 1:
            unsafe.append("broker market-data configuration must be real-time type 1")
        if policy.max_quote_age_seconds > 30:
            unsafe.append("quote age limit exceeds 30 seconds")
        if policy.max_account_snapshot_age_seconds > 60:
            unsafe.append("account snapshot age limit exceeds 60 seconds")
        if policy.authorization_ttl_seconds > 30:
            unsafe.append("authorization TTL exceeds 30 seconds")
        if policy.allow_naked_short_options:
            unsafe.append("naked short options are unsupported by the live SDK")
        if unsafe:
            raise PermissionError(
                "active risk policy violates the hard live safety envelope: " + "; ".join(unsafe)
            )

    def refresh_account_snapshot(self, account: str) -> AccountRiskSnapshot:
        try:
            snapshot = self.broker.account_risk_snapshot(account=account)
            daily_order_count, daily_traded_notional = self.ledger.daily_activity(
                account,
                now=snapshot.captured_at,
            )
            local_open_order_notional = self.ledger.open_order_risk_notional(account)
            snapshot = snapshot.model_copy(
                update={
                    "daily_order_count": daily_order_count,
                    "daily_traded_notional": daily_traded_notional,
                    "open_order_notional": max(
                        snapshot.open_order_notional, local_open_order_notional
                    ),
                }
            )
            self.ledger.record_account_snapshot(snapshot)
            return snapshot
        except Exception:
            self.broker.mark_degraded()
            self.safety.disarm()
            raise

    def market_quote(self, instrument: Any) -> MarketQuote:
        if self.safety.config.mode == TradingMode.LIVE:
            guarded = getattr(self.broker, "tradeable_quote", None)
            if guarded is None:
                raise RuntimeError(
                    "live routing requires a streaming quote with halt-state validation"
                )
            return guarded(instrument)
        return self.broker.snapshot_quote(instrument)

    def _reject_group(
        self,
        intents: list[LiveOrderIntent],
        authorizations: list[Any],
        rows_and_replays: list[tuple[Any, bool]],
    ) -> list[ExecutionResult]:
        first_rejection = next(auth for auth in authorizations if not auth.approved)
        results = []
        for intent, authorization, (_, replay) in zip(
            intents, authorizations, rows_and_replays, strict=True
        ):
            row = self.ledger.transition(
                intent.client_order_id,
                OrderLifecycleState.RISK_REJECTED,
                risk_decision_id=authorization.decision_id,
                reason=(
                    f"{first_rejection.code}: group rejected because "
                    f"{first_rejection.client_order_id} failed risk"
                ),
            )
            results.append(_execution_result(row, idempotent_replay=replay))
        return results

    def _unknown_group(
        self,
        intents: list[LiveOrderIntent],
        rows_and_replays: list[tuple[Any, bool]],
        exc: Exception,
    ) -> list[ExecutionResult]:
        results = []
        for intent, (_, replay) in zip(intents, rows_and_replays, strict=True):
            row = self._mark_unknown_unless_terminal(
                intent.client_order_id,
                reason=f"batch submission outcome unknown: {type(exc).__name__}: {exc}",
            )
            results.append(_execution_result(row, idempotent_replay=replay))
        return results

    def _apply_batch_statuses(
        self,
        intents: list[LiveOrderIntent],
        statuses: list[BrokerOrderStatus],
        rows_and_replays: list[tuple[Any, bool]],
    ) -> list[ExecutionResult]:
        if len(statuses) != len(intents):
            return self._unknown_group(
                intents,
                rows_and_replays,
                RuntimeError("broker returned an incomplete linked-order acknowledgement"),
            )
        results = []
        for intent, status, (_, replay) in zip(intents, statuses, rows_and_replays, strict=True):
            row = self.ledger.apply_reconciled_status(
                intent.client_order_id,
                state=lifecycle_from_broker_status(status),
                broker_status=status,
            )
            results.append(
                ExecutionResult(
                    client_order_id=intent.client_order_id,
                    state=OrderLifecycleState(row.state),
                    broker_status=status,
                    idempotent_replay=replay,
                )
            )
        return results


def lifecycle_from_broker_status(status: BrokerOrderStatus) -> OrderLifecycleState:
    normalized = status.status.replace(" ", "").upper()
    if status.quantity is not None and status.filled >= status.quantity:
        return OrderLifecycleState.FILLED
    if status.filled > 0 and status.remaining > 0:
        return OrderLifecycleState.PARTIAL_FILL
    if normalized in {"FILLED"}:
        return OrderLifecycleState.FILLED
    if normalized in {"CANCELLED", "APICANCELLED"}:
        return OrderLifecycleState.CANCELLED
    if normalized in {"PENDINGCANCEL"}:
        return OrderLifecycleState.CANCEL_PENDING
    if normalized in {"INACTIVE", "REJECTED"}:
        return OrderLifecycleState.REJECTED
    if normalized in {"PENDINGSUBMIT", "APIPENDING"}:
        return OrderLifecycleState.SUBMITTING
    if normalized in {"PRESUBMITTED", "SUBMITTED"}:
        return OrderLifecycleState.ACKNOWLEDGED
    return OrderLifecycleState.UNKNOWN


def _latest_statuses(statuses: list[BrokerOrderStatus]) -> list[BrokerOrderStatus]:
    latest: dict[tuple[str | None, int | None, int], BrokerOrderStatus] = {}
    for status in statuses:
        key = (status.account, status.client_id, status.order_id)
        current = latest.get(key)
        if current is None or _as_utc(status.updated_at) >= _as_utc(current.updated_at):
            latest[key] = status
    return list(latest.values())


def _position_differences(
    local_positions: list[Any],
    broker_positions: list[BrokerPosition],
    *,
    managed_executions: list[Any] | None = None,
) -> list[ReconciliationIssue]:
    local = {row.position_key: Decimal(str(row.quantity)) for row in local_positions}
    for execution in managed_executions or []:
        for key, delta in _execution_position_deltas(execution):
            local[key] = local.get(key, Decimal(0)) + delta
    broker = {_broker_position_key(position): position.quantity for position in broker_positions}
    issues: list[ReconciliationIssue] = []
    for key in sorted(local.keys() | broker.keys()):
        if local.get(key, Decimal(0)) != broker.get(key, Decimal(0)):
            issues.append(
                ReconciliationIssue(
                    code="POSITION_MISMATCH",
                    detail=f"position {key} differs between ledger and broker",
                    payload={
                        "local_quantity": str(local.get(key, Decimal(0))),
                        "broker_quantity": str(broker.get(key, Decimal(0))),
                    },
                )
            )
    return issues


def _execution_position_deltas(execution: Any) -> list[tuple[str, Decimal]]:
    quantity = Decimal(str(execution.quantity))
    order_sign = Decimal(1) if str(execution.side).upper() in {"BUY", "BOT"} else Decimal(-1)
    raw_instrument = (execution.raw_payload or {}).get("instrument", {})
    metadata = raw_instrument.get("metadata", {}) if isinstance(raw_instrument, dict) else {}
    combo_legs = metadata.get("combo_legs", []) if execution.asset_type == "COMBO" else []
    if combo_legs:
        deltas: list[tuple[str, Decimal]] = []
        for leg in combo_legs:
            conid = _int_or_none(leg.get("conid"))
            ratio = _decimal_or_none(leg.get("ratio"))
            action = str(leg.get("action", "")).upper()
            if conid is None or ratio is None or ratio <= 0 or action not in {"BUY", "SELL"}:
                continue
            leg_sign = Decimal(1) if action == "BUY" else Decimal(-1)
            deltas.append(
                (f"{execution.account}:{conid}", quantity * order_sign * leg_sign * ratio)
            )
        if deltas:
            return deltas
    identity = execution.conid or ":".join(
        str(value or "")
        for value in (
            execution.asset_type,
            execution.symbol,
            execution.currency,
            execution.venue,
            execution.expiry,
            execution.option_right,
            execution.strike,
        )
    )
    return [(f"{execution.account}:{identity}", quantity * order_sign)]


def _broker_position_key(position: BrokerPosition) -> str:
    instrument = position.instrument
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
    return f"{position.account}:{identity}"


def _execution_result(row: Any, *, idempotent_replay: bool) -> ExecutionResult:
    return ExecutionResult(
        client_order_id=row.client_order_id,
        state=OrderLifecycleState(row.state),
        idempotent_replay=idempotent_replay,
        detail=row.last_error,
    )


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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
