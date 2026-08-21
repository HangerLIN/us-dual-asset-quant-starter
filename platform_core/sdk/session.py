from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from threading import Event, RLock, Thread

from .execution import ExecutionSDK
from .metrics import ExecutionMetrics
from .models import BrokerSessionState, ReconciliationReport


class SessionSupervisorSDK:
    """心跳、重连、重新订阅和对账状态机。"""

    def __init__(
        self,
        *,
        execution: ExecutionSDK,
        heartbeat_interval_seconds: int = 15,
        account_snapshot_refresh_seconds: int = 5,
        reconciliation_interval_seconds: int = 60,
        metrics: ExecutionMetrics | None = None,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if account_snapshot_refresh_seconds <= 0:
            raise ValueError("account snapshot refresh interval must be positive")
        if reconciliation_interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self.execution = execution
        self.broker = execution.broker
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.account_snapshot_refresh_seconds = account_snapshot_refresh_seconds
        self.reconciliation_interval_seconds = reconciliation_interval_seconds
        self.metrics = metrics or execution.metrics
        self._lock = RLock()
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._account: str | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_account_snapshot_at: datetime | None = None
        self.last_reconciliation_at: datetime | None = None
        self.last_error: str | None = None
        self.execution.register_supervisor_wakeup(self.wake)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, *, account: str) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._account = account
            self._stop.clear()
            self._wake.clear()
            self._thread = Thread(
                target=self._run,
                name="ibkr-session-supervisor",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
            self._wake.set()
        if thread is not None:
            thread.join(timeout_seconds)
        with self._lock:
            self._thread = None

    def wake(self) -> None:
        """经纪商完整性或连接事件发生后唤醒单写者循环。"""

        self._wake.set()

    def check_once(self, *, account: str | None = None) -> ReconciliationReport | None:
        selected = account or self._account
        if not selected:
            raise ValueError("session supervisor account is required")
        if not self.execution.renew_execution_lease():
            self.execution.safety.disarm()
            raise PermissionError("execution lease renewal failed")
        try:
            self.execution.sync_persistent_control(selected)
        except PermissionError:
            self._heartbeat(account=selected)
            return None
        state = self.broker.session_state
        if state == BrokerSessionState.KILLED:
            self._heartbeat(account=selected)
            return None
        if state == BrokerSessionState.DISCONNECTED:
            self.execution.safety.disarm()
            self.broker.connect()
            state = self.broker.session_state
        if state in {
            BrokerSessionState.RECOVERING,
            BrokerSessionState.RECONCILING,
            BrokerSessionState.DEGRADED,
        }:
            self.execution.safety.disarm()
            report = self.execution.recover(account=selected)
            self.last_reconciliation_at = report.completed_at
            self.metrics.increment(
                "trading_session_recoveries_total", result="ok" if report.ok else "blocked"
            )
            return report
        if state != BrokerSessionState.READY:
            raise ConnectionError(f"IBKR session cannot be supervised from {state.value}")
        self._heartbeat(account=selected)
        # TTL 检查属于同一个单写者循环，避免订单仅因无人调用接口而在过期后继续存活。
        from .lifecycle import OrderSupervisorSDK

        expired = OrderSupervisorSDK(
            execution=self.execution,
            ledger=self.execution.ledger,
            safety=self.execution.safety,
        ).expire_due_orders(account=selected)
        for result in expired:
            self.metrics.increment(
                "trading_order_ttl_actions_total", result=result.state.value.lower()
            )
        if self.last_reconciliation_at is None:
            self.last_reconciliation_at = (
                self.execution.ledger.latest_successful_reconciliation_at(selected)
            )
        now = datetime.now(UTC)
        if (
            self.last_reconciliation_at is None
            or now - self.last_reconciliation_at
            >= timedelta(seconds=self.reconciliation_interval_seconds)
        ):
            report = self.execution.audit_broker_state(account=selected)
            self.last_reconciliation_at = report.completed_at
            return report
        return None

    def _heartbeat(self, *, account: str) -> None:
        heartbeat = getattr(self.broker, "heartbeat", None)
        if heartbeat is None:
            raise RuntimeError("broker does not implement heartbeat")
        heartbeat()
        now = datetime.now(UTC)
        self.last_heartbeat_at = now
        if (
            self.last_account_snapshot_at is None
            or now - self.last_account_snapshot_at
            >= timedelta(seconds=self.account_snapshot_refresh_seconds)
        ):
            snapshot = self.execution.refresh_account_snapshot(account)
            self.last_account_snapshot_at = snapshot.captured_at
        self.last_error = None
        self.metrics.increment("trading_broker_heartbeats_total", result="ok")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001 - 监督器必须保持运行并关闭交易。
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.execution.safety.disarm()
                if self.broker.session_state != BrokerSessionState.KILLED:
                    with suppress(Exception):
                        self.broker.mark_degraded()
                self.metrics.increment("trading_broker_heartbeats_total", result="failed")
            self._wake.wait(self.heartbeat_interval_seconds)
