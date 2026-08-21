from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from platform_core.schemas import RiskCheckRequest, RiskCheckResult
from platform_core.schemas.assets import AssetType


class BasicRiskEngine:
    def __init__(
        self,
        *,
        notional_cap: Decimal,
        option_spread_pct_max: Decimal = Decimal("0.10"),
        daily_loss_limit: Decimal | None = None,
        gross_exposure_cap: Decimal | None = None,
        max_quote_age_seconds: int = 30,
        trading_enabled: bool = True,
    ) -> None:
        self.notional_cap = notional_cap
        self.option_spread_pct_max = option_spread_pct_max
        self.daily_loss_limit = daily_loss_limit
        self.gross_exposure_cap = gross_exposure_cap
        self.max_quote_age_seconds = max_quote_age_seconds
        self.trading_enabled = trading_enabled

    def evaluate(
        self,
        request: RiskCheckRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> RiskCheckResult:
        context = context or {}
        if not self.trading_enabled or context.get("kill_switch"):
            return RiskCheckResult(
                approved=False,
                code="BLOCK:TRADING_DISABLED",
                detail="trading is disabled by the platform kill switch",
            )
        if request.notional > self.notional_cap:
            return RiskCheckResult(
                approved=False,
                code="BLOCK:NOTIONAL_CAP",
                detail="requested notional exceeds cap",
            )
        quote_ts_value = (request.quote or {}).get("quote_ts")
        if quote_ts_value is not None:
            quote_ts = datetime.fromisoformat(str(quote_ts_value))
            age_seconds = (request.timestamp - quote_ts).total_seconds()
            if age_seconds > self.max_quote_age_seconds:
                return RiskCheckResult(
                    approved=False,
                    code="BLOCK:STALE_QUOTE",
                    detail="quote is too old for order submission",
                    reasons={"age_seconds": str(age_seconds)},
                )
        if request.instrument.asset_type == AssetType.OPTION:
            spread_pct = (request.quote or {}).get("spread_pct")
            if spread_pct is None:
                return RiskCheckResult(
                    approved=False,
                    code="BLOCK:OPTION_TWO_SIDED_QUOTE_REQUIRED",
                    detail="option trading requires a valid bid and ask",
                )
            if Decimal(str(spread_pct)) > self.option_spread_pct_max:
                return RiskCheckResult(
                    approved=False,
                    code="BLOCK:OPTION_SPREAD",
                    detail="option spread too wide",
                    reasons={"spread_pct": str(spread_pct)},
                )
        daily_pnl = Decimal(str(context.get("daily_pnl", 0)))
        if self.daily_loss_limit is not None and daily_pnl <= -abs(self.daily_loss_limit):
            return RiskCheckResult(
                approved=False,
                code="BLOCK:DAILY_LOSS",
                detail="daily loss limit reached",
                reasons={"daily_pnl": str(daily_pnl)},
            )
        gross_exposure = Decimal(str(context.get("gross_exposure", 0)))
        if (
            self.gross_exposure_cap is not None
            and gross_exposure + request.notional > self.gross_exposure_cap
        ):
            return RiskCheckResult(
                approved=False,
                code="BLOCK:GROSS_EXPOSURE",
                detail="gross exposure cap would be exceeded",
                reasons={"gross_exposure": str(gross_exposure)},
            )
        return RiskCheckResult(approved=True)
