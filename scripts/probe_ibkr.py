from __future__ import annotations

import argparse

from _bootstrap import ROOT  # noqa: F401
from _common import json_print

from platform_core.core import get_settings
from platform_core.infra import IBKRAdapter
from platform_core.schemas.assets import AssetType, InstrumentRef


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Probe IBKR connectivity and basic contract resolution.")
    parser.add_argument("--symbol", default="SPY")
    args = parser.parse_args()

    adapter = IBKRAdapter()
    try:
        instrument = InstrumentRef(asset_type=AssetType.ETF, symbol=args.symbol.upper())
        details = adapter.contract_details(instrument)
        quote = adapter.snapshot_quote(instrument)
        json_print(
            {
                "status": "ok",
                "host": settings.ib_host,
                "port": settings.ib_port,
                "client_id": settings.ib_client_id,
                "contract_details_count": len(details),
                "first_contract": details[0] if details else None,
                "quote": quote.model_dump(),
            }
        )
    finally:
        adapter.disconnect()


if __name__ == "__main__":
    main()
