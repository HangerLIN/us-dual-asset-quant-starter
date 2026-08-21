from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

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
    if end:
        end_dt = parse_datetime(end, default_time=time(19, 59))
    else:
        today = datetime.now(UTC).date()
        end_dt = datetime.combine(today, time(19, 59), tzinfo=UTC)
    if start:
        start_dt = parse_datetime(start, default_time=time(13, 30))
    else:
        start_dt = datetime.combine(end_dt.date() - timedelta(days=max(0, days - 1)), time(13, 30), tzinfo=UTC)
    return start_dt, end_dt


def parse_datetime(value: str, *, default_time: time) -> datetime:
    if "T" not in value and " " not in value:
        return datetime.combine(date.fromisoformat(value), default_time, tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
