from __future__ import annotations

import argparse
from datetime import UTC, datetime

from _bootstrap import ROOT  # noqa: F401
from _common import json_print, open_db, parse_symbols, parse_window
from platform_core.core import get_settings
from platform_core.data import (
    IngestionResult,
    record_progress,
    upsert_equity_bars,
    upsert_universe,
)
from platform_core.infra import IBKRAdapter
from platform_core.schemas.assets import AssetType


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Ingest IBKR 1m bars for equities/ETFs.")
    parser.add_argument("--symbols", default=settings.smoke_symbols, help="Comma-separated symbols.")
    parser.add_argument("--start", default=None, help="UTC date/datetime; date defaults to 13:30 UTC.")
    parser.add_argument("--end", default=None, help="UTC date/datetime; date defaults to 19:59 UTC.")
    parser.add_argument("--days", type=int, default=settings.smoke_days)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--universe", default="starter")
    args = parser.parse_args()

    symbols = parse_symbols(args.symbols)
    start, end = parse_window(start=args.start, end=args.end, days=args.days)
    _, session = open_db(args.db_url)
    adapter = IBKRAdapter()
    started_at = datetime.now(UTC)
    rows = 0
    try:
        for symbol in symbols:
            bars = adapter.historical_equity_bars(symbol, start=start, end=end)
            rows += upsert_equity_bars(session, bars)
        upsert_universe(session, symbols=symbols, universe_code=args.universe, asset_type=AssetType.EQUITY)
        result = IngestionResult(
            task_key=f"equity:{','.join(symbols)}:{start.date()}:{end.date()}",
            status="COMPLETED",
            rows_written=rows,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            details={"symbols": symbols, "start": start.isoformat(), "end": end.isoformat()},
        )
        record_progress(session, result, cursor=end.isoformat())
        session.commit()
        json_print(result)
    except Exception as exc:  # noqa: BLE001 - 命令行工具会先记录失败详情再抛出异常。
        session.rollback()
        result = IngestionResult(
            task_key=f"equity:{','.join(symbols)}:{start.date()}:{end.date()}",
            status="FAILED",
            rows_written=rows,
            failure_reason=str(exc),
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
        record_progress(session, result)
        session.commit()
        raise
    finally:
        adapter.disconnect()
        session.close()


if __name__ == "__main__":
    main()
