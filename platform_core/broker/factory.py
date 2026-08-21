from __future__ import annotations

from platform_core.core import Settings, get_settings
from platform_core.schemas import RuntimeMode

from .contracts import BrokerAdapter
from .simulated import SimulatedBroker


def build_broker(mode: RuntimeMode, settings: Settings | None = None) -> BrokerAdapter:
    _ = settings or get_settings()
    if mode == RuntimeMode.BACKTEST:
        return SimulatedBroker()
    raise PermissionError(
        "策略运行时不得直连 IBKR；PAPER/LIVE 必须使用 StrategyExecutionClient 访问 exec_svc"
    )
