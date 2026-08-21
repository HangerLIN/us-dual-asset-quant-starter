from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from platform_core.infra import IBKRAdapter
from platform_core.schemas import (
    AccountSnapshot,
    AssetType,
    BrokerOrderUpdate,
    ExecutionFill,
    ExecutionRequest,
    InstrumentRef,
    OrderStatus,
    PositionSnapshot,
    RuntimeMode,
)

from .contracts import BrokerEvent

LIVE_CONFIRMATION = "I_UNDERSTAND_LIVE_ORDERS_ARE_REAL"


class IBKRBroker:
    """IBKR order/position adapter shared by paper and live runtimes."""

    def __init__(
        self,
        *,
        mode: RuntimeMode,
        account_id: str,
        adapter: IBKRAdapter | None = None,
        allow_live_trading: bool = False,
        live_confirmation: str = "",
    ) -> None:
        if mode not in {RuntimeMode.PAPER, RuntimeMode.LIVE}:
            raise ValueError("IBKRBroker mode must be PAPER or LIVE")
        if not account_id or account_id == "DU0000000":
            raise ValueError(f"an explicit IBKR {mode.value.lower()} account is required")
        if mode == RuntimeMode.LIVE and (
            not allow_live_trading or live_confirmation != LIVE_CONFIRMATION
        ):
            raise PermissionError(
                "live trading is locked; set ALLOW_LIVE_TRADING=true and the exact "
                "LIVE_TRADING_CONFIRMATION value"
            )
        self.mode = mode
        self.account_id = account_id
        self.adapter = adapter or IBKRAdapter()
        self._requests_by_broker_id: dict[str, ExecutionRequest] = {}
        self._broker_id_by_client_id: dict[str, str] = {}
        self._latest_updates: dict[str, BrokerOrderUpdate] = {}
        self._pending_events: list[BrokerEvent] = []

    def connect(self) -> None:
        self.adapter.connect()
        self._restore_open_orders()

    def disconnect(self) -> None:
        self.adapter.disconnect()

    def submit_order(self, request: ExecutionRequest) -> BrokerOrderUpdate:
        if not request.client_order_id:
            raise ValueError("client_order_id is required before broker submission")
        existing = self._latest_updates.get(request.client_order_id)
        if existing is not None:
            return existing
        broker_order_id = str(self.adapter.submit_order(request, account_id=self.account_id))
        self._requests_by_broker_id[broker_order_id] = request
        self._broker_id_by_client_id[request.client_order_id] = broker_order_id
        update = BrokerOrderUpdate(
            client_order_id=request.client_order_id,
            broker_order_id=broker_order_id,
            status=OrderStatus.SUBMITTED,
            remaining_quantity=request.quantity,
            updated_at=datetime.now(UTC),
        )
        self._latest_updates[request.client_order_id] = update
        self._pending_events.append(update)
        return update

    def cancel_order(self, client_order_id: str) -> BrokerOrderUpdate:
        broker_order_id = self._broker_id_by_client_id.get(client_order_id)
        if broker_order_id is None:
            raise KeyError(f"unknown client_order_id: {client_order_id}")
        self.adapter.cancel_order(int(broker_order_id))
        current = self._latest_updates[client_order_id]
        update = current.model_copy(
            update={"status": OrderStatus.PENDING_CANCEL, "updated_at": datetime.now(UTC)}
        )
        self._latest_updates[client_order_id] = update
        self._pending_events.append(update)
        return update

    def drain_events(self) -> list[BrokerEvent]:
        events = list(self._pending_events)
        self._pending_events.clear()
        for raw in self.adapter.order_updates():
            broker_order_id = str(raw["broker_order_id"])
            request = self._requests_by_broker_id.get(broker_order_id)
            if request is None or request.client_order_id is None:
                continue
            filled_quantity = Decimal(str(raw.get("filled") or 0))
            remaining_quantity = Decimal(str(raw.get("remaining") or 0))
            status = _ibkr_status(raw.get("status"))
            if filled_quantity > 0 and remaining_quantity > 0:
                status = OrderStatus.PARTIALLY_FILLED
            update = BrokerOrderUpdate(
                client_order_id=request.client_order_id,
                broker_order_id=broker_order_id,
                status=status,
                filled_quantity=filled_quantity,
                remaining_quantity=remaining_quantity,
                average_fill_price=_positive_decimal(raw.get("average_fill_price")),
                message=raw.get("message"),
                updated_at=datetime.now(UTC),
            )
            self._latest_updates[request.client_order_id] = update
            events.append(update)
        for raw in self.adapter.execution_updates():
            broker_order_id = str(raw["broker_order_id"])
            request = self._requests_by_broker_id.get(broker_order_id)
            if request is None:
                continue
            events.append(
                ExecutionFill(
                    strategy_code=request.strategy_code,
                    instrument=request.instrument,
                    side="BUY" if str(raw.get("side", "")).upper() in {"BOT", "BUY"} else "SELL",
                    quantity=Decimal(str(raw["quantity"])),
                    fill_price=Decimal(str(raw["fill_price"])),
                    filled_at=_parse_ibkr_time(raw.get("filled_at")),
                    fees=Decimal(str(raw.get("commission") or 0)),
                    execution_id=str(raw["execution_id"]),
                    client_order_id=request.client_order_id,
                    broker_order_id=broker_order_id,
                )
            )
        return events

    def open_orders(self) -> list[BrokerOrderUpdate]:
        terminal = {OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.REJECTED}
        return [update for update in self._latest_updates.values() if update.status not in terminal]

    def positions(self) -> list[PositionSnapshot]:
        snapshots: list[PositionSnapshot] = []
        now = datetime.now(UTC)
        for raw in self.adapter.account_positions(account_id=self.account_id):
            instrument = _instrument_from_contract(raw["contract"])
            quantity = Decimal(str(raw["quantity"]))
            average = Decimal(str(raw["average_cost"])) / instrument.multiplier
            snapshots.append(
                PositionSnapshot(
                    strategy_code="BROKER_ACCOUNT",
                    instrument=instrument,
                    quantity=quantity,
                    avg_open_price=average,
                    mark_price=average,
                    notional=quantity * average * instrument.multiplier,
                    updated_at=now,
                )
            )
        return snapshots

    def account_snapshot(self) -> AccountSnapshot:
        values = self.adapter.account_values(account_id=self.account_id)
        return AccountSnapshot(
            account_id=self.account_id,
            mode=self.mode,
            captured_at=datetime.now(UTC),
            cash=_decimal_or_none(values.get("TotalCashValue")),
            net_liquidation=_decimal_or_none(values.get("NetLiquidation")),
            buying_power=_decimal_or_none(values.get("BuyingPower")),
            realized_pnl=_decimal_or_none(values.get("RealizedPnL")),
            unrealized_pnl=_decimal_or_none(values.get("UnrealizedPnL")),
            values=values,
        )

    def _restore_open_orders(self) -> None:
        for raw in self.adapter.open_orders():
            order = raw["order"]
            client_order_id = str(getattr(order, "orderRef", "") or "")
            if not client_order_id:
                continue
            broker_order_id = str(raw["broker_order_id"])
            request = ExecutionRequest(
                strategy_code=_strategy_from_client_order_id(client_order_id),
                instrument=_instrument_from_contract(raw["contract"]),
                side=str(order.action).upper(),
                quantity=Decimal(str(order.totalQuantity)),
                limit_price=Decimal(str(order.lmtPrice)),
                tif=str(order.tif or "DAY"),
                trace_id=client_order_id,
                client_order_id=client_order_id,
            )
            self._requests_by_broker_id[broker_order_id] = request
            self._broker_id_by_client_id[client_order_id] = broker_order_id
            update = BrokerOrderUpdate(
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                status=_ibkr_status(raw.get("status")),
                remaining_quantity=request.quantity,
                updated_at=datetime.now(UTC),
            )
            self._latest_updates[client_order_id] = update
            self._pending_events.append(update)


