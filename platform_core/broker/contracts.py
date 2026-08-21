from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeAlias, runtime_checkable

from platform_core.schemas import (
    AccountSnapshot,
    BrokerOrderUpdate,
    ExecutionFill,
    ExecutionRequest,
    PositionSnapshot,
    RuntimeMode,
)

BrokerEvent: TypeAlias = BrokerOrderUpdate | ExecutionFill


@runtime_checkable
class BrokerAdapter(Protocol):
    mode: RuntimeMode
    account_id: str

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def submit_order(self, request: ExecutionRequest) -> BrokerOrderUpdate:
        ...

    def cancel_order(self, client_order_id: str) -> BrokerOrderUpdate:
        ...

    def drain_events(self) -> list[BrokerEvent]:
        ...

    def open_orders(self) -> Sequence[BrokerOrderUpdate]:
        ...

    def positions(self) -> Sequence[PositionSnapshot]:
        ...

    def account_snapshot(self) -> AccountSnapshot:
        ...
