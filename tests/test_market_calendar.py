from __future__ import annotations

from datetime import UTC, date, datetime

from platform_core.data.calendar import expected_market_minutes, session_window
from scripts._common import parse_window


def test_session_window_handles_us_daylight_saving_time() -> None:
    winter_open, winter_close = session_window(date(2026, 1, 5))
    summer_open, summer_close = session_window(date(2026, 7, 10))

    assert winter_open == datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    assert winter_close == datetime(2026, 1, 5, 21, 0, tzinfo=UTC)
    assert summer_open == datetime(2026, 7, 10, 13, 30, tzinfo=UTC)
    assert summer_close == datetime(2026, 7, 10, 20, 0, tzinfo=UTC)


def test_expected_minutes_respects_early_close() -> None:
    start, end = session_window(date(2026, 11, 27))

    assert end == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert expected_market_minutes(start, end) == 210


def test_parse_window_uses_exclusive_session_close() -> None:
    start, end = parse_window(start="2026-07-10", end="2026-07-10", days=1)

    assert start == datetime(2026, 7, 10, 13, 30, tzinfo=UTC)
    assert end == datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
    assert expected_market_minutes(start, end) == 390


def test_parse_window_moves_weekend_end_to_previous_session() -> None:
    start, end = parse_window(start=None, end="2026-07-11", days=1)

    assert start == datetime(2026, 7, 10, 13, 30, tzinfo=UTC)
    assert end == datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
