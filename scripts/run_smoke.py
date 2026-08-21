from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal

try:
    from _bootstrap import ROOT
    from _common import json_print, open_db, parse_symbols, parse_window, write_json
except ModuleNotFoundError:  # pragma: no cover - used when imported as scripts.run_smoke in tests.
    from scripts._bootstrap import ROOT  # noqa: F401
    from scripts._common import json_print, open_db, parse_symbols, parse_window, write_json
from platform_core.backtest import DBBacktestRunner
from platform_core.core import get_settings
from platform_core.data import (
    IngestionResult,
    build_quality_report,
    persist_quality_report,
    record_progress,
    upsert_equity_bars,
    upsert_option_bars,
    upsert_option_chain,
    upsert_universe,
)
from platform_core.data.fixtures import build_fixture_dataset
from platform_core.infra import IBKRAdapter
from platform_core.risk import BasicRiskEngine
from platform_core.schemas.assets import AssetType
from platform_core.strategy import load_strategy


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run starter smoke: offline fixture or real IBKR.")
    parser.add_argument("--mode", choices=["offline", "ibkr"], default="offline")
    parser.add_argument("--symbols", default=settings.smoke_symbols)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--days", type=int, default=settings.smoke_days)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--track", choices=["equity", "option", "dual"], default="dual")
    parser.add_argument("--dte-min", type=int, default=settings.option_dte_min)
    parser.add_argument("--dte-max", type=int, default=settings.option_dte_max)
    parser.add_argument("--report-path", default=None)
    parser.add_argument(
        "--strategy",
        default=None,
        help="Optional external package.module:attribute; no strategy is bundled.",
    )
    parser.add_argument("--strategy-params", default="{}", help="JSON object")
    args = parser.parse_args()

    symbols = parse_symbols(args.symbols)
    start, end = parse_window(start=args.start, end=args.end, days=args.days)
    _, session = open_db(args.db_url)
    adapter = IBKRAdapter() if args.mode == "ibkr" else None
    try:
        if args.mode == "offline":
            ingest_result, start, end = _ingest_offline(session, symbols=symbols)
        else:
            ingest_result, effective_track = _ingest_ibkr(
                session,
                adapter=adapter,
                symbols=symbols,
                start=start,
                end=end,
                dte_min=args.dte_min,
                dte_max=args.dte_max,
                requested_track=args.track,
            )
            args.track = effective_track
        quality = build_quality_report(
            session,
            run_key=f"smoke-{args.mode}",
            symbols=symbols,
            start=start,
            end=end,
            option_spread_pct_max=Decimal(str(settings.option_spread_pct_max)),
        )
        persist_quality_report(session, quality)
        backtest = None
        if args.strategy:
            parameters = json.loads(args.strategy_params)
            if not isinstance(parameters, dict):
                raise TypeError("--strategy-params must decode to a JSON object")
            strategy = load_strategy(args.strategy, parameters)
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
            backtest = runner.run(
                start=start,
                end=end,
                symbols=symbols,
                track=args.track,
            )
        session.commit()
        output = {
            "mode": args.mode,
            "ingestion": ingest_result.as_dict(),
            "quality": quality.as_dict(),
            "backtest": backtest.as_dict() if backtest is not None else None,
        }
        json_print(output)
        write_json(args.report_path, output)
    finally:
        if adapter is not None:
            adapter.disconnect()
        session.close()


def _ingest_offline(session, *, symbols: list[str]) -> tuple[IngestionResult, datetime, datetime]:
    fixture = build_fixture_dataset(symbols)
    rows = 0
    rows += upsert_universe(session, symbols=symbols, universe_code="starter", asset_type=AssetType.EQUITY)
    rows += upsert_equity_bars(session, fixture.equity_bars)
    rows += upsert_option_chain(session, trade_date=fixture.start.date(), contracts=fixture.option_contracts)
    rows += upsert_option_bars(session, fixture.option_quotes)
    result = IngestionResult(
        task_key=f"offline-smoke:{','.join(symbols)}:{fixture.start.date()}",
        status="COMPLETED",
        rows_written=rows,
        completed_at=datetime.now(UTC),
        details={
            "symbols": symbols,
            "start": fixture.start.isoformat(),
            "end": fixture.end.isoformat(),
        },
    )
    record_progress(session, result, cursor=fixture.end.isoformat())
    return result, fixture.start, fixture.end


def _ingest_ibkr(
    session,
    *,
    adapter: IBKRAdapter | None,
    symbols: list[str],
    start: datetime,
    end: datetime,
    dte_min: int,
    dte_max: int,
    requested_track: str,
) -> tuple[IngestionResult, str]:
    if adapter is None:
        raise RuntimeError("IBKR adapter is not available")
    rows = 0
    option_probe: dict[str, dict[str, str | bool | None]] = {}
    effective_track = requested_track
    rows += upsert_universe(session, symbols=symbols, universe_code="starter", asset_type=AssetType.EQUITY)
    for symbol in symbols:
        rows += upsert_equity_bars(session, adapter.historical_equity_bars(symbol, start=start, end=end))
        option_probe[symbol] = {
            "contract_found": False,
            "quote_found": False,
            "l1_loaded": False,
            "reason": None,
        }
        try:
            contract = adapter.probe_option_contract(symbol, as_of=start.date(), dte_min=dte_min, dte_max=dte_max)
            if contract is None:
                option_probe[symbol]["reason"] = "no_contract_found"
                effective_track = "equity"
                continue
            option_probe[symbol]["contract_found"] = True
            rows += upsert_option_chain(session, trade_date=start.date(), contracts=[contract])
            option_probe[symbol]["reason"] = "contract_details_ok"
            try:
                quote = adapter.snapshot_quote(contract)
                if quote.bid is not None or quote.ask is not None or quote.last is not None:
                    option_probe[symbol]["quote_found"] = True
            except Exception as exc:  # noqa: BLE001
                option_probe[symbol]["reason"] = f"snapshot_failed:{exc}"
            try:
                quotes = adapter.historical_option_l1(contract, start=start, end=end)
                if quotes:
                    rows += upsert_option_bars(session, quotes)
                    option_probe[symbol]["l1_loaded"] = True
                else:
                    effective_track = "equity"
                    option_probe[symbol]["reason"] = "no_option_l1_rows"
            except Exception as exc:  # noqa: BLE001
                effective_track = "equity"
                option_probe[symbol]["reason"] = f"option_l1_failed:{exc}"
        except Exception as exc:  # noqa: BLE001
            effective_track = "equity"
            option_probe[symbol]["reason"] = f"option_probe_failed:{exc}"
    result = IngestionResult(
        task_key=f"ibkr-smoke:{','.join(symbols)}:{start.date()}:{end.date()}",
        status="COMPLETED",
        rows_written=rows,
        completed_at=datetime.now(UTC),
        details={
            "symbols": symbols,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "requested_track": requested_track,
            "effective_track": effective_track,
            "option_probe": option_probe,
        },
    )
    record_progress(session, result, cursor=end.isoformat())
    return result, effective_track


if __name__ == "__main__":
    main()
