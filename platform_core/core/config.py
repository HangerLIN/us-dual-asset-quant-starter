from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    database_url: str = "sqlite:///./starter.db"
    redis_url: str = "redis://localhost:6379/0"

    ib_host: str = "127.0.0.1"
    ib_port: int = 7497
    ib_exec_client_id: int = 11
    ib_market_data_client_id: int = 12
    ib_pnl_client_id: int = 13
    ib_account: str = "DU0000000"
    # 实时行情不可用时必须关闭交易；实盘路由不得静默降级到延迟或冻结行情。
    ib_market_data_type: int = 1
    ib_request_timeout_seconds: int = 30
    ib_pacing_sleep_seconds: float = 0.25
    ib_heartbeat_interval_seconds: int = 15
    ib_reconciliation_interval_seconds: int = 60
    ib_min_server_version: int = 150
    ib_order_messages_per_second: int = 20
    ib_max_order_efficiency_ratio: float = 20.0
    ib_flex_token: str = ""
    ib_flex_query_id: str = ""
    ib_flex_timeout_seconds: int = 30
    ib_flex_max_poll_attempts: int = 30

    default_strategy_code: str = "dual-asset-momentum"
    default_calibration_version: str = "local-dev"
    smoke_symbols: str = "SPY"
    smoke_days: int = 1
    option_dte_min: int = 7
    option_dte_max: int = 45
    risk_notional_cap: Decimal = Decimal("100000")
    risk_daily_loss_limit: Decimal = Decimal("1000")
    option_spread_pct_max: Decimal = Decimal("0.10")
    order_ttl_seconds: int = 90
    trading_mode: str = "READ_ONLY"
    trading_allowed_accounts: str = ""
    live_trading_enabled: bool = False
    live_arm_ttl_seconds: int = 300
    service_api_keys: str = ""
    service_api_identities: str = ""
    risk_symbol_notional_cap: Decimal = Decimal("150000")
    risk_gross_notional_cap: Decimal = Decimal("500000")
    risk_max_daily_order_count: int = 100
    risk_max_daily_traded_notional: Decimal = Decimal("250000")
    risk_account_snapshot_max_age_seconds: int = 10
    risk_account_snapshot_refresh_seconds: int = 5
    risk_quote_max_age_seconds: int = 5
    risk_price_deviation_pct_max: Decimal = Decimal("0.03")
    risk_allow_outside_rth: bool = False
    risk_allow_market_closed_orders: bool = False
    risk_allow_opening_equity_shorts: bool = False
    risk_allow_naked_short_options: bool = False
    risk_require_halt_status_for_live: bool = True
    risk_minimum_shortable_rating: Decimal = Decimal("2.5")
    risk_require_active_policy_for_live: bool = True


def get_settings() -> Settings:
    return Settings()
