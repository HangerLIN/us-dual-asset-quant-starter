from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from sqlalchemy.orm import sessionmaker

from platform_core.core import Settings, get_settings
from platform_core.db import Base, get_engine
from platform_core.infra.ibkr import IBKRAdapter, IBKRAdapterConfig

from .execution import ExecutionSDK, IBKRReconciliationSDK
from .ledger import SQLAlchemyOrderLedger
from .metrics import ExecutionMetrics
from .models import TradingMode
from .risk import LiveRiskGateway, LiveRiskPolicy
from .safety import TradingSafetyConfig, TradingSafetyController
from .session import SessionSupervisorSDK
from .pacing import OrderPacingSDK
from .combos import DefinedRiskComboSDK
from .risk_control import RiskLimitControlSDK
from .statement import EndOfDayReconciliationSDK
from .flex import IBKRFlexConfig, IBKRFlexStatementProvider


@dataclass(slots=True)
class TradingRuntime:
    broker: IBKRAdapter
    ledger: SQLAlchemyOrderLedger
    safety: TradingSafetyController
    risk: LiveRiskGateway
    risk_controls: RiskLimitControlSDK
    reconciliation: IBKRReconciliationSDK
    execution: ExecutionSDK
    combos: DefinedRiskComboSDK
    end_of_day: EndOfDayReconciliationSDK
    statement_provider: IBKRFlexStatementProvider | None
    metrics: ExecutionMetrics
    session_supervisor: SessionSupervisorSDK


@dataclass(slots=True)
class RiskControlRuntime:
    """仅访问数据库的风控运行时，有意不建立经纪商连接。"""

    ledger: SQLAlchemyOrderLedger
    risk: LiveRiskGateway
    risk_controls: RiskLimitControlSDK
    metrics: ExecutionMetrics


def get_execution_service_runtime() -> TradingRuntime:
    """返回由 exec_svc 独占且唯一具备订单能力的运行时。"""

    return _get_trading_runtime("exec")


def get_read_only_runtime(role: str) -> TradingRuntime:
    """返回安全模式强制为 READ_ONLY 的 IBKR 数据运行时。"""

    if role not in {"md", "pnl"}:
        raise ValueError("read-only runtime role must be 'md' or 'pnl'")
    return _get_trading_runtime(role)


