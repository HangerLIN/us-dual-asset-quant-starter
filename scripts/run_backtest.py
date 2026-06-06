from __future__ import annotations

import argparse
from decimal import Decimal

from _bootstrap import ROOT  # noqa: F401
from _common import json_print, open_db, parse_symbols, parse_window, write_json
from platform_core.backtest import DBBacktestRunner
from platform_core.core import get_settings
from platform_core.risk import BasicRiskEngine


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run a DB-backed starter backtest.")
    parser.add_argument("--track", choices=["equity", "option", "dual"], default="dual")
    parser.add_argument("--symbols", default=settings.smoke_symbols)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--days", type=int, default=settings.smoke_days)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    symbols = parse_symbols(args.symbols)
    start, end = parse_window(start=args.start, end=args.end, days=args.days)
    _, session = open_db(args.db_url)
    try:
        runner = DBBacktestRunner(
            session=session,
            risk=BasicRiskEngine(
                notional_cap=Decimal(str(settings.risk_notional_cap)),
                option_spread_pct_max=Decimal(str(settings.option_spread_pct_max)),
            ),
        )
        result = runner.run(
            start=start,
            end=end,
            symbols=symbols,
            track=args.track,
            calibration_version=settings.default_calibration_version,
        )
        session.commit()
        json_print(result)
        write_json(args.report_path, result)
    finally:
        session.close()


if __name__ == "__main__":
    main()
