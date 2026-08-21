from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .assets import AssetType, InstrumentRef


class BrokerOrderRequest(BaseModel):
    instrument: InstrumentRef
    side: Literal["BUY", "SELL"]
    quantity: Decimal = Field(..., gt=0)
    order_type: Literal["MKT", "LMT", "STP", "STP_LMT"] = "LMT"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: Literal["DAY", "GTC", "GTD", "IOC", "FOK"] = "DAY"
    account: str | None = None
    order_ref: str | None = Field(default=None, max_length=64)
    transmit: bool = True
    what_if: bool = False
    outside_rth: bool = False
    good_after_time: datetime | None = None
    good_till_date: datetime | None = None
    parent_order_id: int | None = Field(default=None, ge=0)
    oca_group: str | None = Field(default=None, max_length=64)
    oca_type: Literal[1, 2, 3] | None = None
    reduce_only: bool = False

    @model_validator(mode="after")
    def validate_prices(self) -> "BrokerOrderRequest":
        if self.order_type in {"LMT", "STP_LMT"} and self.limit_price is None:
            raise ValueError(f"{self.order_type} order requires limit_price")
        if self.order_type in {"STP", "STP_LMT"} and self.stop_price is None:
            raise ValueError(f"{self.order_type} order requires stop_price")
        if self.limit_price is not None:
            if self.instrument.asset_type == AssetType.COMBO:
                if self.limit_price == 0:
                    raise ValueError("COMBO limit_price cannot be zero")
            elif self.limit_price <= 0:
                raise ValueError("limit_price must be positive")
        if self.stop_price is not None and self.stop_price <= 0:
            raise ValueError("stop_price must be positive")
        if self.tif == "GTD" and self.good_till_date is None:
            raise ValueError("GTD order requires good_till_date")
        if self.oca_type is not None and not self.oca_group:
            raise ValueError("oca_type requires oca_group")
        return self


class BrokerOrderStatus(BaseModel):
    order_id: int
    status: str
    instrument: InstrumentRef | None = None
    account: str | None = None
    side: str | None = None
    order_type: str | None = None
    quantity: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: str | None = None
    filled: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    last_fill_price: Decimal = Decimal("0")
    permanent_id: int | None = None
    client_id: int | None = None
    parent_id: int | None = None
    why_held: str | None = None
    order_ref: str | None = None
    initial_margin_change: Decimal | None = None
    maintenance_margin_change: Decimal | None = None
    equity_with_loan_change: Decimal | None = None
    warning_text: str | None = None
    updated_at: datetime


class BrokerExecution(BaseModel):
    execution_id: str
    order_id: int
    permanent_id: int | None = None
    client_id: int | None = None
    account: str
    instrument: InstrumentRef
    side: str
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    exchange: str | None = None
    order_ref: str | None = None
    commission: Decimal | None = None
    commission_currency: str | None = None
    realized_pnl: Decimal | None = None


class BrokerPosition(BaseModel):
    account: str
    instrument: InstrumentRef
    quantity: Decimal
    avg_cost: Decimal


class BrokerAccountValue(BaseModel):
    account: str
    tag: str
    value: str
    currency: str | None = None


class BrokerPnL(BaseModel):
    account: str
    daily_pnl: Decimal
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    captured_at: datetime
