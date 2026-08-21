from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from platform_core.schemas import ExecutionRequest, MarketQuote, PortfolioDecision


class QuoteAwareExecutionSelector:
    def build_request(
        self,
        decision: PortfolioDecision,
        *,
        quote: MarketQuote | None = None,
        trace_id: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> ExecutionRequest:
        trace_id = trace_id or str((context or {}).get("trace_id") or "") or None
        price = Decimal("0")
        if quote is not None:
            if decision.side == "BUY":
                price = quote.ask or quote.mid or quote.last or quote.bid or Decimal("0")
            else:
                price = quote.bid or quote.mid or quote.last or quote.ask or Decimal("0")
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
