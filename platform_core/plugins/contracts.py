from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Protocol, Sequence

import pandas as pd

from platform_core.schemas import (
    BarEvent,
    ExecutionRequest,
    InstrumentRef,
    MarketQuote,
    PortfolioDecision,
    RiskCheckRequest,
    RiskCheckResult,
    SignalEnvelope,
)
from platform_core.sdk.models import StrategyOrderEvent


class DataIngestionAdapter(Protocol):
    def ingest(
        self,
        *,
        instruments: Sequence[InstrumentRef],
        start: datetime,
        end: datetime,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        ...


class FeatureBuilder(Protocol):
    def build(
        self,
        *,
        bars: pd.DataFrame,
        quotes: Sequence[MarketQuote] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        ...


class CalibrationJob(Protocol):
    def run(self) -> Mapping[str, Any]:
        ...


class SignalPlugin(Protocol):
    strategy_code: str

    def process_bar(
        self,
        event: BarEvent,
        *,
        features: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> list[SignalEnvelope]:
        ...


class StatefulStrategyPlugin(SignalPlugin, Protocol):
    """``StrategyRuntime`` 支持的可选生命周期与状态回调。

    订单事件采用至少一次投递语义，因此实现必须使用 ``event.event_id`` 保证
    ``on_order_event`` 幂等。
    """

    def on_start(self, *, context: Mapping[str, Any]) -> None: ...

    def on_trading_day_start(self, *, context: Mapping[str, Any]) -> None: ...

    def on_order_event(
        self,
        event: StrategyOrderEvent,
        *,
        context: Mapping[str, Any],
    ) -> None: ...

    def on_trading_day_end(self, *, context: Mapping[str, Any]) -> None: ...

    def on_stop(self, *, context: Mapping[str, Any]) -> None: ...

    def snapshot_state(self) -> Mapping[str, Any]: ...

    def restore_state(self, payload: Mapping[str, Any]) -> None: ...


class CandidateSelector(Protocol):
    def select(self, signals: Sequence[SignalEnvelope], *, limit: int | None = None) -> list[SignalEnvelope]:
        ...


class PortfolioConstructor(Protocol):
    def construct(
        self,
        signals: Sequence[SignalEnvelope],
        *,
        prices: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> list[PortfolioDecision]:
        ...


class RiskRulePlugin(Protocol):
    def evaluate(self, request: RiskCheckRequest, *, context: Mapping[str, Any] | None = None) -> RiskCheckResult:
        ...


class ExecutionSelectionPlugin(Protocol):
    def build_request(
        self,
        decision: PortfolioDecision,
        *,
        quote: MarketQuote | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> ExecutionRequest:
        ...


class BacktestStrategyPlugin(Protocol):
    def replay(
        self,
        *,
        start: datetime,
        end: datetime,
        symbols: Sequence[str],
        context: Mapping[str, Any] | None = None,
    ) -> Iterable[SignalEnvelope]:
        ...


class PerformanceReporter(Protocol):
    def build_report(self, *, run_ids: Sequence[int], context: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        ...
