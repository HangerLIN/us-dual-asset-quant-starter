from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from importlib import metadata
import json
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from platform_core.db.models import StrategyCheckpointRecord
from platform_core.schemas import (
    BarEvent,
    BrokerOrderRequest,
    ExecutionRequest,
    MarketQuote,
    PortfolioDecision,
    SignalEnvelope,
)
from platform_core.sdk.models import (
    ExecutionResult,
    LiveOrderIntent,
    StrategyOrderEvent,
    StrategyOrderEventPage,
)

from .contracts import ExecutionSelectionPlugin, PortfolioConstructor, SignalPlugin


class StrategyRuntimeMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class StrategyRuntimeStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class StrategyRuntimeConfig:
    strategy_code: str
    strategy_version: str
    mode: StrategyRuntimeMode
    runtime_id: str = "default"
    account: str | None = None

    def __post_init__(self) -> None:
        identities = (self.strategy_code, self.strategy_version, self.runtime_id)
        if any(not value.strip() or len(value) > 64 for value in identities):
            raise ValueError("strategy code, version, and runtime ID must contain 1-64 characters")
        if self.mode in {StrategyRuntimeMode.PAPER, StrategyRuntimeMode.LIVE} and not self.account:
            raise ValueError("PAPER/LIVE strategy runtime configuration requires an account")


class FeatureState(Protocol):
    def update(self, event: BarEvent) -> Mapping[str, Any]: ...

    def snapshot_state(self) -> Mapping[str, Any]: ...

    def restore_state(self, payload: Mapping[str, Any]) -> None: ...


class IntentExecutor(Protocol):
    def submit(self, intent: LiveOrderIntent) -> ExecutionResult: ...


class OrderEventSource(Protocol):
    def order_events(
        self,
        *,
        after_event_id: int = 0,
        limit: int = 100,
        wait_seconds: float = 0,
    ) -> StrategyOrderEventPage: ...


@dataclass(frozen=True, slots=True)
class StrategyPipeline:
    strategy_code: str
    version: str
    signal: SignalPlugin
    portfolio: PortfolioConstructor
    execution: ExecutionSelectionPlugin
    features: FeatureState

    def __post_init__(self) -> None:
        if not self.strategy_code.strip() or len(self.strategy_code) > 64:
            raise ValueError("strategy_code must contain between 1 and 64 characters")
        if not self.version.strip() or len(self.version) > 64:
            raise ValueError("strategy version must contain between 1 and 64 characters")
        if self.signal.strategy_code != self.strategy_code:
            raise ValueError("signal plugin strategy_code differs from pipeline registration")


class StrategyRegistry:
    """使用显式且不可变策略版本的进程内注册表。"""

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], Callable[[], StrategyPipeline]] = {}
        self._lock = RLock()

    def register(
        self,
        *,
        strategy_code: str,
        version: str,
        factory: Callable[[], StrategyPipeline],
    ) -> None:
        key = (strategy_code.strip(), version.strip())
        if not all(key) or any(len(value) > 64 for value in key):
            raise ValueError("strategy code and version are required")
        with self._lock:
            if key in self._factories:
                raise ValueError(f"strategy version already registered: {key[0]}@{key[1]}")
            pipeline = factory()
            if (pipeline.strategy_code, pipeline.version) != key:
                raise ValueError("strategy factory identity differs from registry key")
            self._factories[key] = factory

    def load(self, strategy_code: str, version: str) -> StrategyPipeline:
        key = (strategy_code.strip(), version.strip())
        with self._lock:
            factory = self._factories.get(key)
        if factory is None:
            raise LookupError(f"strategy version is not registered: {key[0]}@{key[1]}")
        pipeline = factory()
        if (pipeline.strategy_code, pipeline.version) != key:
            raise RuntimeError("registered strategy factory returned a different identity")
        return pipeline

    def versions(self, strategy_code: str | None = None) -> list[tuple[str, str]]:
        with self._lock:
            keys = list(self._factories)
        if strategy_code is not None:
            keys = [key for key in keys if key[0] == strategy_code]
        return sorted(keys)

    def discover(self, *, group: str = "us_dual_asset.strategies") -> list[tuple[str, str]]:
        """加载 Python 包入口点发布的策略流水线工厂。"""

        discovered: list[tuple[str, str]] = []
        for entry_point in metadata.entry_points(group=group):
            factory = entry_point.load()
            if not callable(factory):
                raise TypeError(f"strategy entry point is not callable: {entry_point.name}")
            pipeline = factory()
            if not isinstance(pipeline, StrategyPipeline):
                raise TypeError(
                    f"strategy entry point did not return StrategyPipeline: {entry_point.name}"
                )
            self.register(
                strategy_code=pipeline.strategy_code,
                version=pipeline.version,
                factory=factory,
            )
            discovered.append((pipeline.strategy_code, pipeline.version))
        return sorted(discovered)


