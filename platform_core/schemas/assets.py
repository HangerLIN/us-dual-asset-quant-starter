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
    COMBO = "COMBO"


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

    @property
    def key(self) -> str:
        """返回供订单、成交、持仓和对账共同使用的稳定标识。"""

        if self.conid is not None:
            return f"{self.asset_type.value}:CONID:{self.conid}"
        if self.asset_type == AssetType.OPTION:
            expiry = self.expiry.date() if isinstance(self.expiry, datetime) else self.expiry
            return ":".join(
                [
                    self.asset_type.value,
                    self.symbol.upper(),
                    str(expiry),
                    str(self.option_right),
                    str(self.strike),
                    self.currency.upper(),
                ]
            )
        return ":".join(
            [
                self.asset_type.value,
                self.symbol.upper(),
                (self.venue or "SMART").upper(),
                self.currency.upper(),
            ]
        )

    @property
    def multiplier(self) -> Decimal:
        """返回显式乘数，期权未配置时默认使用 100。"""

        value = self.metadata.get("multiplier")
        if value is not None:
            return Decimal(str(value))
        return Decimal(100) if self.asset_type == AssetType.OPTION else Decimal(1)

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
        if self.asset_type == AssetType.COMBO:
            legs = self.metadata.get("combo_legs")
            if self.metadata.get("broker_unresolved_combo") is True and not legs:
                return self
            if not isinstance(legs, list) or len(legs) < 2:
                raise ValueError("COMBO instrument requires at least two combo_legs")
            for leg in legs:
                if not isinstance(leg, dict) or int(leg.get("conid") or 0) <= 0:
                    raise ValueError("each COMBO leg requires a positive conid")
                if int(leg.get("ratio") or 0) <= 0:
                    raise ValueError("each COMBO leg requires a positive ratio")
                if str(leg.get("action", "")).upper() not in {"BUY", "SELL"}:
                    raise ValueError("each COMBO leg action must be BUY or SELL")
        return self


class MarketQuote(BaseModel):
    instrument: InstrumentRef
    quote_ts: datetime
    received_at: datetime | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = Field(default=None, ge=0)
    ask_size: Decimal | None = Field(default=None, ge=0)
    mid: Decimal | None = None
    last: Decimal | None = None
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    source: str = "ibkr"
    market_data_type: int | None = None
    timestamp_source: str = "broker"
    halted_status: int | None = None
    shortable: Decimal | None = None

    @model_validator(mode="after")
    def derive_mid(self) -> "MarketQuote":
        if self.instrument.asset_type != AssetType.COMBO:
            prices = (self.bid, self.ask, self.mid, self.last)
            if any(price is not None and price < 0 for price in prices):
                raise ValueError("non-combo market prices cannot be negative")
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
        if self.mid is None or self.mid == 0 or self.spread_abs is None:
            return None
        return self.spread_abs / abs(self.mid)


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
