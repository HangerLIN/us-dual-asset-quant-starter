from __future__ import annotations

import argparse
from datetime import UTC, datetime

from _bootstrap import ROOT  # noqa: F401
from _common import json_print, open_db, parse_date, parse_symbols

from platform_core.core import get_settings
from platform_core.data import IngestionResult, record_progress, upsert_option_chain
from platform_core.infra import IBKRAdapter


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Resolve IBKR option chain candidates.")
    parser.add_argument("--symbols", default=settings.smoke_symbols, help="Comma-separated underlying symbols.")
    parser.add_argument("--as-of", default=None, help="Trade date, YYYY-MM-DD.")
    parser.add_argument("--dte-min", type=int, default=settings.option_dte_min)
    parser.add_argument("--dte-max", type=int, default=settings.option_dte_max)
    parser.add_argument("--max-per-side", type=int, default=3)
    parser.add_argument("--max-expiries", type=int, default=1)
    parser.add_argument("--include-quotes", action="store_true")
    parser.add_argument("--db-url", default=None)
    args = parser.parse_args()

    symbols = parse_symbols(args.symbols)
    trade_date = parse_date(args.as_of)
    _, session = open_db(args.db_url)
    adapter = IBKRAdapter()
    started_at = datetime.now(UTC)
    rows = 0
    try:
        for symbol in symbols:
            contracts = adapter.option_chain(
                symbol,
                as_of=trade_date,
                dte_min=args.dte_min,
                dte_max=args.dte_max,
                max_per_side=args.max_per_side,
                max_expiries=args.max_expiries,
                include_quotes=args.include_quotes,
            )
            rows += upsert_option_chain(session, trade_date=trade_date, contracts=contracts)
        result = IngestionResult(
            task_key=f"option-chain:{','.join(symbols)}:{trade_date}",
            status="COMPLETED",
            rows_written=rows,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            details={
                "symbols": symbols,
                "as_of": trade_date.isoformat(),
                "dte_min": args.dte_min,
                "dte_max": args.dte_max,
            },
        )
        record_progress(session, result, cursor=trade_date.isoformat())
        session.commit()
        json_print(result)
    except Exception as exc:
        session.rollback()
        result = IngestionResult(
            task_key=f"option-chain:{','.join(symbols)}:{trade_date}",
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
