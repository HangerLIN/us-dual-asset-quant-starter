from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from platform_core.schemas import BrokerOrderRequest, BrokerOrderStatus, InstrumentRef


class TradingMode(str, Enum):
    READ_ONLY = "READ_ONLY"
    PAPER = "PAPER"
    LIVE = "LIVE"


class BrokerSessionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    RECOVERING = "RECOVERING"
    RECONCILING = "RECONCILING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    KILLED = "KILLED"


class OrderLifecycleState(str, Enum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    RISK_REJECTED = "RISK_REJECTED"
    AUTHORIZED = "AUTHORIZED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    REPLACE_PENDING = "REPLACE_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    VALIDATED = "VALIDATED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ORDER_STATES = {
    OrderLifecycleState.RISK_REJECTED,
    OrderLifecycleState.FILLED,
    OrderLifecycleState.CANCELLED,
    OrderLifecycleState.REJECTED,
    OrderLifecycleState.EXPIRED,
    OrderLifecycleState.VALIDATED,
}


class BrokerEventType(str, Enum):
    CONNECTION = "CONNECTION"
    OPEN_ORDER = "OPEN_ORDER"
    ORDER_STATUS = "ORDER_STATUS"
    EXECUTION = "EXECUTION"
    COMMISSION = "COMMISSION"
    REJECTION = "REJECTION"
    POSITION = "POSITION"
    ACCOUNT = "ACCOUNT"
    PNL = "PNL"
    OPTION_LIFECYCLE = "OPTION_LIFECYCLE"


class LiveOrderIntent(BaseModel):
    client_order_id: str = Field(..., min_length=8, max_length=64)
    strategy_code: str = Field(..., min_length=1, max_length=64)
    request: BrokerOrderRequest
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_expiry(self) -> "LiveOrderIntent":
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class BrokerEvent(BaseModel):
    event_type: BrokerEventType
    event_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    account: str | None = None
    client_order_id: str | None = None
    broker_order_id: int | None = None
    permanent_id: int | None = None
    execution_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class StrategyOrderEvent(BaseModel):
    """单个经纪商回调的持久化策略隔离视图。"""

    event_id: int = Field(..., ge=1)
    event_type: BrokerEventType
    event_time: datetime
    received_at: datetime
    client_order_id: str
    broker_order_id: int | None = None
    permanent_id: int | None = None
    execution_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class StrategyOrderEventPage(BaseModel):
    """可恢复订单事件流使用的游标分页。"""

    events: list[StrategyOrderEvent] = Field(default_factory=list)
    next_event_id: int = Field(default=0, ge=0)


class AccountRiskSnapshot(BaseModel):
    account: str
    captured_at: datetime
    net_liquidation: Decimal
    available_funds: Decimal
    buying_power: Decimal
    maintenance_margin: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    gross_position_notional: Decimal = Decimal("0")
    open_order_notional: Decimal = Decimal("0")
    daily_order_count: int = 0
    daily_traded_notional: Decimal = Decimal("0")
    symbol_position_notional: dict[str, Decimal] = Field(default_factory=dict)
    instrument_position_notional: dict[str, Decimal] = Field(default_factory=dict)
    instrument_position_quantity: dict[str, Decimal] = Field(default_factory=dict)
    market_data_type: int | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(UTC)
        return max(0.0, (current - self.captured_at).total_seconds())


class RiskAuthorization(BaseModel):
    decision_id: str
    approved: bool
    code: str
    detail: str
    account: str
    client_order_id: str
    order_hash: str
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    computed_notional: Decimal
    projected_symbol_notional: Decimal
    reasons: dict[str, Any] = Field(default_factory=dict)


class QualifiedContract(BaseModel):
    instrument: InstrumentRef
    primary_exchange: str | None = None
    valid_exchanges: list[str] = Field(default_factory=list)
    supported_order_types: list[str] = Field(default_factory=list)
    min_tick: Decimal
    min_size: Decimal = Decimal("1")
    size_increment: Decimal = Decimal("1")
    market_rule_ids: list[int] = Field(default_factory=list)
    time_zone_id: str | None = None
    trading_hours: str | None = None
    liquid_hours: str | None = None


