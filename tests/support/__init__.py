"""共享的确定性测试替身与工厂。"""

from __future__ import annotations

from decimal import Decimal

from platform_core.schemas import BarEvent, PortfolioDecision
from platform_core.strategy import BaseStrategy, StrategyContext


class BuyOnceTestStrategy(BaseStrategy):
    strategy_code = "test-buy-once"
    strategy_version = "1.0.0"

    def __init__(self, parameters: dict | None = None) -> None:
        self.parameters = parameters or {}
        self.fired = False
        self.fills = []

    def on_bar(
        self,
        event: BarEvent,
        context: StrategyContext,
    ) -> list[PortfolioDecision]:
        if self.fired:
            return []
        self.fired = True
        notional = Decimal(str(self.parameters.get("notional", "1000")))
        quantity = max(Decimal(1), (notional / event.close).to_integral_value())
        return [
            PortfolioDecision(
                strategy_code=self.strategy_code,
                instrument=event.instrument,
                side="BUY",
                quantity=quantity,
                target_notional=quantity * event.close,
                signal_code="TEST_BUY_ONCE",
                score=Decimal(1),
            )
        ]

    def on_fill(self, fill, context: StrategyContext) -> None:
        self.fills.append(fill)
