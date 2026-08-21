from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from platform_core.schemas import PortfolioDecision, SignalEnvelope
from platform_core.schemas.assets import AssetType


@dataclass(frozen=True, slots=True)
class AllocationBudget:
    strategy_code: str
    total_notional: Decimal
    max_symbol_notional: Decimal | None = None
    max_asset_notional: dict[AssetType, Decimal] = field(default_factory=dict)
    reserve_cash: Decimal = Decimal(0)


class TopRankCandidateSelector:
    def select(self, signals: Sequence[SignalEnvelope], *, limit: int | None = None) -> list[SignalEnvelope]:
        ordered = sorted(
            signals,
            key=lambda signal: Decimal(str(signal.reason.get("score", signal.confidence))),
            reverse=True,
        )
        return ordered[:limit] if limit is not None else ordered


class EqualWeightPortfolioConstructor:
    def __init__(
        self,
        *,
        budget: AllocationBudget,
        selector: TopRankCandidateSelector | None = None,
        max_candidates: int | None = None,
    ) -> None:
        self.budget = budget
        self.selector = selector or TopRankCandidateSelector()
        self.max_candidates = max_candidates

    def construct(
        self,
        signals: Sequence[SignalEnvelope],
        *,
        prices: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> list[PortfolioDecision]:
        selected = self.selector.select(signals, limit=self.max_candidates)
        decisions: list[PortfolioDecision] = []
        available = max(Decimal(0), self.budget.total_notional - self.budget.reserve_cash)
        count = max(1, len(selected))
        for signal in selected:
            price = Decimal(str(prices.get(signal.instrument.symbol.upper(), "0")))
            if price <= 0:
                continue
            notional = available / Decimal(count)
            if self.budget.max_symbol_notional is not None:
                notional = min(notional, self.budget.max_symbol_notional)
            asset_cap = self.budget.max_asset_notional.get(signal.instrument.asset_type)
            if asset_cap is not None:
                notional = min(notional, asset_cap / Decimal(count))
            unit_notional = price * _asset_multiplier(signal.instrument.asset_type)
            quantity = (notional / unit_notional).quantize(Decimal(1), rounding=ROUND_DOWN)
            if quantity <= 0:
                continue
            decisions.append(
                PortfolioDecision(
                    strategy_code=self.budget.strategy_code,
                    instrument=signal.instrument,
                    side="BUY" if signal.side == "BUY" else "SELL",
                    quantity=quantity,
                    target_notional=quantity * unit_notional,
                    signal_code=signal.signal_code,
                    score=Decimal(str(signal.reason.get("score", signal.confidence))),
                    reason={"constructor": "equal_weight"},
                )
            )
        return decisions


def _asset_multiplier(asset_type: AssetType) -> Decimal:
    return Decimal(100) if asset_type == AssetType.OPTION else Decimal(1)
