from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock

from .models import TradingMode


class TradingSafetyError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class TradingSafetyConfig:
    mode: TradingMode = TradingMode.READ_ONLY
    allowed_accounts: frozenset[str] = frozenset()
    live_enabled: bool = False
    live_arm_ttl_seconds: int = 300


class TradingSafetyController:
    """关闭优先的交易模式与紧急停止控制器。

    实盘模式同时要求静态配置和短时运行授权；模拟盘只接受 DU 前缀账户。What-if 请求
    不会发送到交易场所，因此在模拟盘或实盘模式下无需 ARM 即可执行。
    """

    def __init__(self, config: TradingSafetyConfig) -> None:
        self.config = config
        self._lock = RLock()
        self._armed_until: datetime | None = None
        self._liquidation_armed_until: datetime | None = None
        self._liquidation_account: str | None = None
        self._killed_reason: str | None = None

    @property
    def killed_reason(self) -> str | None:
        with self._lock:
            return self._killed_reason

    def arm_live(self, *, account: str, confirmation: str) -> datetime:
        with self._lock:
            if self.config.mode != TradingMode.LIVE or not self.config.live_enabled:
                raise TradingSafetyError("live trading is not statically enabled")
            self._assert_allowed_account(account)
            expected = f"ARM-LIVE:{account}"
            if confirmation != expected:
                raise TradingSafetyError("live arm confirmation does not match the account")
            if self._killed_reason is not None:
                raise TradingSafetyError("trading is killed; clear the kill switch before arming")
            self._armed_until = datetime.now(UTC) + timedelta(
                seconds=self.config.live_arm_ttl_seconds
            )
            return self._armed_until

    def disarm(self) -> None:
        with self._lock:
            self._armed_until = None

    def kill(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("kill-switch reason is required")
        with self._lock:
            self._killed_reason = reason.strip()
            self._armed_until = None
            self._liquidation_armed_until = None
            self._liquidation_account = None

    def clear_kill(self, *, account: str, confirmation: str) -> None:
        with self._lock:
            self._assert_allowed_account(account)
            if confirmation != f"CLEAR-KILL-SWITCH:{account}":
                raise TradingSafetyError("invalid kill-switch clear confirmation")
            self._killed_reason = None

    def arm_liquidation(self, *, account: str, confirmation: str) -> datetime:
        with self._lock:
            self._assert_allowed_account(account)
            if confirmation != f"LIQUIDATE:{account}":
                raise TradingSafetyError("invalid emergency-liquidation confirmation")
            if self.config.mode == TradingMode.READ_ONLY:
                raise TradingSafetyError("liquidation is disabled in READ_ONLY mode")
            if self.config.mode == TradingMode.PAPER and not account.upper().startswith("DU"):
                raise TradingSafetyError("PAPER mode only permits DU-prefixed accounts")
            if self.config.mode == TradingMode.LIVE:
                if account.upper().startswith("DU") or not self.config.live_enabled:
                    raise TradingSafetyError("live liquidation is not statically enabled")
            self._liquidation_account = account
            self._liquidation_armed_until = datetime.now(UTC) + timedelta(seconds=60)
            return self._liquidation_armed_until

    def disarm_liquidation(self) -> None:
        with self._lock:
            self._liquidation_account = None
            self._liquidation_armed_until = None

    def assert_can_transmit(
        self,
        *,
        account: str,
        what_if: bool = False,
        reduce_only: bool = False,
    ) -> None:
        with self._lock:
            liquidation_override = (
                reduce_only
                and self._liquidation_account == account
                and self._liquidation_armed_until is not None
                and datetime.now(UTC) < self._liquidation_armed_until
            )
            if self._killed_reason is not None and not liquidation_override:
                raise TradingSafetyError(f"trading is killed: {self._killed_reason}")
            if self.config.mode == TradingMode.READ_ONLY:
                raise TradingSafetyError("trading mode is READ_ONLY")
            self._assert_allowed_account(account)
            if self.config.mode == TradingMode.PAPER:
                if not account.upper().startswith("DU"):
                    raise TradingSafetyError("PAPER mode only permits DU-prefixed accounts")
                return
            if account.upper().startswith("DU"):
                raise TradingSafetyError("LIVE mode does not permit paper accounts")
            if not self.config.live_enabled:
                raise TradingSafetyError("live trading is disabled")
            if what_if:
                return
            if liquidation_override:
                return
            if self._armed_until is None or datetime.now(UTC) >= self._armed_until:
                raise TradingSafetyError("live trading is not armed or the arm has expired")

    def _assert_allowed_account(self, account: str) -> None:
        if not self.config.allowed_accounts or account not in self.config.allowed_accounts:
            raise TradingSafetyError(f"account {account!r} is not in the trading allowlist")