@dataclass(slots=True)
class StrategyCheckpoint:
    strategy_code: str
    strategy_version: str
    runtime_id: str
    mode: StrategyRuntimeMode
    status: StrategyRuntimeStatus
    trading_date: date | None = None
    event_cursor: int = 0
    last_market_event_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    heartbeat_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class StrategyCheckpointStore(Protocol):
    def load(
        self, *, strategy_code: str, strategy_version: str, runtime_id: str
    ) -> StrategyCheckpoint | None: ...

    def save(self, checkpoint: StrategyCheckpoint) -> None: ...


class MemoryStrategyCheckpointStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], StrategyCheckpoint] = {}
        self._lock = RLock()

    def load(
        self, *, strategy_code: str, strategy_version: str, runtime_id: str
    ) -> StrategyCheckpoint | None:
        with self._lock:
            value = self._items.get((strategy_code, strategy_version, runtime_id))
            return None if value is None else _copy_checkpoint(value)

    def save(self, checkpoint: StrategyCheckpoint) -> None:
        with self._lock:
            self._items[
                (checkpoint.strategy_code, checkpoint.strategy_version, checkpoint.runtime_id)
            ] = _copy_checkpoint(checkpoint)


class SQLAlchemyStrategyCheckpointStore:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def load(
        self, *, strategy_code: str, strategy_version: str, runtime_id: str
    ) -> StrategyCheckpoint | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(StrategyCheckpointRecord).where(
                    StrategyCheckpointRecord.strategy_code == strategy_code,
                    StrategyCheckpointRecord.strategy_version == strategy_version,
                    StrategyCheckpointRecord.runtime_id == runtime_id,
                )
            )
            if row is None:
                return None
            return StrategyCheckpoint(
                strategy_code=row.strategy_code,
                strategy_version=row.strategy_version,
                runtime_id=row.runtime_id,
                mode=StrategyRuntimeMode(row.mode),
                status=StrategyRuntimeStatus(row.status),
                trading_date=row.trading_date,
                event_cursor=row.event_cursor,
                last_market_event_at=_as_utc(row.last_market_event_at),
                payload=dict(row.payload or {}),
                heartbeat_at=_as_utc(row.heartbeat_at),
            )

    def save(self, checkpoint: StrategyCheckpoint) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(StrategyCheckpointRecord).where(
                    StrategyCheckpointRecord.strategy_code == checkpoint.strategy_code,
                    StrategyCheckpointRecord.strategy_version == checkpoint.strategy_version,
                    StrategyCheckpointRecord.runtime_id == checkpoint.runtime_id,
                )
            )
            values = {
                "mode": checkpoint.mode.value,
                "status": checkpoint.status.value,
                "trading_date": checkpoint.trading_date,
                "event_cursor": checkpoint.event_cursor,
                "last_market_event_at": checkpoint.last_market_event_at,
                "payload": checkpoint.payload,
                "heartbeat_at": checkpoint.heartbeat_at,
                "updated_at": now,
            }
            if row is None:
                session.add(
                    StrategyCheckpointRecord(
                        strategy_code=checkpoint.strategy_code,
                        strategy_version=checkpoint.strategy_version,
                        runtime_id=checkpoint.runtime_id,
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(row, key, value)


class BarFeatureState:
    """适用于 SDK 示例和冒烟运行的轻量默认特征状态。"""

    def __init__(self) -> None:
        self._last: dict[str, Any] = {}

    def update(self, event: BarEvent) -> Mapping[str, Any]:
        self._last = {
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "close": event.close,
            "volume": event.volume,
            "vwap": event.vwap,
        }
        return dict(self._last)

    def snapshot_state(self) -> Mapping[str, Any]:
        return _json_safe(self._last)

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        self._last = dict(payload)


class StrategyRuntime:
    """模式无关的策略运行器，唯一写输出是 ``LiveOrderIntent``。"""

    def __init__(
        self,
        *,
        pipeline: StrategyPipeline,
        mode: StrategyRuntimeMode,
        executor: IntentExecutor,
        checkpoint_store: StrategyCheckpointStore,
        runtime_id: str = "default",
        account: str | None = None,
        order_events: OrderEventSource | None = None,
    ) -> None:
        if not runtime_id.strip() or len(runtime_id) > 64:
            raise ValueError("runtime_id must contain between 1 and 64 characters")
        if mode in {StrategyRuntimeMode.PAPER, StrategyRuntimeMode.LIVE} and not account:
            raise ValueError("PAPER/LIVE strategy runtimes require an account")
        self.pipeline = pipeline
        self.mode = mode
        self.executor = executor
        self.checkpoint_store = checkpoint_store
        self.runtime_id = runtime_id
        self.account = account
        self.order_event_source = order_events or (
            executor if hasattr(executor, "order_events") else None
        )
        self.status = StrategyRuntimeStatus.CREATED
        self.trading_date: date | None = None
        self.event_cursor = 0
        self.last_market_event_at: datetime | None = None
        self.last_heartbeat_at: datetime | None = None
        self.last_error: str | None = None
        self._lock = RLock()

    @classmethod
    def from_config(
        cls,
        config: StrategyRuntimeConfig,
        *,
        registry: StrategyRegistry,
        executor: IntentExecutor,
        checkpoint_store: StrategyCheckpointStore,
        order_events: OrderEventSource | None = None,
    ) -> "StrategyRuntime":
        return cls(
            pipeline=registry.load(config.strategy_code, config.strategy_version),
            mode=config.mode,
            executor=executor,
            checkpoint_store=checkpoint_store,
            runtime_id=config.runtime_id,
            account=config.account,
            order_events=order_events,
        )

    def start(self, *, trading_date: date) -> None:
        with self._lock:
            if self.status == StrategyRuntimeStatus.RUNNING:
                return
            restored = self.checkpoint_store.load(
                strategy_code=self.pipeline.strategy_code,
                strategy_version=self.pipeline.version,
                runtime_id=self.runtime_id,
            )
            if restored is not None:
                if restored.mode != self.mode:
                    raise ValueError("checkpoint mode differs from requested runtime mode")
                self.event_cursor = restored.event_cursor
                self.last_market_event_at = restored.last_market_event_at
                self._restore_payload(restored.payload)
            self.trading_date = trading_date
            self.status = StrategyRuntimeStatus.RUNNING
            self.last_error = None
            self._callback("on_start")
            self._callback("on_trading_day_start")
            self._checkpoint()

    def pause(self) -> None:
        with self._lock:
            self._require_status(StrategyRuntimeStatus.RUNNING)
            self.status = StrategyRuntimeStatus.PAUSED
            self._checkpoint()

    def resume(self) -> None:
        with self._lock:
            self._require_status(StrategyRuntimeStatus.PAUSED)
            self.status = StrategyRuntimeStatus.RUNNING
            self.last_error = None
            self._checkpoint()

    def close_trading_day(self) -> None:
        with self._lock:
            if self.status not in {
                StrategyRuntimeStatus.RUNNING,
                StrategyRuntimeStatus.PAUSED,
            }:
                raise RuntimeError("strategy runtime has no active trading day")
            self._callback("on_trading_day_end")
            self.trading_date = None
            self.status = StrategyRuntimeStatus.PAUSED
            self._checkpoint()

    def stop(self) -> None:
        with self._lock:
            if self.status == StrategyRuntimeStatus.STOPPED:
                return
            self._callback("on_stop")
            self.status = StrategyRuntimeStatus.STOPPED
            self._checkpoint()

    def heartbeat(self) -> datetime:
        with self._lock:
            if self.status not in {
                StrategyRuntimeStatus.RUNNING,
                StrategyRuntimeStatus.PAUSED,
            }:
                raise RuntimeError("strategy runtime is not active")
            self.last_heartbeat_at = datetime.now(UTC)
            self._checkpoint()
            return self.last_heartbeat_at

    def health(self) -> dict[str, Any]:
        return {
            "strategy_code": self.pipeline.strategy_code,
            "strategy_version": self.pipeline.version,
            "runtime_id": self.runtime_id,
            "mode": self.mode.value,
            "status": self.status.value,
            "trading_date": self.trading_date,
            "event_cursor": self.event_cursor,
            "last_market_event_at": self.last_market_event_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_error": self.last_error,
        }

    def process_bar(
        self,
        event: BarEvent,
        *,
        quote: MarketQuote | None = None,
        prices: Mapping[str, Decimal] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> list[ExecutionResult]:
        with self._lock:
            self._require_status(StrategyRuntimeStatus.RUNNING)
            event_at = _as_utc(event.bar_end)
            if self.last_market_event_at is not None and event_at <= self.last_market_event_at:
                return []
            runtime_context = self._context(context)
            try:
                features = self.pipeline.features.update(event)
                signals = self.pipeline.signal.process_bar(
                    event,
                    features=features,
                    context=runtime_context,
                )
                self._assert_signals(signals)
                selected_prices = dict(prices or {})
                if event.instrument.symbol not in selected_prices:
                    selected_prices[event.instrument.symbol] = event.close
                decisions = self.pipeline.portfolio.construct(
                    signals,
                    prices=selected_prices,
                    context=runtime_context,
                )
                results = [
                    self.executor.submit(
                        self._intent_for_decision(
                            decision,
                            event=event,
                            quote=quote,
                            ordinal=ordinal,
                            context=runtime_context,
                        )
                    )
                    for ordinal, decision in enumerate(decisions)
                ]
                self.last_market_event_at = event_at
                self.last_heartbeat_at = datetime.now(UTC)
                self.last_error = None
                self._checkpoint()
                return results
            except Exception as exc:
                self.status = StrategyRuntimeStatus.FAILED
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._checkpoint()
                raise

    def poll_order_events(
        self,
        *,
        limit: int = 100,
        wait_seconds: float = 0,
    ) -> list[StrategyOrderEvent]:
        with self._lock:
            if self.order_event_source is None:
                raise RuntimeError("strategy runtime has no order-event source")
            if self.status not in {
                StrategyRuntimeStatus.RUNNING,
                StrategyRuntimeStatus.PAUSED,
            }:
                raise RuntimeError("strategy runtime is not active")
            page = self.order_event_source.order_events(
                after_event_id=self.event_cursor,
                limit=limit,
                wait_seconds=wait_seconds,
            )
            for event in page.events:
                self._callback("on_order_event", event)
                self.event_cursor = event.event_id
                # 回调是 at-least-once；逐事件落游标，避免一页末尾失败时整页重放。
                self._checkpoint()
            self.last_heartbeat_at = datetime.now(UTC)
            self._checkpoint()
            return page.events

    def _intent_for_decision(
        self,
        decision: PortfolioDecision,
        *,
        event: BarEvent,
        quote: MarketQuote | None,
        ordinal: int,
        context: Mapping[str, Any],
    ) -> LiveOrderIntent:
        if decision.strategy_code != self.pipeline.strategy_code:
            raise ValueError("portfolio decision belongs to another strategy")
        trace_id = self.deterministic_client_order_id(
            decision=decision,
            event=event,
            ordinal=ordinal,
        )
        selected = self.pipeline.execution.build_request(
            decision,
            quote=quote,
            context={**context, "trace_id": trace_id},
        )
        if selected.strategy_code != self.pipeline.strategy_code:
            raise ValueError("execution request belongs to another strategy")
        request = _broker_request(selected, account=self.account)
        return LiveOrderIntent(
            client_order_id=trace_id,
            strategy_code=self.pipeline.strategy_code,
            request=request,
            created_at=_as_utc(event.bar_end),
            metadata={
                "strategy_version": self.pipeline.version,
                "runtime_id": self.runtime_id,
                "runtime_mode": self.mode.value,
                "signal_code": decision.signal_code,
            },
        )

    def deterministic_client_order_id(
        self,
        *,
        decision: PortfolioDecision,
        event: BarEvent,
        ordinal: int,
    ) -> str:
        payload = {
            "strategy_code": self.pipeline.strategy_code,
            "strategy_version": self.pipeline.version,
            "runtime_id": self.runtime_id,
            "mode": self.mode.value,
            "account": self.account,
            "trading_date": self.trading_date.isoformat() if self.trading_date else None,
            "bar_end": _as_utc(event.bar_end).isoformat(),
            "instrument": decision.instrument.model_dump(mode="json"),
            "side": decision.side,
            "quantity": str(decision.quantity),
            "signal_code": decision.signal_code,
            "ordinal": ordinal,
        }
        digest = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        prefix = self.pipeline.strategy_code[:24]
        session = self.trading_date.strftime("%Y%m%d") if self.trading_date else "nodate"
        return f"{prefix}-{session}-{digest}"

    def _context(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "strategy_code": self.pipeline.strategy_code,
            "strategy_version": self.pipeline.version,
            "runtime_id": self.runtime_id,
            "mode": self.mode.value,
            "account": self.account,
            "trading_date": self.trading_date,
            **dict(extra or {}),
        }

    def _callback(self, name: str, *args: Any) -> None:
        callback = getattr(self.pipeline.signal, name, None)
        if callback is not None:
            callback(*args, context=self._context())

    def _assert_signals(self, signals: list[SignalEnvelope]) -> None:
        if any(signal.strategy_code != self.pipeline.strategy_code for signal in signals):
            raise ValueError("signal plugin emitted a signal for another strategy")

    def _checkpoint(self) -> None:
        now = datetime.now(UTC)
        self.last_heartbeat_at = self.last_heartbeat_at or now
        self.checkpoint_store.save(
            StrategyCheckpoint(
                strategy_code=self.pipeline.strategy_code,
                strategy_version=self.pipeline.version,
                runtime_id=self.runtime_id,
                mode=self.mode,
                status=self.status,
                trading_date=self.trading_date,
                event_cursor=self.event_cursor,
                last_market_event_at=self.last_market_event_at,
                payload=self._snapshot_payload(),
                heartbeat_at=self.last_heartbeat_at,
            )
        )

    def _snapshot_payload(self) -> dict[str, Any]:
        strategy_snapshot = getattr(self.pipeline.signal, "snapshot_state", None)
        feature_snapshot = getattr(self.pipeline.features, "snapshot_state", None)
        return _json_safe(
            {
                "strategy": strategy_snapshot() if strategy_snapshot else {},
                "features": feature_snapshot() if feature_snapshot else {},
            }
        )

    def _restore_payload(self, payload: Mapping[str, Any]) -> None:
        restore_strategy = getattr(self.pipeline.signal, "restore_state", None)
        if restore_strategy is not None:
            restore_strategy(dict(payload.get("strategy") or {}))
        restore_features = getattr(self.pipeline.features, "restore_state", None)
        if restore_features is not None:
            restore_features(dict(payload.get("features") or {}))

    def _require_status(self, expected: StrategyRuntimeStatus) -> None:
        if self.status != expected:
            raise RuntimeError(
                f"strategy runtime must be {expected.value}; got {self.status.value}"
            )


def _broker_request(request: ExecutionRequest, *, account: str | None) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        instrument=request.instrument,
        side=request.side,
        quantity=request.quantity,
        order_type="LMT",
        limit_price=request.limit_price,
        tif=request.tif,
        account=account,
        order_ref=None,
    )


def _copy_checkpoint(checkpoint: StrategyCheckpoint) -> StrategyCheckpoint:
    return StrategyCheckpoint(
        strategy_code=checkpoint.strategy_code,
        strategy_version=checkpoint.strategy_version,
        runtime_id=checkpoint.runtime_id,
        mode=checkpoint.mode,
        status=checkpoint.status,
        trading_date=checkpoint.trading_date,
        event_cursor=checkpoint.event_cursor,
        last_market_event_at=checkpoint.last_market_event_at,
        payload=json.loads(json.dumps(_json_safe(checkpoint.payload))),
        heartbeat_at=checkpoint.heartbeat_at,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
