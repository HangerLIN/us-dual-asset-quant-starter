from __future__ import annotations

import argparse
from decimal import Decimal

from _bootstrap import ROOT  # noqa: F401
from _common import json_print, open_db, parse_symbols, parse_window, write_json

from platform_core.core import get_settings
from platform_core.data import build_quality_report, persist_quality_report


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run starter data quality checks.")
    parser.add_argument("--symbols", default=settings.smoke_symbols)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--days", type=int, default=settings.smoke_days)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--run-key", default="starter-quality")
    parser.add_argument("--report-path", default=None)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on any FAIL/WARN.")
    args = parser.parse_args()

    symbols = parse_symbols(args.symbols)
    start, end = parse_window(start=args.start, end=args.end, days=args.days)
    _, session = open_db(args.db_url)
    try:
        report = build_quality_report(
            session,
            run_key=args.run_key,
            symbols=symbols,
            start=start,
            end=end,
            option_spread_pct_max=Decimal(str(settings.option_spread_pct_max)),
        )
        persist_quality_report(session, report)
        session.commit()
        json_print(report)
        write_json(args.report_path, report)
        if args.strict and not report.ok:
            raise SystemExit(1)
    finally:
        session.close()


if __name__ == "__main__":
    main()
