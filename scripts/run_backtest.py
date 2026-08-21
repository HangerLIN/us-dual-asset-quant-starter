from __future__ import annotations

import argparse
import json
from decimal import Decimal

from _bootstrap import ROOT  # noqa: F401
from _common import json_print, open_db, parse_symbols, parse_window, write_json

from platform_core.backtest import DBBacktestRunner
from platform_core.core import get_settings
from platform_core.risk import BasicRiskEngine
from platform_core.strategy import load_strategy


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run a DB-backed starter backtest.")
    parser.add_argument("--strategy", required=True, help="package.module:attribute")
    parser.add_argument("--strategy-params", default="{}", help="JSON object")
    parser.add_argument("--track", choices=["equity", "option", "dual"], default="dual")
    parser.add_argument("--symbols", default=settings.smoke_symbols)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--days", type=int, default=settings.smoke_days)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--report-path", default=None)
    args = parser.parse_args()

    parameters = json.loads(args.strategy_params)
    if not isinstance(parameters, dict):
        raise TypeError("--strategy-params must decode to a JSON object")
    strategy = load_strategy(args.strategy, parameters)
    symbols = parse_symbols(args.symbols)
    start, end = parse_window(start=args.start, end=args.end, days=args.days)
    _, session = open_db(args.db_url)
    try:
        runner = DBBacktestRunner(
            session=session,
            strategy=strategy,
            parameters=parameters,
            risk=BasicRiskEngine(
                notional_cap=Decimal(str(settings.risk_notional_cap)),
                option_spread_pct_max=Decimal(str(settings.option_spread_pct_max)),
                daily_loss_limit=Decimal(str(settings.risk_daily_loss_limit)),
                gross_exposure_cap=Decimal(str(settings.risk_gross_exposure_cap)),
                max_quote_age_seconds=settings.max_quote_age_seconds,
            ),
        )
        result = runner.run(
            start=start,
            end=end,
            symbols=symbols,
            track=args.track,
        )
        session.commit()
        json_print(result)
        write_json(args.report_path, result)
    finally:
        session.close()


if __name__ == "__main__":
    main()
