from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from platform_core.plugins import (
    BarFeatureState,
    MemoryStrategyCheckpointStore,
    SQLAlchemyStrategyCheckpointStore,
    StrategyCheckpoint,
    StrategyPipeline,
    StrategyRegistry,
    StrategyRuntime,
    StrategyRuntimeConfig,
    StrategyRuntimeMode,
    StrategyRuntimeStatus,
)
from platform_core.schemas import (
    AssetType,
    BarEvent,
    ExecutionRequest,
    InstrumentRef,
    PortfolioDecision,
    SignalEnvelope,
)
from platform_core.sdk import (
    ExecutionResult,
    OrderLifecycleState,
    StrategyOrderEvent,
    StrategyOrderEventPage,
)
from tests.support.execution import _ledger


class RuntimeStrategy:
    strategy_code = "runtime-alpha"

    def __init__(self) -> None:
        self.processed = 0
        self.callbacks: list[str] = []
        self.order_event_ids: list[int] = []

    def process_bar(self, event, *, features, context=None):
        self.processed += 1
        return [
            SignalEnvelope(
                strategy_code=self.strategy_code,
                signal_code="momentum",
                instrument=event.instrument,
                side="BUY",
                confidence=Decimal("0.8"),
                generated_at=event.bar_end,
            )
        ]

    def on_start(self, *, context):
        self.callbacks.append(f"start:{context['mode']}")

    def on_trading_day_start(self, *, context):
        self.callbacks.append("day-start")

    def on_order_event(self, event, *, context):
        self.order_event_ids.append(event.event_id)

    def on_trading_day_end(self, *, context):
        self.callbacks.append("day-end")

    def on_stop(self, *, context):
        self.callbacks.append("stop")

    def snapshot_state(self):
        return {"processed": self.processed, "order_event_ids": self.order_event_ids}

    def restore_state(self, payload):
        self.processed = int(payload.get("processed", 0))
        self.order_event_ids = [int(value) for value in payload.get("order_event_ids", [])]


class RuntimePortfolio:
    def construct(self, signals, *, prices, context=None):
        return [
            PortfolioDecision(
                strategy_code=signal.strategy_code,
                instrument=signal.instrument,
                side="BUY",
                quantity=Decimal(1),
                target_notional=prices[signal.instrument.symbol],
                signal_code=signal.signal_code,
                score=signal.confidence,
            )
            for signal in signals
        ]


class RuntimeExecutionSelector:
    def build_request(self, decision, *, quote=None, context=None):
        return ExecutionRequest(
            strategy_code=decision.strategy_code,
            instrument=decision.instrument,
            side=decision.side,
            quantity=decision.quantity,
            limit_price=Decimal(100),
            signal_code=decision.signal_code,
            trace_id=(context or {}).get("trace_id"),
        )


class RecordingExecutor:
    def __init__(self) -> None:
        self.intents = []

    def submit(self, intent):
        self.intents.append(intent)
        return ExecutionResult(
            client_order_id=intent.client_order_id,
            state=OrderLifecycleState.ACKNOWLEDGED,
        )


class EventSource:
    def __init__(self, events: list[StrategyOrderEvent]) -> None:
        self.events = events

    def order_events(self, *, after_event_id=0, limit=100, wait_seconds=0):
        selected = [event for event in self.events if event.event_id > after_event_id][:limit]
        return StrategyOrderEventPage(
            events=selected,
            next_event_id=selected[-1].event_id if selected else after_event_id,
        )


def _pipeline(strategy: RuntimeStrategy | None = None) -> StrategyPipeline:
    return StrategyPipeline(
        strategy_code="runtime-alpha",
        version="1.2.3",
        signal=strategy or RuntimeStrategy(),
        portfolio=RuntimePortfolio(),
        execution=RuntimeExecutionSelector(),
        features=BarFeatureState(),
    )


def _bar() -> BarEvent:
    return BarEvent(
        instrument=InstrumentRef(asset_type=AssetType.ETF, symbol="SPY"),
        bar_start=datetime(2026, 8, 21, 14, 29, tzinfo=UTC),
        bar_end=datetime(2026, 8, 21, 14, 30, tzinfo=UTC),
        open=Decimal(99),
        high=Decimal(101),
        low=Decimal(98),
        close=Decimal(100),
        volume=1000,
        vwap=Decimal("99.5"),
    )


@pytest.mark.parametrize(
    ("mode", "account"),
    [
        (StrategyRuntimeMode.BACKTEST, None),
        (StrategyRuntimeMode.PAPER, "DU123456"),
        (StrategyRuntimeMode.LIVE, "U123456"),
    ],
)
def test_runtime_reuses_one_callback_pipeline_across_modes(mode, account) -> None:
    strategy = RuntimeStrategy()
    executor = RecordingExecutor()
    runtime = StrategyRuntime(
        pipeline=_pipeline(strategy),
        mode=mode,
        executor=executor,
        checkpoint_store=MemoryStrategyCheckpointStore(),
        account=account,
    )
    runtime.start(trading_date=date(2026, 8, 21))

    result = runtime.process_bar(_bar())

    assert result[0].state == OrderLifecycleState.ACKNOWLEDGED
    assert executor.intents[0].request.account == account
    assert strategy.callbacks[:2] == [f"start:{mode.value}", "day-start"]
    assert runtime.health()["status"] == "RUNNING"


