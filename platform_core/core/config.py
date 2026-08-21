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
    ib_client_id: int = 11
    ib_paper_port: int = 7497
    ib_live_port: int = 7496
    ib_paper_account: str = ""
    ib_live_account: str = ""
    ib_market_data_type: int = 4
    ib_request_timeout_seconds: int = 30
    ib_pacing_sleep_seconds: float = 0.25

    default_calibration_version: str = "local-dev"
    smoke_symbols: str = "SPY"
    smoke_days: int = 1
    option_dte_min: int = 7
    option_dte_max: int = 45
    risk_notional_cap: Decimal = Decimal(100000)
    risk_daily_loss_limit: Decimal = Decimal(1000)
    risk_gross_exposure_cap: Decimal = Decimal(250000)
    option_spread_pct_max: Decimal = Decimal("0.10")
    max_quote_age_seconds: int = 30
    order_ttl_seconds: int = 90
    allow_live_trading: bool = False
    live_trading_confirmation: str = ""


def get_settings() -> Settings:
    return Settings()
