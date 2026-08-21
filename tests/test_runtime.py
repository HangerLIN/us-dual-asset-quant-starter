from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from platform_core.broker import SimulatedBroker, build_broker
from platform_core.data import QuoteBarAggregator
from platform_core.db import Base, get_engine, get_session_factory
from platform_core.db.models import Fill, Order, Position
from platform_core.execution import OrderManager
from platform_core.risk import BasicRiskEngine
from platform_core.runtime import InMemoryEventBus, TradingEngine
from platform_core.schemas import (
    AssetType,
    BarEvent,
    ExecutionRequest,
    InstrumentRef,
    MarketQuote,
    OrderStatus,
    RuntimeMode,
)
from platform_core.strategy import load_strategy
from scripts.run_smoke import main as smoke_main
from tests.support import BuyOnceTestStrategy


def test_strategy_loader_loads_external_plugin() -> None:
    strategy = load_strategy("tests.support:BuyOnceTestStrategy", {"notional": "2500"})
    assert strategy.strategy_code == "test-buy-once"
    assert strategy.parameters == {"notional": "2500"}


def test_runtime_persists_order_fill_position_and_events(tmp_path) -> None:
    session = _session(tmp_path)
    broker = SimulatedBroker(initial_cash=Decimal(10_000))
    bus = InMemoryEventBus()
    strategy = BuyOnceTestStrategy({"notional": "1000"})
    engine = TradingEngine(
        strategy=strategy,
        broker=broker,
        order_manager=OrderManager(session=session, broker=broker),
        risk=BasicRiskEngine(notional_cap=Decimal(5_000)),
        event_bus=bus,
    )
    bar, quote = _bar_and_quote()
    engine.start()
    result = engine.process_bar(bar, quote=quote)
    engine.stop()
    session.commit()

    assert result.order_updates[0].status == OrderStatus.FILLED
    assert session.scalar(select(Order)) is not None
    assert session.scalar(select(Fill)) is not None
    assert session.scalar(select(Position)) is not None
    assert len(strategy.fills) == 1
    assert {event.event_type for event in bus.events} >= {
        "BAR_RECEIVED",
        "PORTFOLIO_DECISION",
        "ORDER_SUBMITTED",
        "ORDER_UPDATE",
        "FILL",
    }
    session.close()


def test_order_manager_is_idempotent(tmp_path) -> None:
    session = _session(tmp_path)
    broker = SimulatedBroker()
    broker.connect()
    manager = OrderManager(session=session, broker=broker)
    bar, _ = _bar_and_quote()
    request = ExecutionRequest(
        strategy_code="test",
        instrument=bar.instrument,
        side="BUY",
        quantity=Decimal(1),
        limit_price=Decimal(100),
        client_order_id="stable-id",
    )
    first = manager.submit(request)
    second = manager.submit(request)
    assert first.client_order_id == second.client_order_id
    assert session.scalars(select(Order)).all().__len__() == 1
    broker.disconnect()
    session.close()


def test_runtime_recomputes_notional_instead_of_trusting_strategy(tmp_path) -> None:
    session = _session(tmp_path)
    broker = SimulatedBroker()
    strategy = _MisreportedNotionalStrategy()
    engine = TradingEngine(
        strategy=strategy,
        broker=broker,
        order_manager=OrderManager(session=session, broker=broker),
        risk=BasicRiskEngine(notional_cap=Decimal(5_000)),
    )
    bar, quote = _bar_and_quote()
    engine.start()
    result = engine.process_bar(bar, quote=quote)
    engine.stop()
    assert result.risk_results[0].code == "BLOCK:NOTIONAL_CAP"
    assert not result.order_updates
    session.close()


def test_runtime_rejects_stale_quote(tmp_path) -> None:
    session = _session(tmp_path)
    broker = SimulatedBroker()
    engine = TradingEngine(
        strategy=BuyOnceTestStrategy(),
        broker=broker,
        order_manager=OrderManager(session=session, broker=broker),
        risk=BasicRiskEngine(notional_cap=Decimal(5_000), max_quote_age_seconds=30),
    )
    bar, quote = _bar_and_quote()
    stale_quote = quote.model_copy(update={"quote_ts": quote.quote_ts - timedelta(minutes=1)})
    engine.start()
    result = engine.process_bar(bar, quote=stale_quote)
    engine.stop()
    assert result.risk_results[0].code == "BLOCK:STALE_QUOTE"
    session.close()


@pytest.mark.parametrize("mode", [RuntimeMode.PAPER, RuntimeMode.LIVE])
def test_strategy_runtime_cannot_build_direct_ibkr_broker(mode) -> None:
    with pytest.raises(PermissionError, match="StrategyExecutionClient"):
        build_broker(mode)


def test_quote_bar_aggregator_emits_completed_bar() -> None:
    _, quote = _bar_and_quote()
    aggregator = QuoteBarAggregator(timeframe_seconds=60)
    assert aggregator.update(quote) is None
    next_quote = quote.model_copy(
        update={
            "quote_ts": datetime(2026, 5, 27, 13, 31, tzinfo=UTC),
            "last": Decimal(101),
        }
    )
    completed = aggregator.update(next_quote)
    assert completed is not None
    assert completed.close == Decimal(100)


def test_offline_smoke_requires_no_bundled_strategy(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "smoke.db"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_smoke.py",
            "--mode",
            "offline",
            "--db-url",
            f"sqlite:///{db_path}",
            "--symbols",
            "SPY",
        ],
    )
    smoke_main()
    captured = capsys.readouterr()
    assert '"backtest": null' in captured.out
    assert '"status": "COMPLETED"' in captured.out


def _session(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'runtime.db'}"
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)
    return get_session_factory(db_url)()


def _bar_and_quote() -> tuple[BarEvent, MarketQuote]:
    now = datetime(2026, 5, 27, 13, 30, tzinfo=UTC)
    instrument = InstrumentRef(asset_type=AssetType.ETF, symbol="SPY")
    bar = BarEvent(
        instrument=instrument,
        bar_start=now,
        bar_end=now,
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal(100),
        volume=1000,
    )
    quote = MarketQuote(
        instrument=instrument,
        quote_ts=now,
        bid=Decimal("99.99"),
        ask=Decimal("100.01"),
        last=Decimal(100),
    )
    return bar, quote


class _MisreportedNotionalStrategy(BuyOnceTestStrategy):
    strategy_code = "test-misreported-notional"

    def on_bar(self, event, context):
        decisions = super().on_bar(event, context)
        return [
            decisions[0].model_copy(
                update={"quantity": Decimal(1000), "target_notional": Decimal(1)}
            )
        ]
