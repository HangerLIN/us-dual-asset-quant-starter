from __future__ import annotations

from datetime import UTC, date, datetime

import exchange_calendars as xcals


_XNYS = xcals.get_calendar("XNYS")


def session_window(trade_date: date) -> tuple[datetime, datetime]:
    if not _XNYS.is_session(trade_date):
        raise ValueError(f"{trade_date.isoformat()} is not an XNYS trading session")
    return _as_utc(_XNYS.session_open(trade_date)), _as_utc(_XNYS.session_close(trade_date))


def latest_session_on_or_before(value: date) -> date:
    return _XNYS.date_to_session(value, direction="previous").date()


def session_start_for_lookback(end_session: date, *, days: int) -> datetime:
    if days < 1:
        raise ValueError("days must be at least 1")
    sessions = _XNYS.sessions_window(end_session, -days)
    return _as_utc(_XNYS.session_open(sessions[0]))


def expected_market_minutes(start: datetime, end: datetime) -> int:
    start_utc = _require_aware_utc(start)
    end_utc = _require_aware_utc(end)
    if end_utc <= start_utc:
        return 0

    sessions = _XNYS.sessions_in_range(start_utc.date(), end_utc.date())
    total = 0
    for session in sessions:
        session_open = _as_utc(_XNYS.session_open(session))
        session_close = _as_utc(_XNYS.session_close(session))
        overlap_start = max(start_utc, session_open)
        overlap_end = min(end_utc, session_close)
        if overlap_end > overlap_start:
            total += int((overlap_end - overlap_start).total_seconds() // 60)
    return total


def _as_utc(value) -> datetime:
    return value.to_pydatetime().astimezone(UTC)


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("market window datetimes must be timezone-aware")
    return value.astimezone(UTC)
