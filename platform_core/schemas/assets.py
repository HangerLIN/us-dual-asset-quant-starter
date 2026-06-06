from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class AssetType(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    OPTION = "OPTION"


class InstrumentRef(BaseModel):
    asset_type: AssetType
    symbol: str = Field(..., max_length=32)
    currency: str = "USD"
    venue: str | None = None
    conid: int | None = None
    option_right: str | None = Field(default=None, pattern="^(CALL|PUT)$")
    strike: Decimal | None = Field(default=None, gt=0)
    expiry: date | datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_option_contract(self) -> "InstrumentRef":
        if self.asset_type == AssetType.OPTION:
            missing = [
                name
                for name, value in {
                    "option_right": self.option_right,
                    "strike": self.strike,
                    "expiry": self.expiry,
                }.items()
                if value is None
            ]
            if missing:
                raise ValueError(f"OPTION instrument missing fields: {', '.join(missing)}")
        return self


class MarketQuote(BaseModel):
    instrument: InstrumentRef
    quote_ts: datetime
    bid: Decimal | None = Field(default=None, ge=0)
    ask: Decimal | None = Field(default=None, ge=0)
    mid: Decimal | None = Field(default=None, ge=0)
    last: Decimal | None = Field(default=None, ge=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    source: str = "ibkr"

    @model_validator(mode="after")
    def derive_mid(self) -> "MarketQuote":
        if self.mid is None and self.bid is not None and self.ask is not None and self.ask >= self.bid:
            self.mid = (self.bid + self.ask) / Decimal("2")
        return self

    @property
    def spread_abs(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal | None:
        if self.mid is None or self.mid <= 0 or self.spread_abs is None:
            return None
        return self.spread_abs / self.mid


class BarEvent(BaseModel):
    instrument: InstrumentRef
    bar_start: datetime
    bar_end: datetime
    timeframe: str = "1m"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = Field(..., ge=0)
    vwap: Decimal | None = None


class PositionSnapshot(BaseModel):
    strategy_code: str
    instrument: InstrumentRef
    quantity: Decimal
    avg_open_price: Decimal
    mark_price: Decimal
    notional: Decimal
    opened_at: datetime | None = None
    updated_at: datetime | None = None
