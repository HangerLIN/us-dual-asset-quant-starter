from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import platform_core.infra.ibkr as ibkr_module
from platform_core.infra.ibkr import (
    IBKRAdapter,
    IBKRAdapterConfig,
    _broker_execution,
    _IBApiClient,
    _order_record_matches,
    _raw_order_record,
)
from platform_core.schemas import AssetType, BrokerOrderRequest, InstrumentRef
from platform_core.sdk.safety import TradingSafetyError


def _instrument() -> InstrumentRef:
    return InstrumentRef(asset_type=AssetType.ETF, symbol="SPY")


def _request(**updates) -> BrokerOrderRequest:
    values = {
        "instrument": _instrument(),
        "side": "BUY",
        "quantity": Decimal(1),
        "order_type": "LMT",
        "limit_price": Decimal(700),
        "account": "DU123456",
        "order_ref": "test-order",
    }
    values.update(updates)
    return BrokerOrderRequest(**values)


def _contract() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="SPY",
        localSymbol="SPY",
        secType="STK",
        currency="USD",
        exchange="SMART",
        conId=756733,
    )


def _order() -> SimpleNamespace:
    return SimpleNamespace(
        account="DU123456",
        action="BUY",
        orderType="LMT",
        totalQuantity=Decimal(1),
        lmtPrice=700.0,
        auxPrice=float("inf"),
        tif="DAY",
        filledQuantity=0,
        permId=0,
        clientId=11,
        parentId=0,
        orderRef="test-order",
        whatIf=False,
    )


def test_broker_order_request_validates_prices() -> None:
    with pytest.raises(ValidationError, match="LMT order requires limit_price"):
        _request(limit_price=None)
    with pytest.raises(ValidationError, match="STP order requires stop_price"):
        _request(order_type="STP", limit_price=None)


def test_ibkr_order_disables_deprecated_routing_flags() -> None:
    pytest.importorskip("ibapi.order")
    order = ibkr_module._broker_order(_request())

    assert order.eTradeOnly is False
    assert order.firmQuoteOnly is False


@pytest.mark.parametrize(
    ("order_request", "ib_order_type", "limit_price", "stop_price"),
    [
        (_request(order_type="MKT", limit_price=None), "MKT", None, None),
        (_request(order_type="LMT"), "LMT", Decimal(700), None),
        (
            _request(order_type="STP", limit_price=None, stop_price=Decimal(770)),
            "STP",
            None,
            Decimal(770),
        ),
        (
            _request(order_type="STP_LMT", limit_price=Decimal(771), stop_price=Decimal(770)),
            "STP LMT",
            Decimal(771),
            Decimal(770),
        ),
    ],
)
def test_ibkr_serializes_supported_order_types(
    order_request: BrokerOrderRequest,
    ib_order_type: str,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
) -> None:
    pytest.importorskip("ibapi.order")
    order = ibkr_module._broker_order(order_request)

    assert order.orderType == ib_order_type
    if limit_price is not None:
        assert Decimal(str(order.lmtPrice)) == limit_price
    if stop_price is not None:
        assert Decimal(str(order.auxPrice)) == stop_price


def test_adapter_resolves_and_guards_paper_account(monkeypatch) -> None:
    adapter = IBKRAdapter(IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11))
    monkeypatch.setattr(adapter, "managed_accounts", lambda: ["DU123456"])
    assert adapter.require_paper_account() == "DU123456"

    monkeypatch.setattr(adapter, "managed_accounts", lambda: ["U123456"])
    with pytest.raises(PermissionError, match="non-paper"):
        adapter.require_paper_account()


def test_adapter_places_normalized_order_with_session_account(monkeypatch) -> None:
    class FakeClient:
        def request_managed_accounts(self):
            return ["DU123456"]

        def submit_order(self, *, contract, order, order_id):
            assert order.account == "DU123456"
            assert order.limit_price == Decimal(700)
            assert order_id is None
            return {
                "order_id": 42,
                "status": "Submitted",
                "instrument": order.instrument,
                "account": order.account,
                "side": order.side,
                "order_type": order.order_type,
                "quantity": order.quantity,
                "limit_price": order.limit_price,
                "updated_at": datetime.now(UTC),
            }

    adapter = IBKRAdapter(IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11))
    monkeypatch.setattr(adapter, "_ensure_client", lambda: FakeClient())
    monkeypatch.setattr(ibkr_module, "_broker_order", lambda request: request)

    status = adapter.place_order(_request(account=None))

    assert status.order_id == 42
    assert status.status == "Submitted"
    assert status.account == "DU123456"


def test_configured_execution_boundary_rejects_direct_adapter_order(
    monkeypatch,
) -> None:
    class FakeClient:
        def request_managed_accounts(self):
            return ["DU123456"]

        def submit_order(self, *, contract, order, order_id):
            return {
                "order_id": 42,
                "status": "Submitted",
                "instrument": order.instrument,
                "account": order.account,
                "side": order.side,
                "order_type": order.order_type,
                "quantity": order.quantity,
                "limit_price": order.limit_price,
                "updated_at": datetime.now(UTC),
            }

    adapter = IBKRAdapter(IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11))
    monkeypatch.setattr(adapter, "_ensure_client", lambda: FakeClient())
    monkeypatch.setattr(ibkr_module, "_broker_order", lambda request: request)
    boundary = object()
    adapter.configure_execution_boundary(boundary)

    with pytest.raises(TradingSafetyError, match="submit through ExecutionSDK"):
        adapter.place_order(_request(account=None))
    with pytest.raises(TradingSafetyError, match="submit through ExecutionSDK"):
        adapter.cancel_order(42, account="DU123456")

    assert adapter.place_order(_request(account=None), execution_token=boundary).order_id == 42


