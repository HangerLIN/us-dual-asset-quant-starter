from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .assets import AssetType, InstrumentRef


class RuntimeMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    INACTIVE = "INACTIVE"


class SignalEnvelope(BaseModel):
    strategy_code: str
    signal_code: str
    instrument: InstrumentRef
    side: str = Field(..., pattern="^(BUY|SELL|FLAT)$")
    confidence: Decimal = Field(..., ge=0, le=1)
    generated_at: datetime
    reason: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = Field(90, ge=1)

    @property
    def asset_type(self) -> AssetType:
        return self.instrument.asset_type


class PortfolioDecision(BaseModel):
    strategy_code: str
    instrument: InstrumentRef
    side: str = Field(..., pattern="^(BUY|SELL)$")
    quantity: Decimal = Field(..., gt=0)
    target_notional: Decimal = Field(..., ge=0)
    signal_code: str
    score: Decimal = Field(..., ge=0)
    reason: dict[str, Any] = Field(default_factory=dict)


class RiskCheckRequest(BaseModel):
    strategy_code: str
    instrument: InstrumentRef
    side: str = Field(..., pattern="^(BUY|SELL)$")
    quantity: Decimal = Field(..., gt=0)
    notional: Decimal = Field(..., ge=0)
    timestamp: datetime
    signal_code: str | None = None
    quote: dict[str, Any] | None = None


class RiskCheckResult(BaseModel):
    approved: bool
    code: str = "OK"
    detail: str = "risk-ok"
    reasons: dict[str, Any] = Field(default_factory=dict)


class ExecutionRequest(BaseModel):
    strategy_code: str
    instrument: InstrumentRef
    side: str = Field(..., pattern="^(BUY|SELL)$")
    quantity: Decimal = Field(..., gt=0)
    limit_price: Decimal = Field(..., gt=0)
    tif: str = "DAY"
    signal_code: str | None = None
    execution_mode: str = "MARKETABLE_LIMIT"
    trace_id: str | None = None
    client_order_id: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


class ExecutionFill(BaseModel):
    strategy_code: str
    instrument: InstrumentRef
    side: str = Field(..., pattern="^(BUY|SELL)$")
    quantity: Decimal
    fill_price: Decimal
    filled_at: datetime
    fees: Decimal = Decimal(0)
    execution_id: str | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None


class BrokerOrderUpdate(BaseModel):
    client_order_id: str
    status: OrderStatus
    updated_at: datetime
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal(0)
    remaining_quantity: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None
    message: str | None = None


class AccountSnapshot(BaseModel):
    account_id: str
    mode: RuntimeMode
    captured_at: datetime
    cash: Decimal | None = None
    net_liquidation: Decimal | None = None
    buying_power: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    values: dict[str, Decimal | str] = Field(default_factory=dict)


class PlatformEvent(BaseModel):
    event_type: str
    occurred_at: datetime
    trace_id: str
    strategy_code: str | None = None
    instrument: InstrumentRef | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class BacktestOrderEvent(BaseModel):
    run_id: int | None = None
    trace_id: str | None = None
    instrument: InstrumentRef
    event_type: str
    reason_code: str | None = None
    event_time: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
