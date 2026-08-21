from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from platform_core.data.calendar import (
    latest_session_on_or_before,
    session_start_for_lookback,
    session_window,
)
from platform_core.db import Base, get_engine, get_session_factory


def parse_symbols(value: str | Sequence[str]) -> list[str]:
    if isinstance(value, str):
        parts = value.replace(" ", "").split(",")
    else:
        parts = list(value)
    return [part.upper() for part in parts if part]


def parse_date(value: str | None, *, default: date | None = None) -> date:
    if value is None:
        if default is None:
            return datetime.now(UTC).date()
        return default
    return date.fromisoformat(value)


def parse_window(
    *,
    start: str | None,
    end: str | None,
    days: int,
) -> tuple[datetime, datetime]:
    if days < 1:
        raise ValueError("days must be at least 1")

    if end and not _is_date_only(end):
        end_dt = parse_datetime(end, default_time=time.max)
        end_session = latest_session_on_or_before(end_dt.date())
    else:
        requested_end = date.fromisoformat(end) if end else datetime.now(UTC).date()
        end_session = latest_session_on_or_before(requested_end)
        _, end_dt = session_window(end_session)
        if end is None and datetime.now(UTC) < end_dt:
            end_session = latest_session_on_or_before(end_session - timedelta(days=1))
            _, end_dt = session_window(end_session)

    if start and not _is_date_only(start):
        start_dt = parse_datetime(start, default_time=time.min)
    elif start:
        start_dt, _ = session_window(date.fromisoformat(start))
    else:
        start_dt = session_start_for_lookback(end_session, days=days)
    if start_dt >= end_dt:
        raise ValueError("market window start must be before end")
    return start_dt, end_dt


def parse_datetime(value: str, *, default_time: time) -> datetime:
    if "T" not in value and " " not in value:
        return datetime.combine(date.fromisoformat(value), default_time, tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_date_only(value: str) -> bool:
    return "T" not in value and " " not in value


def open_db(database_url: str | None, *, create: bool = True):
    engine = get_engine(database_url)
    if create:
        init_db(engine)
    factory = get_session_factory(database_url)
    return engine, factory()


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def json_print(payload: Any) -> None:
    print(json.dumps(json_safe(payload), indent=2, sort_keys=True))


def write_json(path: str | None, payload: Any) -> None:
    if not path:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True), encoding="utf-8")


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "as_dict"):
        return json_safe(value.as_dict())
    return value
