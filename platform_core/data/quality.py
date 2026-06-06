from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from platform_core.db.models import Bar1mEquity, Bar1mOption, DataQualityReport, OptionChainMeta
from platform_core.schemas.assets import AssetType


@dataclass(frozen=True, slots=True)
class CoverageResult:
    ok: bool
    expected_rows: int
    actual_rows: int
    reason: str = "ok"


@dataclass(frozen=True, slots=True)
class QualityCheckResult:
    check_name: str
    asset_type: AssetType
    status: str
    symbol: str | None = None
    expected_rows: int | None = None
    actual_rows: int | None = None
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "PASS"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["asset_type"] = self.asset_type.value
        return payload


@dataclass(frozen=True, slots=True)
class QualityReport:
    run_key: str
    checked_at: datetime
    results: list[QualityCheckResult]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "checked_at": self.checked_at.isoformat(),
            "ok": self.ok,
            "results": [result.as_dict() for result in self.results],
        }


def check_minute_coverage(*, expected_rows: int, actual_rows: int) -> CoverageResult:
    if actual_rows < expected_rows:
        return CoverageResult(False, expected_rows, actual_rows, "missing_minutes")
    return CoverageResult(True, expected_rows, actual_rows)


def build_quality_report(
    session: Session,
    *,
    run_key: str,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    option_spread_pct_max: Decimal = Decimal("0.10"),
    min_option_chain_candidates: int = 1,
    min_option_quote_rows: int = 1,
) -> QualityReport:
    results: list[QualityCheckResult] = []
    expected_minutes = _expected_market_minutes(start, end)
    for raw_symbol in symbols:
        symbol = raw_symbol.upper()
        equity_rows = session.scalar(
            select(func.count())
            .select_from(Bar1mEquity)
            .where(Bar1mEquity.symbol == symbol, Bar1mEquity.ts_end >= start, Bar1mEquity.ts_end <= end)
        )
        coverage = check_minute_coverage(expected_rows=expected_minutes, actual_rows=int(equity_rows or 0))
        results.append(
            QualityCheckResult(
                check_name="equity_minute_coverage",
                asset_type=AssetType.EQUITY,
                symbol=symbol,
                status="PASS" if coverage.ok else "FAIL",
                expected_rows=coverage.expected_rows,
                actual_rows=coverage.actual_rows,
                reason=coverage.reason,
            )
        )

        chain_rows = session.scalar(
            select(func.count())
            .select_from(OptionChainMeta)
            .where(
                OptionChainMeta.underlying_symbol == symbol,
                OptionChainMeta.trade_date >= start.date(),
                OptionChainMeta.trade_date <= end.date(),
            )
        )
        chain_count = int(chain_rows or 0)
        results.append(
            QualityCheckResult(
                check_name="option_chain_candidates",
                asset_type=AssetType.OPTION,
                symbol=symbol,
                status="PASS" if chain_count >= min_option_chain_candidates else "WARN",
                expected_rows=min_option_chain_candidates,
                actual_rows=chain_count,
                reason="ok" if chain_count >= min_option_chain_candidates else "no_option_chain_candidates",
            )
        )

        quote_rows = session.scalar(
            select(func.count())
            .select_from(Bar1mOption)
            .where(
                Bar1mOption.underlying_symbol == symbol,
                Bar1mOption.ts_end >= start,
                Bar1mOption.ts_end <= end,
            )
        )
        missing_bid_ask = session.scalar(
            select(func.count())
            .select_from(Bar1mOption)
            .where(
                Bar1mOption.underlying_symbol == symbol,
                Bar1mOption.ts_end >= start,
                Bar1mOption.ts_end <= end,
                (Bar1mOption.bid.is_(None) | Bar1mOption.ask.is_(None)),
            )
        )
        wide_spread = _wide_spread_count(session, symbol=symbol, start=start, end=end, limit=option_spread_pct_max)
        quote_count = int(quote_rows or 0)
        results.append(
            QualityCheckResult(
                check_name="option_l1_quotes",
                asset_type=AssetType.OPTION,
                symbol=symbol,
                status="PASS" if quote_count >= min_option_quote_rows and wide_spread == 0 else "WARN",
                expected_rows=min_option_quote_rows,
                actual_rows=quote_count,
                reason="ok"
                if quote_count >= min_option_quote_rows and wide_spread == 0
                else "missing_or_wide_option_quotes",
                payload={
                    "missing_bid_ask": int(missing_bid_ask or 0),
                    "wide_spread": wide_spread,
                    "spread_limit": str(option_spread_pct_max),
                },
            )
        )
    return QualityReport(run_key=run_key, checked_at=datetime.now(UTC), results=results)


def persist_quality_report(session: Session, report: QualityReport) -> int:
    count = 0
    for result in report.results:
        session.add(
            DataQualityReport(
                run_key=report.run_key,
                check_name=result.check_name,
                asset_type=result.asset_type.value,
                symbol=result.symbol,
                status=result.status,
                expected_rows=result.expected_rows,
                actual_rows=result.actual_rows,
                reason=result.reason,
                payload=result.payload,
                checked_at=report.checked_at,
            )
        )
        count += 1
    return count


def _expected_market_minutes(start: datetime, end: datetime) -> int:
    if end <= start:
        return 0
    days = _business_days(start.date(), end.date())
    if days <= 1:
        return max(1, int((end - start).total_seconds() // 60) + 1)
    return days * 390


def _business_days(start: date, end: date) -> int:
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _wide_spread_count(session: Session, *, symbol: str, start: datetime, end: datetime, limit: Decimal) -> int:
    rows: Iterable[tuple[Decimal | None, Decimal | None, Decimal | None]] = session.execute(
        select(Bar1mOption.bid, Bar1mOption.ask, Bar1mOption.mid).where(
            Bar1mOption.underlying_symbol == symbol,
            Bar1mOption.ts_end >= start,
            Bar1mOption.ts_end <= end,
        )
    )
    count = 0
    for bid, ask, mid in rows:
        if bid is None or ask is None:
            continue
        effective_mid = mid
        if effective_mid is None and ask >= bid:
            effective_mid = (ask + bid) / Decimal("2")
        if effective_mid is None or effective_mid <= 0:
            continue
        if (ask - bid) / effective_mid > limit:
            count += 1
    return count
