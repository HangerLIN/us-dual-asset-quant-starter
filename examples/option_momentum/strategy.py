from __future__ import annotations

from datetime import date
from decimal import Decimal

from platform_core.schemas import BarEvent, SignalEnvelope
from platform_core.schemas.assets import AssetType, InstrumentRef


class OptionMomentumStrategy:
    strategy_code = "option-momentum"

    def process_bar(self, event: BarEvent, *, features: dict, context: dict | None = None) -> list[SignalEnvelope]:
        option = (context or {}).get("selected_option")
        spread_pct = Decimal(str((context or {}).get("option_spread_pct", "0")))
        if option is None or spread_pct > Decimal("0.10"):
            return []
        vwap = event.vwap or features.get("vwap")
        if vwap is None or event.close <= vwap:
            return []
        return [
            SignalEnvelope(
                strategy_code=self.strategy_code,
                signal_code="SIG_OPTION_MOMENTUM_BUY",
                instrument=option,
                side="BUY",
                confidence=Decimal("0.75"),
                generated_at=event.bar_end,
                reason={"underlying": event.instrument.symbol, "spread_pct": str(spread_pct), "score": "0.75"},
            )
        ]


def sample_option(symbol: str = "SPY") -> InstrumentRef:
    return InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol=symbol,
        option_right="CALL",
        strike=Decimal("500"),
        expiry=date(2026, 6, 19),
    )
