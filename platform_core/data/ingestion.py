from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from platform_core.db.models import (
    Bar1mEquity,
    Bar1mOption,
    IngestionProgress,
    OptionChainMeta,
    StockUniverse,
)
from platform_core.schemas import BarEvent, MarketQuote
from platform_core.schemas.assets import AssetType, InstrumentRef


@dataclass(frozen=True, slots=True)
class IngestionResult:
    task_key: str
    status: str
    rows_written: int = 0
    failure_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["started_at"] = self.started_at.isoformat()
        payload["completed_at"] = self.completed_at.isoformat()
        payload["details"] = _json_safe(self.details)
        return payload


def record_progress(session: Session, result: IngestionResult, *, cursor: str | None = None) -> None:
    session.merge(
        IngestionProgress(
            task_key=result.task_key,
            status=result.status,
            last_cursor=cursor,
            failure_reason=result.failure_reason,
            updated_at=result.completed_at,
        )
    )


def upsert_universe(
    session: Session,
    *,
    symbols: Iterable[str],
    universe_code: str = "starter",
    asset_type: AssetType = AssetType.EQUITY,
) -> int:
    count = 0
    for symbol in symbols:
        session.merge(
            StockUniverse(
                universe_code=universe_code,
                symbol=symbol.upper(),
                asset_type=asset_type.value,
                enabled=True,
            )
        )
        count += 1
    return count


def upsert_equity_bars(session: Session, bars: Iterable[BarEvent]) -> int:
    count = 0
    for bar in bars:
        session.merge(
            Bar1mEquity(
                symbol=bar.instrument.symbol.upper(),
                ts_end=bar.bar_end,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                vwap=bar.vwap,
            )
        )
        count += 1
    return count


def upsert_option_bars(session: Session, quotes: Iterable[MarketQuote]) -> int:
    count = 0
    for quote in quotes:
        instrument = quote.instrument
        if instrument.asset_type != AssetType.OPTION:
            continue
        conid = instrument.conid or int(instrument.metadata.get("conid") or 0)
        if conid <= 0:
            continue
        expiry = _as_date(instrument.expiry)
        session.merge(
            Bar1mOption(
                conid=conid,
                ts_end=quote.quote_ts,
                underlying_symbol=instrument.symbol.upper(),
                expiry=expiry,
                right=_right_code(instrument.option_right),
                strike=Decimal(str(instrument.strike)),
                bid=quote.bid,
                ask=quote.ask,
                mid=quote.mid,
                last=quote.last,
                volume=quote.volume,
                open_interest=quote.open_interest,
            )
        )
        count += 1
    return count


def upsert_option_chain(
    session: Session,
    *,
    trade_date: date,
    contracts: Iterable[InstrumentRef | Mapping[str, Any]],
) -> int:
    count = 0
    for contract in contracts:
        payload = _contract_payload(contract)
        conid = int(payload.get("conid") or 0)
        if conid <= 0:
            continue
        bid = _decimal_or_none(payload.get("bid"))
        ask = _decimal_or_none(payload.get("ask"))
        mid = _decimal_or_none(payload.get("mid"))
        if mid is None and bid is not None and ask is not None and ask >= bid:
            mid = (bid + ask) / Decimal(2)
        session.merge(
            OptionChainMeta(
                trade_date=trade_date,
                conid=conid,
                underlying_symbol=str(payload["symbol"]).upper(),
                expiry=_as_date(payload["expiry"]),
                right=_right_code(str(payload["right"])),
                strike=Decimal(str(payload["strike"])),
                dte=payload.get("dte"),
                delta=_decimal_or_none(payload.get("delta")),
                bid=bid,
                ask=ask,
                mid=mid,
                open_interest=payload.get("open_interest"),
                volume=payload.get("volume"),
            )
        )
        count += 1
    return count


def _contract_payload(contract: InstrumentRef | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(contract, InstrumentRef):
        return {
            "conid": contract.conid or contract.metadata.get("conid"),
            "symbol": contract.symbol,
            "expiry": contract.expiry,
            "right": contract.option_right,
            "strike": contract.strike,
            "dte": contract.metadata.get("dte"),
            "delta": contract.metadata.get("delta"),
            "bid": contract.metadata.get("bid"),
            "ask": contract.metadata.get("ask"),
            "mid": contract.metadata.get("mid"),
            "volume": contract.metadata.get("volume"),
            "open_interest": contract.metadata.get("open_interest"),
        }
    return dict(contract)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _right_code(value: str | None) -> str:
    if value in {"CALL", "C"}:
        return "CALL"
    if value in {"PUT", "P"}:
        return "PUT"
    raise ValueError(f"unsupported option right: {value}")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value
