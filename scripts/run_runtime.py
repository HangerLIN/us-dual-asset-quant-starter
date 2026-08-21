from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
from threading import Event

from _bootstrap import ROOT  # noqa: F401
from _common import open_db, parse_symbols

from platform_core.broker import IBKRBroker, build_broker
from platform_core.core import get_settings
from platform_core.data import IBKRPollingQuoteFeed, QuoteBarAggregator
from platform_core.execution import OrderManager
from platform_core.risk import BasicRiskEngine
from platform_core.runtime import InMemoryEventBus, RedisStreamEventBus, TradingEngine
from platform_core.schemas import AssetType, InstrumentRef, RuntimeMode
from platform_core.strategy import load_strategy


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Run an external strategy against IBKR paper or live trading."
    )
    parser.add_argument("--mode", choices=["paper", "live"], required=True)
    parser.add_argument("--strategy", required=True, help="package.module:attribute")
    parser.add_argument("--strategy-params", default="{}", help="JSON object")
    parser.add_argument("--symbols", default=settings.smoke_symbols)
    parser.add_argument(
        "--instruments-file",
        default=None,
        help="Optional JSON file containing full InstrumentRef objects.",
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--bar-seconds", type=int, default=60)
    parser.add_argument("--db-url", default=None)
    parser.add_argument("--publish-redis", action="store_true")
    parser.add_argument("--once", action="store_true", help="Process one snapshot per instrument.")
    args = parser.parse_args()

    mode = RuntimeMode(args.mode.upper())
    parameters = json.loads(args.strategy_params)
    if not isinstance(parameters, dict):
        raise TypeError("--strategy-params must decode to a JSON object")
    strategy = load_strategy(args.strategy, parameters)
    instruments = _load_instruments(args.instruments_file, args.symbols)
    _, session = open_db(args.db_url)
    broker = build_broker(mode, settings)
    if not isinstance(broker, IBKRBroker):
        raise TypeError("paper/live runtime requires an IBKR broker")
    event_bus = (
        RedisStreamEventBus(settings.redis_url)
        if args.publish_redis
        else InMemoryEventBus()
    )
    engine = TradingEngine(
        strategy=strategy,
        broker=broker,
        order_manager=OrderManager(session=session, broker=broker),
        risk=BasicRiskEngine(
            notional_cap=settings.risk_notional_cap,
            option_spread_pct_max=settings.option_spread_pct_max,
            daily_loss_limit=settings.risk_daily_loss_limit,
            gross_exposure_cap=settings.risk_gross_exposure_cap,
            max_quote_age_seconds=settings.max_quote_age_seconds,
        ),
        event_bus=event_bus,
        parameters=parameters,
        order_ttl_seconds=settings.order_ttl_seconds,
    )
    feed = IBKRPollingQuoteFeed(
        broker.adapter,
        poll_interval_seconds=args.poll_seconds,
    )
    aggregator = QuoteBarAggregator(timeframe_seconds=args.bar_seconds)
    stop_event = Event()
    _install_signal_handlers(stop_event)
    try:
        engine.start()
        for quote in feed.stream(instruments, stop_event=stop_event, once=args.once):
            engine.process_quote(quote)
            bar = aggregator.update(quote)
            if bar is not None:
                engine.process_bar(bar, quote=quote)
            session.commit()
        engine.poll_broker()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        engine.stop()
        session.close()


def _load_instruments(path: str | None, symbols: str) -> list[InstrumentRef]:
    if path:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("instrument file must contain a JSON list")
        return [InstrumentRef.model_validate(item) for item in payload]
    return [
        InstrumentRef(
            asset_type=AssetType.ETF if symbol in {"SPY", "QQQ", "IWM"} else AssetType.EQUITY,
            symbol=symbol,
        )
        for symbol in parse_symbols(symbols)
    ]


def _install_signal_handlers(stop_event: Event) -> None:
    def stop(*_args) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


if __name__ == "__main__":
    main()
