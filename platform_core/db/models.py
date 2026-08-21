from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
    runtime_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="BACKTEST")
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, default="SIMULATED")
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_key: Mapped[str] = mapped_column(String(192), nullable=False, default="")
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    conid: Mapped[int | None] = mapped_column(nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_right: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    multiplier: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal(1)
    )
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
    fees: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))


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
    multiplier: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal(1)
    )
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


class BrokerOrderRecord(Base):
    """所有待发送经纪商订单意图的持久化事实来源。"""

    __tablename__ = "broker_orders"
    __table_args__ = (
        UniqueConstraint(
            "account",
            "broker_client_id",
            "broker_order_id",
            name="uq_broker_order_identity",
        ),
        Index("ix_broker_orders_perm_id", "permanent_id"),
        Index("ix_broker_orders_state", "account", "state"),
    )

    order_record_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    conid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    venue: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_right: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    tif: Mapped[str] = mapped_column(String(8), nullable=False)
    order_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    transmit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    what_if: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    outside_rth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    good_after_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    good_till_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    oca_group: Mapped[str | None] = mapped_column(String(64), nullable=True)
    oca_type: Mapped[int | None] = mapped_column(nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    pending_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_request_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    broker_order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    broker_client_id: Mapped[int | None] = mapped_column(nullable=True)
    permanent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    parent_order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    risk_decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submission_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submission_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    filled: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    remaining: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    avg_fill_price: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )
    revision: Mapped[int] = mapped_column(nullable=False, default=1)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BrokerOrderEventRecord(Base):
    """只追加的经纪商事件日志；``dedupe_key`` 保证回调重放安全。"""

    __tablename__ = "broker_order_events"
    __table_args__ = (
        Index("ix_broker_events_order_time", "order_record_id", "event_time"),
        Index("ix_broker_events_exec_id", "execution_id"),
    )

    event_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    order_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_orders.order_record_id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    broker_order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    permanent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StrategyCheckpointRecord(Base):
    """一个带版本策略运行实例的最新持久化状态。"""

    __tablename__ = "strategy_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "strategy_code",
            "strategy_version",
            "runtime_id",
            name="uq_strategy_checkpoint_identity",
        ),
        Index("ix_strategy_checkpoint_heartbeat", "status", "heartbeat_at"),
    )

    checkpoint_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy_code: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    event_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_market_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrokerExecutionRecord(Base):
    __tablename__ = "broker_executions"
    __table_args__ = (
        Index("ix_broker_executions_order", "order_record_id"),
        Index("ix_broker_executions_account_time", "account", "executed_at"),
    )

    execution_record_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    execution_root_id: Mapped[str] = mapped_column(String(128), nullable=False)
    is_correction: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    order_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_orders.order_record_id"), nullable=True
    )
    broker_order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    permanent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    broker_client_id: Mapped[int | None] = mapped_column(nullable=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    order_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    conid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    venue: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_right: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    commission: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    commission_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrokerPositionRecord(Base):
    __tablename__ = "broker_positions"
    __table_args__ = (Index("ix_broker_positions_account", "account"),)

    position_key: Mapped[str] = mapped_column(String(192), primary_key=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    conid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    venue: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    option_right: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrokerAccountSnapshotRecord(Base):
    __tablename__ = "broker_account_snapshots"
    __table_args__ = (Index("ix_account_snapshots_account_time", "account", "captured_at"),)

    snapshot_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    net_liquidation: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    available_funds: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    buying_power: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    maintenance_margin: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    daily_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    gross_position_notional: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    open_order_notional: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    daily_order_count: Mapped[int] = mapped_column(nullable=False, default=0)
    daily_traded_notional: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, default=Decimal("0")
    )
    market_data_type: Mapped[int | None] = mapped_column(nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class RiskDecisionRecord(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (Index("ix_risk_decisions_order", "client_order_id", "decided_at"),)

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    order_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_notional: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    projected_symbol_notional: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    reasons: Mapped[dict] = mapped_column(JSON, nullable=False)


class RiskPolicyVersionRecord(Base):
    __tablename__ = "risk_policy_versions"
    __table_args__ = (
        UniqueConstraint("scope", "version", name="uq_risk_policy_scope_version"),
        Index("ix_risk_policy_active", "scope", "status"),
        Index(
            "uq_risk_policy_one_active_scope",
            "scope",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    proposed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    activated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconciliationRunRecord(Base):
    __tablename__ = "broker_reconciliation_runs"

    reconciliation_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    open_order_count: Mapped[int] = mapped_column(nullable=False, default=0)
    execution_count: Mapped[int] = mapped_column(nullable=False, default=0)
    position_count: Mapped[int] = mapped_column(nullable=False, default=0)
    issues: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class StatementReconciliationRecord(Base):
    __tablename__ = "statement_reconciliation_runs"
    __table_args__ = (
        Index("ix_statement_reconciliation_account_time", "account", "period_end"),
    )

    statement_reconciliation_id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    account: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    issues: Mapped[list] = mapped_column(JSON, nullable=False)
    statement_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    reconciled_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reconciled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradingControlRecord(Base):
    __tablename__ = "trading_controls"

    scope: Mapped[str] = mapped_column(String(128), primary_key=True)
    killed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionLeaseRecord(Base):
    __tablename__ = "execution_leases"

    account: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder_id: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