def test_execution_boundary_rejects_every_direct_broker_mutation() -> None:
    adapter = IBKRAdapter(IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11))
    adapter.configure_execution_boundary(object())
    option = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=12345,
        option_right="CALL",
        strike=Decimal(500),
        expiry=datetime(2026, 9, 18, tzinfo=UTC).date(),
    )
    mutations = {
        "place": lambda: adapter.place_order(_request()),
        "bracket": lambda: adapter.place_bracket(
            entry=_request(), take_profit=_request(), stop_loss=_request()
        ),
        "oca": lambda: adapter.place_oca([_request(), _request()], oca_group="safe-oca"),
        "replace": lambda: adapter.replace_order(42, _request()),
        "cancel": lambda: adapter.cancel_order(42, account="DU123456"),
        "cancel_all": lambda: adapter.cancel_all_orders(
            account="DU123456",
            confirmation="CANCEL-OWNED:DU123456",
        ),
        "exercise": lambda: adapter.exercise_option(
            instrument=option,
            action="EXERCISE",
            quantity=Decimal(1),
            account="DU123456",
            confirmation="EXERCISE:DU123456:12345:1",
        ),
    }

    for name, mutation in mutations.items():
        with pytest.raises(
            TradingSafetyError,
            match="submit through ExecutionSDK",
        ) as denied:
            mutation()
        assert name not in str(denied.value)


def test_low_level_order_acknowledgement_and_cancellation() -> None:
    client = object.__new__(_IBApiClient)
    client.timeout_seconds = 1
    client._lock = Lock()
    client._next_order_id = 50
    client._order_id_event = Event()
    client._orders = {}
    client._order_events = {}
    client._open_orders = {}
    client._collecting_open_orders = False
    client._request_errors = {}
    client.errors = []
    contract = _contract()
    order = _order()
    order_state = SimpleNamespace(
        status="Submitted",
        whyHeld="",
        initMarginChange="100",
        maintMarginChange="80",
        equityWithLoanChange="-100",
        warningText="",
    )

    def place_order(order_id, submitted_contract, submitted_order):
        client.openOrder(order_id, submitted_contract, submitted_order, order_state)
        client.orderStatus(order_id, "Submitted", 0, 1, 0, 9001, 0, 0, 11, "", 0)

    def cancel_order(order_id):
        client.orderStatus(order_id, "Cancelled", 0, 1, 0, 9001, 0, 0, 11, "", 0)

    client.placeOrder = place_order
    client.cancelOrder = cancel_order

    submitted = client.submit_order(contract=contract, order=order, order_id=None)
    cancelled = client.request_cancel_order(submitted["order_id"])

    assert submitted["order_id"] == 50
    assert submitted["status"] == "Submitted"
    assert submitted["remaining"] == 1
    assert cancelled["status"] == "Cancelled"


def test_order_acknowledgement_must_match_modified_limit_price() -> None:
    order = _order()
    row = _raw_order_record(
        50,
        _contract(),
        order,
        SimpleNamespace(
            status="PreSubmitted",
            whyHeld="",
            initMarginChange="",
            maintMarginChange="",
            equityWithLoanChange="",
            warningText="",
        ),
    )
    modified = _order()
    modified.lmtPrice = 699.99

    assert _order_record_matches(row, order)
    assert not _order_record_matches(row, modified)


def test_ibkr_cancel_code_202_is_a_terminal_status_not_a_request_failure() -> None:
    client = object.__new__(_IBApiClient)
    client._orders = {50: {"order_id": 50, "status": "PendingCancel"}}
    client._order_events = {50: Event()}
    client._request_errors = {50: []}

    client._complete_on_error(50, 202, "order cancelled")

    assert client._orders[50]["status"] == "Cancelled"
    assert client._request_errors[50] == []
    assert client._order_events[50].is_set()


def test_completed_order_uses_completed_status_and_fill_quantities() -> None:
    client = object.__new__(_IBApiClient)
    client._completed_orders = {}
    client._collecting_completed_orders = True
    order = _order()
    order.orderId = 51
    order.totalQuantity = Decimal(2)
    order.filledQuantity = Decimal(2)
    order_state = SimpleNamespace(
        status="",
        completedStatus="Filled",
        whyHeld="",
        initMarginChange="",
        maintMarginChange="",
        equityWithLoanChange="",
        warningText="",
    )

    client.completedOrder(_contract(), order, order_state)

    row = client._completed_orders[51]
    assert row["status"] == "Filled"
    assert row["filled"] == Decimal(2)
    assert row["remaining"] == Decimal(0)


def test_execution_and_commission_are_normalized() -> None:
    execution = SimpleNamespace(
        execId="0001.01",
        orderId=51,
        permId=9001,
        clientId=11,
        acctNumber="DU123456",
        side="BOT",
        shares=Decimal(1),
        price=Decimal("763.25"),
        time="20260821 09:30:01 US/Eastern",
        exchange="ARCA",
        orderRef="test-order",
    )
    normalized = _broker_execution(
        {
            "execution": execution,
            "contract": _contract(),
            "commission_report": {
                "commission": Decimal("0.35"),
                "currency": "USD",
                "realized_pnl": Decimal(0),
            },
        }
    )

    assert normalized.execution_id == "0001.01"
    assert normalized.side == "BUY"
    assert normalized.executed_at == datetime(2026, 8, 21, 13, 30, 1, tzinfo=UTC)
    assert normalized.commission == Decimal("0.35")
