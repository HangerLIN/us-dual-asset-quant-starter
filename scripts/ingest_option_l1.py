from __future__ import annotations

import argparse
from datetime import UTC, datetime

from sqlalchemy import select

from _bootstrap import ROOT  # noqa: F401
from _common import json_print, open_db, parse_symbols, parse_window
from platform_core.core import get_settings
from platform_core.data import IngestionResult, record_progress, upsert_option_bars
from platform_core.db.models import OptionChainMeta
from platform_core.infra import IBKRAdapter
from platform_core.schemas.assets import AssetType, InstrumentRef


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Ingest IBKR historical option bid/ask bars.")
    parser.add_argument("--symbols", default=settings.smoke_symbols, help="Comma-separated underlying symbols.")
    parser.add_argument("--start", default=None, help="UTC date/datetime; date defaults to 13:30 UTC.")
    parser.add_argument("--end", default=None, help="UTC date/datetime; date defaults to 19:59 UTC.")
    parser.add_argument("--days", type=int, default=settings.smoke_days)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--limit-contracts", type=int, default=6)
    args = parser.parse_args()

    symbols = parse_symbols(args.symbols)
    start, end = parse_window(start=args.start, end=args.end, days=args.days)
    _, session = open_db(args.db_url)
    contracts = _load_contracts(session, symbols=symbols, trade_date=start.date(), limit=args.limit_contracts)
    if not contracts:
        raise RuntimeError("No option_chain_meta rows found; run ingest_option_chain.py first.")

    adapter = IBKRAdapter()
    started_at = datetime.now(UTC)
    rows = 0
    try:
        for contract in contracts:
            quotes = adapter.historical_option_l1(contract, start=start, end=end)
            rows += upsert_option_bars(session, quotes)
        result = IngestionResult(
            task_key=f"option-l1:{','.join(symbols)}:{start.date()}:{end.date()}",
            status="COMPLETED",
            rows_written=rows,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            details={"symbols": symbols, "contracts": len(contracts), "start": start.isoformat(), "end": end.isoformat()},
        )
        record_progress(session, result, cursor=end.isoformat())
        session.commit()
        json_print(result)
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        result = IngestionResult(
            task_key=f"option-l1:{','.join(symbols)}:{start.date()}:{end.date()}",
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


def _load_contracts(session, *, symbols: list[str], trade_date, limit: int) -> list[InstrumentRef]:
    rows = session.scalars(
        select(OptionChainMeta)
        .where(
            OptionChainMeta.underlying_symbol.in_(symbols),
            OptionChainMeta.trade_date == trade_date,
        )
        .order_by(OptionChainMeta.underlying_symbol.asc(), OptionChainMeta.expiry.asc(), OptionChainMeta.strike.asc())
        .limit(limit)
    )
    output: list[InstrumentRef] = []
    for row in rows:
        output.append(
            InstrumentRef(
                asset_type=AssetType.OPTION,
                symbol=row.underlying_symbol,
                conid=row.conid,
                option_right="CALL" if row.right == "CALL" else "PUT",
                strike=row.strike,
                expiry=row.expiry,
                metadata={"dte": row.dte},
            )
        )
    return output


if __name__ == "__main__":
    main()
