from __future__ import annotations

from decimal import Decimal

from examples.equity_momentum import EquityMomentumStrategy
from examples.option_momentum import OptionMomentumStrategy
from platform_core.portfolio import AllocationBudget, EqualWeightPortfolioConstructor
from platform_core.schemas import BarEvent, PortfolioDecision, SignalEnvelope
from platform_core.schemas.assets import AssetType


class DualAssetMomentumStrategy:
    strategy_code = "dual-asset-momentum"

    def __init__(self) -> None:
        self.equity = EquityMomentumStrategy()
        self.option = OptionMomentumStrategy()

    def process_bar(self, event: BarEvent, *, features: dict, context: dict | None = None) -> list[SignalEnvelope]:
        signals = self.equity.process_bar(event, features=features, context=context)
        signals.extend(self.option.process_bar(event, features=features, context=context))
        for signal in signals:
            signal.strategy_code = self.strategy_code
        return signals

    def construct_portfolio(
        self,
        signals: list[SignalEnvelope],
        *,
        prices: dict[str, Decimal],
        total_notional: Decimal = Decimal("10000"),
    ) -> list[PortfolioDecision]:
        constructor = EqualWeightPortfolioConstructor(
            budget=AllocationBudget(
                strategy_code=self.strategy_code,
                total_notional=total_notional,
                max_asset_notional={
                    AssetType.EQUITY: total_notional * Decimal("0.5"),
                    AssetType.ETF: total_notional * Decimal("0.5"),
                    AssetType.OPTION: total_notional * Decimal("0.5"),
                },
            ),
            max_candidates=4,
        )
        return constructor.construct(signals, prices=prices)
