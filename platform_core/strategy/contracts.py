from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from platform_core.schemas import (
    AccountSnapshot,
    BarEvent,
    BrokerOrderUpdate,
    ExecutionFill,
    MarketQuote,
    PortfolioDecision,
    PositionSnapshot,
    RuntimeMode,
)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Read-only platform state passed to a strategy on every event."""

    mode: RuntimeMode
    parameters: Mapping[str, Any] = field(default_factory=dict)
    positions: Mapping[str, PositionSnapshot] = field(default_factory=dict)
    quotes: Mapping[str, MarketQuote] = field(default_factory=dict)
    account: AccountSnapshot | None = None


@runtime_checkable
class StrategyPlugin(Protocol):
    """The only contract a separately packaged strategy must implement."""

    strategy_code: str
    strategy_version: str

    def on_start(self, context: StrategyContext) -> None:
        ...

    def on_bar(
        self,
        event: BarEvent,
        context: StrategyContext,
    ) -> Sequence[PortfolioDecision]:
        ...

    def on_quote(
        self,
        event: MarketQuote,
        context: StrategyContext,
    ) -> Sequence[PortfolioDecision]:
        ...

    def on_order_update(
        self,
        update: BrokerOrderUpdate,
        context: StrategyContext,
    ) -> None:
        ...

    def on_fill(
        self,
        fill: ExecutionFill,
        context: StrategyContext,
    ) -> None:
        ...

    def on_stop(self, context: StrategyContext) -> None:
        ...


class BaseStrategy(ABC):
    """Convenience base class; strategy packages may implement the protocol directly."""

    strategy_code: str
    strategy_version = "0.1.0"

    def on_start(self, context: StrategyContext) -> None:
        return None

    @abstractmethod
    def on_bar(
        self,
        event: BarEvent,
        context: StrategyContext,
    ) -> Sequence[PortfolioDecision]:
        raise NotImplementedError

    def on_quote(
        self,
        event: MarketQuote,
        context: StrategyContext,
    ) -> Sequence[PortfolioDecision]:
        return []

    def on_order_update(
        self,
        update: BrokerOrderUpdate,
        context: StrategyContext,
    ) -> None:
        return None

    def on_fill(
        self,
        fill: ExecutionFill,
        context: StrategyContext,
    ) -> None:
        return None

    def on_stop(self, context: StrategyContext) -> None:
        return None
