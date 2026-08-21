from __future__ import annotations

from platform_core.core import Settings, get_settings
from platform_core.infra import IBKRAdapter
from platform_core.infra.ibkr import IBKRAdapterConfig
from platform_core.schemas import RuntimeMode

from .contracts import BrokerAdapter
from .ibkr import IBKRBroker
from .simulated import SimulatedBroker


def build_broker(mode: RuntimeMode, settings: Settings | None = None) -> BrokerAdapter:
    settings = settings or get_settings()
    if mode == RuntimeMode.BACKTEST:
        return SimulatedBroker()
    account_id = (
        settings.ib_paper_account if mode == RuntimeMode.PAPER else settings.ib_live_account
    )
    port = settings.ib_paper_port if mode == RuntimeMode.PAPER else settings.ib_live_port
    adapter = IBKRAdapter(
        IBKRAdapterConfig(
            host=settings.ib_host,
            port=port,
            client_id=settings.ib_client_id,
            market_data_type=settings.ib_market_data_type,
            request_timeout_seconds=settings.ib_request_timeout_seconds,
            pacing_sleep_seconds=settings.ib_pacing_sleep_seconds,
        )
    )
    return IBKRBroker(
        mode=mode,
        account_id=account_id,
        adapter=adapter,
        allow_live_trading=settings.allow_live_trading,
        live_confirmation=settings.live_trading_confirmation,
    )
