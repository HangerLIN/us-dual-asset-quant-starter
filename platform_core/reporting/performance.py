from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal


def summarize_fills(fills: Iterable[Mapping[str, object]]) -> dict[str, object]:
    total_fees = Decimal(0)
    gross_notional = Decimal(0)
    count = 0
    by_asset: dict[str, int] = {}
    for fill in fills:
        count += 1
        asset_type = str(fill.get("asset_type") or "UNKNOWN")
        by_asset[asset_type] = by_asset.get(asset_type, 0) + 1
        total_fees += Decimal(str(fill.get("fees") or "0"))
        gross_notional += abs(Decimal(str(fill.get("quantity") or "0"))) * Decimal(
            str(fill.get("fill_price") or "0")
        )
    return {
        "fill_count": count,
        "gross_notional": str(gross_notional),
        "fees": str(total_fees),
        "by_asset": by_asset,
    }
