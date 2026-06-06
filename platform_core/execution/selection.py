from __future__ import annotations

from decimal import Decimal

from platform_core.schemas import ExecutionRequest, MarketQuote, PortfolioDecision
from platform_core.schemas.assets import AssetType


class QuoteAwareExecutionSelector:
    def build_request(
        self,
        decision: PortfolioDecision,
        *,
        quote: MarketQuote | None = None,
        trace_id: str | None = None,
    ) -> ExecutionRequest:
        price = Decimal("0")
        if quote is not None:
            if decision.instrument.asset_type == AssetType.OPTION:
                price = quote.ask if decision.side == "BUY" and quote.ask is not None else quote.bid or quote.mid or quote.last or Decimal("0")
            else:
                price = quote.ask or quote.last or quote.mid or quote.bid or Decimal("0")
        if price <= 0:
            price = decision.target_notional / decision.quantity
        return ExecutionRequest(
            strategy_code=decision.strategy_code,
            instrument=decision.instrument,
            side=decision.side,
            quantity=decision.quantity,
            limit_price=price,
            signal_code=decision.signal_code,
            trace_id=trace_id,
        )
