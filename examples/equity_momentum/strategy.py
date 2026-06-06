from __future__ import annotations

from decimal import Decimal

from platform_core.schemas import BarEvent, SignalEnvelope
from platform_core.schemas.assets import AssetType, InstrumentRef


class EquityMomentumStrategy:
    strategy_code = "equity-momentum"

    def process_bar(self, event: BarEvent, *, features: dict, context: dict | None = None) -> list[SignalEnvelope]:
        if event.instrument.asset_type not in {AssetType.EQUITY, AssetType.ETF}:
            return []
        vwap = event.vwap or features.get("vwap")
        rvol = Decimal(str(features.get("rvol", "1")))
        if vwap is None or event.close <= vwap or rvol < Decimal("1.5"):
            return []
        score = min(Decimal("1"), rvol / Decimal("5"))
        return [
            SignalEnvelope(
                strategy_code=self.strategy_code,
                signal_code="SIG_EQUITY_MOMENTUM_BUY",
                instrument=event.instrument,
                side="BUY",
                confidence=score,
                generated_at=event.bar_end,
                reason={"above_vwap": True, "rvol": str(rvol), "score": str(score)},
            )
        ]


def sample_instrument(symbol: str = "SPY") -> InstrumentRef:
    return InstrumentRef(asset_type=AssetType.ETF, symbol=symbol)