@lru_cache(maxsize=3)
def _get_trading_runtime(role: str) -> TradingRuntime:
    settings = get_settings()
    client_ids = {
        "exec": settings.ib_exec_client_id,
        "md": settings.ib_market_data_client_id,
        "pnl": settings.ib_pnl_client_id,
    }
    if role not in client_ids:
        raise ValueError(f"unknown IBKR runtime role {role!r}")
    engine = get_engine(settings.database_url)
    if settings.app_env.lower() in {"local", "test", "development"}:
        Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    ledger = SQLAlchemyOrderLedger(session_factory)
    configured_mode = TradingMode(settings.trading_mode.upper())
    # 行情和 PnL 连接即使部署环境设为 LIVE，也必须在代码层降级为只读。
    mode = configured_mode if role == "exec" else TradingMode.READ_ONLY
    if configured_mode == TradingMode.LIVE and settings.ib_market_data_type != 1:
        raise ValueError("LIVE mode requires IB_MARKET_DATA_TYPE=1")
    allowed_accounts = frozenset(
        account.strip()
        for account in settings.trading_allowed_accounts.split(",")
        if account.strip()
    )
    safety = TradingSafetyController(
        TradingSafetyConfig(
            mode=mode,
            allowed_accounts=allowed_accounts,
            live_enabled=settings.live_trading_enabled if role == "exec" else False,
            live_arm_ttl_seconds=settings.live_arm_ttl_seconds,
        )
    )
    broker = IBKRAdapter(
        IBKRAdapterConfig(
            host=settings.ib_host,
            port=settings.ib_port,
            client_id=client_ids[role],
            account=(settings.ib_account if settings.ib_account != "DU0000000" else None),
            market_data_type=settings.ib_market_data_type,
            request_timeout_seconds=settings.ib_request_timeout_seconds,
            pacing_sleep_seconds=settings.ib_pacing_sleep_seconds,
            minimum_server_version=settings.ib_min_server_version,
        ),
        safety=safety,
    )
    policy = _policy_from_settings(settings)
    risk_controls = RiskLimitControlSDK(session_factory)
    risk = LiveRiskGateway(policy, policy_resolver=risk_controls.resolve)
    reconciliation = IBKRReconciliationSDK(broker=broker, ledger=ledger)
    metrics = ExecutionMetrics()
    execution = ExecutionSDK(
        broker=broker,
        ledger=ledger,
        risk=risk,
        safety=safety,
        reconciliation=reconciliation,
        metrics=metrics,
        pacing=OrderPacingSDK(
            max_messages_per_second=settings.ib_order_messages_per_second,
            max_order_efficiency_ratio=settings.ib_max_order_efficiency_ratio,
        ),
        require_active_risk_policy_for_live=(settings.risk_require_active_policy_for_live),
    )
    session_supervisor = SessionSupervisorSDK(
        execution=execution,
        heartbeat_interval_seconds=settings.ib_heartbeat_interval_seconds,
        account_snapshot_refresh_seconds=settings.risk_account_snapshot_refresh_seconds,
        reconciliation_interval_seconds=settings.ib_reconciliation_interval_seconds,
        metrics=metrics,
    )
    combos = DefinedRiskComboSDK(execution)
    end_of_day = EndOfDayReconciliationSDK(ledger)
    flex_values = (settings.ib_flex_token.strip(), settings.ib_flex_query_id.strip())
    if any(flex_values) and not all(flex_values):
        raise ValueError("IB_FLEX_TOKEN and IB_FLEX_QUERY_ID must be configured together")
    statement_provider = (
        IBKRFlexStatementProvider(
            IBKRFlexConfig(
                token=flex_values[0],
                query_id=flex_values[1],
                timeout_seconds=settings.ib_flex_timeout_seconds,
                max_poll_attempts=settings.ib_flex_max_poll_attempts,
            )
        )
        if all(flex_values)
        else None
    )
    return TradingRuntime(
        broker=broker,
        ledger=ledger,
        safety=safety,
        risk=risk,
        risk_controls=risk_controls,
        reconciliation=reconciliation,
        execution=execution,
        combos=combos,
        end_of_day=end_of_day,
        statement_provider=statement_provider,
        metrics=metrics,
        session_supervisor=session_supervisor,
    )


@lru_cache(maxsize=1)
def get_risk_control_runtime() -> RiskControlRuntime:
    settings = get_settings()
    engine = get_engine(settings.database_url)
    if settings.app_env.lower() in {"local", "test", "development"}:
        Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    ledger = SQLAlchemyOrderLedger(session_factory)
    risk_controls = RiskLimitControlSDK(session_factory)
    return RiskControlRuntime(
        ledger=ledger,
        risk=LiveRiskGateway(
            _policy_from_settings(settings),
            policy_resolver=risk_controls.resolve,
        ),
        risk_controls=risk_controls,
        metrics=ExecutionMetrics(),
    )


def _policy_from_settings(settings: Settings) -> LiveRiskPolicy:
    return LiveRiskPolicy(
        max_order_notional=settings.risk_notional_cap,
        max_symbol_notional=settings.risk_symbol_notional_cap,
        max_gross_notional=settings.risk_gross_notional_cap,
        daily_loss_limit=settings.risk_daily_loss_limit,
        max_daily_order_count=settings.risk_max_daily_order_count,
        max_daily_traded_notional=settings.risk_max_daily_traded_notional,
        max_account_snapshot_age_seconds=settings.risk_account_snapshot_max_age_seconds,
        max_quote_age_seconds=settings.risk_quote_max_age_seconds,
        max_price_deviation_pct=settings.risk_price_deviation_pct_max,
        max_option_spread_pct=settings.option_spread_pct_max,
        allow_outside_rth=settings.risk_allow_outside_rth,
        allow_market_closed_orders=settings.risk_allow_market_closed_orders,
        allow_opening_equity_shorts=settings.risk_allow_opening_equity_shorts,
        allow_naked_short_options=settings.risk_allow_naked_short_options,
        require_halt_status_for_live=settings.risk_require_halt_status_for_live,
        minimum_shortable_rating=settings.risk_minimum_shortable_rating,
    )
