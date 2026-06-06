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
