from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Bar1mEquity(Base):
    __tablename__ = "bars1m_equity"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[int] = mapped_column(nullable=False, default=0)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)


class Bar1mOption(Base):
    __tablename__ = "bars1m_option"

    conid: Mapped[int] = mapped_column(primary_key=True)
    ts_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    underlying_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    right: Mapped[str] = mapped_column(String(8), nullable=False)
    strike: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    mid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    last: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    volume: Mapped[int | None] = mapped_column(nullable=True)
    open_interest: Mapped[int | None] = mapped_column(nullable=True)


class OptionChainMeta(Base):
    __tablename__ = "option_chain_meta"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    conid: Mapped[int] = mapped_column(primary_key=True)
    underlying_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    right: Mapped[str] = mapped_column(String(8), nullable=False)
    strike: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    dte: Mapped[int | None] = mapped_column(nullable=True)
    delta: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    mid: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    open_interest: Mapped[int | None] = mapped_column(nullable=True)
    volume: Mapped[int | None] = mapped_column(nullable=True)


class StockUniverse(Base):
    __tablename__ = "stock_universe"

    universe_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False, default="EQUITY")
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)


class DimTradingCalendar(Base):
    __tablename__ = "dim_trading_calendar"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(nullable=False, default=True)
    session_open: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_close: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IngestionProgress(Base):
    __tablename__ = "ingestion_progress"

    task_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DataQualityReport(Base):
    __tablename__ = "data_quality_reports"

    report_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(String(128), nullable=False)
    check_name: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_rows: Mapped[int | None] = mapped_column(nullable=True)
    actual_rows: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    runtime_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_key: Mapped[str] = mapped_column(String(192), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    conid: Mapped[int | None] = mapped_column(nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_right: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal(1))
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    tif: Mapped[str] = mapped_column(String(16), nullable=False, default="DAY")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Fill(Base):
    __tablename__ = "fills"

    fill_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.order_id"), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    instrument_key: Mapped[str] = mapped_column(String(192), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    conid: Mapped[int | None] = mapped_column(nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal(0))


class Position(Base):
    __tablename__ = "positions"

    strategy_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_key: Mapped[str] = mapped_column(String(192), primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    conid: Mapped[int | None] = mapped_column(nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_right: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal(1))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskLimit(Base):
    __tablename__ = "risk_limits"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="global")


class RiskEvent(Base):
    __tablename__ = "risk_events"

    event_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event_code: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BacktestRun(Base):
    __tablename__ = "bt_runs"

    run_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calibration_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    parameters: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BacktestOrderEvent(Base):
    __tablename__ = "bt_order_events"

    event_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("bt_runs.run_id"), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BacktestMetricTotal(Base):
    __tablename__ = "bt_metrics_total"

    metric_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("bt_runs.run_id"), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CalibrationRun(Base):
    __tablename__ = "calibration_runs"

    calibration_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False)
    calibration_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETED")
    train_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    train_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    validation_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    validation_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)


class CalibrationParam(Base):
    __tablename__ = "calibration_params"

    param_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    calibration_id: Mapped[int] = mapped_column(ForeignKey("calibration_runs.calibration_id"))
    param_name: Mapped[str] = mapped_column(String(128), nullable=False)
    param_value: Mapped[dict] = mapped_column(JSON, nullable=False)


class CalibrationMetric(Base):
    __tablename__ = "calibration_metrics"

    metric_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    calibration_id: Mapped[int] = mapped_column(ForeignKey("calibration_runs.calibration_id"))
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