class ReconciliationIssue(BaseModel):
    code: str
    detail: str
    blocking: bool = True
    local_client_order_id: str | None = None
    broker_order_id: int | None = None
    instrument: InstrumentRef | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ReconciliationReport(BaseModel):
    account: str
    started_at: datetime
    completed_at: datetime
    open_order_count: int
    execution_count: int
    position_count: int
    issues: list[ReconciliationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(issue.blocking for issue in self.issues)


class ExecutionResult(BaseModel):
    client_order_id: str
    state: OrderLifecycleState
    broker_status: BrokerOrderStatus | None = None
    idempotent_replay: bool = False
    detail: str | None = None


class BracketOrderIntent(BaseModel):
    entry: LiveOrderIntent
    take_profit: LiveOrderIntent
    stop_loss: LiveOrderIntent

    @model_validator(mode="after")
    def validate_group(self) -> "BracketOrderIntent":
        requests = [self.entry.request, self.take_profit.request, self.stop_loss.request]
        if len({intent.client_order_id for intent in (self.entry, self.take_profit, self.stop_loss)}) != 3:
            raise ValueError("bracket client_order_id values must be unique")
        if any(request.instrument != requests[0].instrument for request in requests[1:]):
            raise ValueError("bracket legs must target exactly the same contract")
        if self.take_profit.request.side == self.entry.request.side:
            raise ValueError("take-profit side must oppose entry side")
        if self.stop_loss.request.side == self.entry.request.side:
            raise ValueError("stop-loss side must oppose entry side")
        if self.take_profit.request.order_type != "LMT":
            raise ValueError("take-profit leg must be a limit order")
        if self.stop_loss.request.order_type not in {"STP", "STP_LMT"}:
            raise ValueError("stop-loss leg must be STP or STP_LMT")
        if any(request.quantity != requests[0].quantity for request in requests[1:]):
            raise ValueError("bracket child quantities must equal entry quantity")
        return self


class OCAOrderIntentGroup(BaseModel):
    group_id: str = Field(..., min_length=8, max_length=64)
    orders: list[LiveOrderIntent] = Field(..., min_length=2)
    oca_type: int = Field(default=1, ge=1, le=3)

    @model_validator(mode="after")
    def validate_orders(self) -> "OCAOrderIntentGroup":
        ids = [intent.client_order_id for intent in self.orders]
        if len(ids) != len(set(ids)):
            raise ValueError("OCA client_order_id values must be unique")
        accounts = {intent.request.account for intent in self.orders if intent.request.account}
        if len(accounts) > 1:
            raise ValueError("OCA orders must use one account")
        return self


class ComboLegRef(BaseModel):
    instrument: InstrumentRef
    ratio: int = Field(default=1, ge=1, le=100)
    action: Literal["BUY", "SELL"]
    exchange: str = Field(default="SMART", min_length=1, max_length=32)
    open_close: Literal[0, 1, 2, 3] = 0

    @model_validator(mode="after")
    def validate_leg(self) -> "ComboLegRef":
        if self.instrument.asset_type == "COMBO":
            raise ValueError("a combo leg cannot itself be a COMBO")
        if not self.instrument.conid:
            raise ValueError("combo legs require qualified IBKR conids")
        return self


class DefinedRiskOptionComboIntent(BaseModel):
    """以一个 IBKR BAG 订单表示的保证型双腿垂直价差。"""

    client_order_id: str = Field(..., min_length=8, max_length=64)
    strategy_code: str = Field(..., min_length=1, max_length=64)
    legs: list[ComboLegRef] = Field(..., min_length=2, max_length=2)
    quantity: Decimal = Field(..., gt=0)
    limit_price: Decimal
    tif: Literal["DAY", "GTC", "IOC"] = "DAY"
    account: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=8)
    venue: str = Field(default="SMART", min_length=1, max_length=32)
    transmit: bool = True
    guaranteed: Literal[True] = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_combo(self) -> "DefinedRiskOptionComboIntent":
        if self.limit_price == 0:
            raise ValueError("combo limit_price cannot be zero")
        if self.quantity != self.quantity.to_integral_value():
            raise ValueError("option combo quantity must be a whole number")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self


class OrderReplaceCommand(BaseModel):
    client_order_id: str = Field(..., min_length=8, max_length=64)
    expected_revision: int = Field(..., ge=1)
    request: BrokerOrderRequest


class OrderCancelCommand(BaseModel):
    client_order_id: str = Field(..., min_length=8, max_length=64)
    expected_revision: int = Field(..., ge=1)