def test_checkpoint_restore_skips_replayed_bar_and_preserves_deterministic_order_id() -> None:
    store = MemoryStrategyCheckpointStore()
    first_executor = RecordingExecutor()
    first = StrategyRuntime(
        pipeline=_pipeline(),
        mode=StrategyRuntimeMode.PAPER,
        executor=first_executor,
        checkpoint_store=store,
        runtime_id="primary",
        account="DU123456",
    )
    first.start(trading_date=date(2026, 8, 21))
    first.process_bar(_bar())
    first_id = first_executor.intents[0].client_order_id

    restored_executor = RecordingExecutor()
    restored_strategy = RuntimeStrategy()
    restored = StrategyRuntime(
        pipeline=_pipeline(restored_strategy),
        mode=StrategyRuntimeMode.PAPER,
        executor=restored_executor,
        checkpoint_store=store,
        runtime_id="primary",
        account="DU123456",
    )
    restored.start(trading_date=date(2026, 8, 21))

    assert restored.process_bar(_bar()) == []
    assert restored_strategy.processed == 1
    assert restored_executor.intents == []

    fresh = StrategyRuntime(
        pipeline=_pipeline(),
        mode=StrategyRuntimeMode.PAPER,
        executor=RecordingExecutor(),
        checkpoint_store=MemoryStrategyCheckpointStore(),
        runtime_id="primary",
        account="DU123456",
    )
    fresh.start(trading_date=date(2026, 8, 21))
    fresh.process_bar(_bar())
    assert fresh.executor.intents[0].client_order_id == first_id


def test_runtime_checkpoints_order_event_cursor_and_lifecycle() -> None:
    now = datetime.now(UTC)
    events = [
        StrategyOrderEvent(
            event_id=number,
            event_type="ORDER_STATUS",
            event_time=now,
            received_at=now,
            client_order_id="runtime-order-001",
            payload={"status": "PartiallyFilled" if number == 7 else "Filled"},
        )
        for number in (7, 8)
    ]
    strategy = RuntimeStrategy()
    store = MemoryStrategyCheckpointStore()
    runtime = StrategyRuntime(
        pipeline=_pipeline(strategy),
        mode=StrategyRuntimeMode.PAPER,
        executor=RecordingExecutor(),
        checkpoint_store=store,
        account="DU123456",
        order_events=EventSource(events),
    )
    runtime.start(trading_date=date(2026, 8, 21))

    assert [event.event_id for event in runtime.poll_order_events()] == [7, 8]
    assert strategy.order_event_ids == [7, 8]
    assert runtime.event_cursor == 8
    assert runtime.poll_order_events() == []
    runtime.pause()
    assert runtime.status == StrategyRuntimeStatus.PAUSED
    runtime.resume()
    runtime.close_trading_day()
    assert runtime.status == StrategyRuntimeStatus.PAUSED
    runtime.stop()
    assert strategy.callbacks[-2:] == ["day-end", "stop"]


def test_registry_and_sql_checkpoint_store_are_version_scoped() -> None:
    registry = StrategyRegistry()
    registry.register(
        strategy_code="runtime-alpha",
        version="1.2.3",
        factory=_pipeline,
    )
    assert registry.versions() == [("runtime-alpha", "1.2.3")]
    assert registry.load("runtime-alpha", "1.2.3").version == "1.2.3"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            strategy_code="runtime-alpha",
            version="1.2.3",
            factory=_pipeline,
        )

    _, factory = _ledger()
    store = SQLAlchemyStrategyCheckpointStore(factory)
    checkpoint = StrategyCheckpoint(
        strategy_code="runtime-alpha",
        strategy_version="1.2.3",
        runtime_id="primary",
        mode=StrategyRuntimeMode.PAPER,
        status=StrategyRuntimeStatus.RUNNING,
        trading_date=date(2026, 8, 21),
        event_cursor=88,
        payload={"strategy": {"counter": 3}},
    )
    store.save(checkpoint)
    loaded = store.load(
        strategy_code="runtime-alpha",
        strategy_version="1.2.3",
        runtime_id="primary",
    )
    assert loaded is not None
    assert loaded.event_cursor == 88
    assert loaded.payload == {"strategy": {"counter": 3}}


def test_registry_discovers_entry_point_and_runtime_loads_typed_config(monkeypatch) -> None:
    from platform_core.plugins import runtime as runtime_module

    class EntryPoint:
        name = "runtime-alpha"

        @staticmethod
        def load():
            return _pipeline

    monkeypatch.setattr(
        runtime_module.metadata,
        "entry_points",
        lambda *, group: [EntryPoint()] if group == "test.strategies" else [],
    )
    registry = StrategyRegistry()

    assert registry.discover(group="test.strategies") == [("runtime-alpha", "1.2.3")]
    runtime = StrategyRuntime.from_config(
        StrategyRuntimeConfig(
            strategy_code="runtime-alpha",
            strategy_version="1.2.3",
            mode=StrategyRuntimeMode.PAPER,
            account="DU123456",
        ),
        registry=registry,
        executor=RecordingExecutor(),
        checkpoint_store=MemoryStrategyCheckpointStore(),
    )
    assert runtime.pipeline.version == "1.2.3"
    assert runtime.mode == StrategyRuntimeMode.PAPER


def test_paper_and_live_runtime_require_explicit_account() -> None:
    with pytest.raises(ValueError, match="require an account"):
        StrategyRuntime(
            pipeline=_pipeline(),
            mode=StrategyRuntimeMode.PAPER,
            executor=RecordingExecutor(),
            checkpoint_store=MemoryStrategyCheckpointStore(),
        )