def _ibkr_status(value: Any) -> OrderStatus:
    normalized = str(value or "").replace(" ", "").upper()
    return {
        "PENDINGSUBMIT": OrderStatus.PENDING_SUBMIT,
        "PRESUBMITTED": OrderStatus.SUBMITTED,
        "SUBMITTED": OrderStatus.SUBMITTED,
        "PENDINGCANCEL": OrderStatus.PENDING_CANCEL,
        "APICANCELLED": OrderStatus.CANCELLED,
        "CANCELLED": OrderStatus.CANCELLED,
        "FILLED": OrderStatus.FILLED,
        "INACTIVE": OrderStatus.INACTIVE,
        "REJECTED": OrderStatus.REJECTED,
    }.get(normalized, OrderStatus.SUBMITTED)


def _instrument_from_contract(contract: Any) -> InstrumentRef:
    symbol = str(contract.symbol).upper()
    conid = int(contract.conId) if getattr(contract, "conId", 0) else None
    if str(contract.secType).upper() == "OPT":
        expiry = date.fromisoformat(
            f"{contract.lastTradeDateOrContractMonth[:4]}-"
            f"{contract.lastTradeDateOrContractMonth[4:6]}-"
            f"{contract.lastTradeDateOrContractMonth[6:8]}"
        )
        return InstrumentRef(
            asset_type=AssetType.OPTION,
            symbol=symbol,
            conid=conid,
            option_right="CALL" if str(contract.right).upper() in {"C", "CALL"} else "PUT",
            strike=Decimal(str(contract.strike)),
            expiry=expiry,
            currency=str(contract.currency or "USD"),
            venue=str(contract.exchange or "SMART"),
            metadata={"multiplier": str(contract.multiplier or "100")},
        )
    asset_type = AssetType.ETF if symbol in {"SPY", "QQQ", "IWM"} else AssetType.EQUITY
    return InstrumentRef(
        asset_type=asset_type,
        symbol=symbol,
        conid=conid,
        currency=str(contract.currency or "USD"),
        venue=str(contract.exchange or "SMART"),
    )


def _parse_ibkr_time(value: Any) -> datetime:
    text = str(value or "").strip().replace("  ", " ")
    for pattern in ("%Y%m%d %H:%M:%S %Z", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.now(UTC)


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    return Decimal(str(value))


def _strategy_from_client_order_id(client_order_id: str) -> str:
    return client_order_id.rsplit("-", 1)[0]
