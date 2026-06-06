from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from .assets import AssetType, InstrumentRef


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


class ExecutionFill(BaseModel):
    strategy_code: str
    instrument: InstrumentRef
    side: str = Field(..., pattern="^(BUY|SELL)$")
    quantity: Decimal
    fill_price: Decimal
    filled_at: datetime
    fees: Decimal = Decimal("0")
    execution_id: str | None = None


class BacktestOrderEvent(BaseModel):
    run_id: int | None = None
    trace_id: str | None = None
    instrument: InstrumentRef
    event_type: str
    reason_code: str | None = None
    event_time: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
