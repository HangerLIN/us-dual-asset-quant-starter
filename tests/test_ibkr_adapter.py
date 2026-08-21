from __future__ import annotations

from datetime import date
from decimal import Decimal

from platform_core.infra.ibkr import _duration, _expiry_yyyymmdd, _select_strikes


def test_ibkr_helper_selects_nearby_strikes() -> None:
    strikes = _select_strikes([490, 495, 500, 505, 510], Decimal(501), max_per_side=2)
    assert strikes == [Decimal(495), Decimal(500), Decimal(505), Decimal(510)]


def test_ibkr_helper_formats_expiry() -> None:
    assert _expiry_yyyymmdd(date(2026, 6, 19)) == "20260619"


def test_ibkr_duration_is_at_least_one_day() -> None:
    from datetime import UTC, datetime

    start = datetime(2026, 5, 27, 13, 30, tzinfo=UTC)
    end = datetime(2026, 5, 27, 19, 59, tzinfo=UTC)
    assert _duration(start, end) == "1 D"
