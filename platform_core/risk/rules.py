from __future__ import annotations

from decimal import Decimal

from platform_core.schemas import RiskCheckRequest, RiskCheckResult
from platform_core.schemas.assets import AssetType


class BasicRiskEngine:
    def __init__(
        self,
        *,
        notional_cap: Decimal,
        option_spread_pct_max: Decimal = Decimal("0.10"),
    ) -> None:
        self.notional_cap = notional_cap
        self.option_spread_pct_max = option_spread_pct_max

    def evaluate(self, request: RiskCheckRequest) -> RiskCheckResult:
        if request.notional > self.notional_cap:
            return RiskCheckResult(
                approved=False,
                code="BLOCK:NOTIONAL_CAP",
                detail="requested notional exceeds cap",
            )
        if request.instrument.asset_type == AssetType.OPTION and request.quote:
            spread_pct = request.quote.get("spread_pct")
            if spread_pct is not None and Decimal(str(spread_pct)) > self.option_spread_pct_max:
                return RiskCheckResult(
                    approved=False,
                    code="BLOCK:OPTION_SPREAD",
                    detail="option spread too wide",
                    reasons={"spread_pct": str(spread_pct)},
                )
        return RiskCheckResult(approved=True)
