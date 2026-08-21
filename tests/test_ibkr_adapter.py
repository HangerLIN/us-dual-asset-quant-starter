from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Event

import pytest

from platform_core.infra.ibkr import (
    IBKRAdapter,
    IBKRAdapterConfig,
    IBKRRequestError,
    _duration,
    _expiry_yyyymmdd,
    _IBApiClient,
    _select_strikes,
)
from platform_core.schemas import BrokerOrderRequest
from platform_core.schemas.assets import AssetType, InstrumentRef
from platform_core.sdk import QualifiedContract


def test_ibkr_helper_selects_nearby_strikes() -> None:
    strikes = _select_strikes([490, 495, 500, 505, 510], Decimal(501), max_per_side=2)
    assert strikes == [Decimal(495), Decimal(500), Decimal(505), Decimal(510)]


def test_ibkr_helper_formats_expiry() -> None:
    assert _expiry_yyyymmdd(date(2026, 6, 19)) == "20260619"


def test_ibkr_duration_is_at_least_one_day() -> None:
    start = datetime(2026, 5, 27, 13, 30, tzinfo=UTC)
    end = datetime(2026, 5, 27, 19, 59, tzinfo=UTC)
    assert _duration(start, end) == "1 D"


def test_historical_callback_reads_ibapi_average_as_wap() -> None:
    client = object.__new__(_IBApiClient)
    client._historical = {1001: []}
    client._historical_done = {1001: Event()}
    bar = type(
        "Bar",
        (),
        {
            "date": "1783690200",
            "open": 750.0,
            "high": 751.0,
            "low": 749.0,
            "close": 750.5,
            "volume": 100,
            "average": 750.25,
        },
    )()

    client.historicalData(1001, bar)

    assert client._historical[1001][0]["wap"] == 750.25


def test_option_bid_ask_bars_use_average_bid_and_ask(monkeypatch) -> None:
    class FakeClient:
        def request_historical_bars(self, **kwargs):
            return [
                {
                    "date": "1783690200",
                    "open": 78.91,
                    "high": 82.83,
                    "low": 78.32,
                    "close": 82.37,
                    "volume": -1,
                    "wap": None,
                }
            ]

    adapter = IBKRAdapter(
        IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11, pacing_sleep_seconds=0)
    )
    monkeypatch.setattr(adapter, "_ensure_client", lambda: FakeClient())
    instrument = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=890875189,
        option_right="CALL",
        strike=Decimal("672"),
        expiry=date(2026, 7, 17),
    )

    quotes = adapter.historical_option_l1(
        instrument,
        start=datetime(2026, 7, 10, 13, 30, tzinfo=UTC),
        end=datetime(2026, 7, 10, 20, 0, tzinfo=UTC),
    )

    assert quotes[0].bid == Decimal("78.91")
    assert quotes[0].ask == Decimal("82.37")
    assert quotes[0].mid == Decimal("80.64")
    assert quotes[0].volume is None
    assert quotes[0].last is None


def test_request_errors_are_raised_for_the_matching_request_id() -> None:
    client = object.__new__(_IBApiClient)
    client.timeout_seconds = 1
    client._historical_done = {1001: Event(), 1002: Event()}
    client._snapshot_done = {}
    client._contract_details_done = {}
    client._option_params_done = {}
    client._request_errors = {1001: [], 1002: []}

    client._complete_on_error(1001, 10089, "market data subscription required")

    with pytest.raises(IBKRRequestError, match="code=10089"):
        client._wait(client._historical_done[1001], "historical bars", req_id=1001)
    assert client._request_errors[1002] == []


def test_fractional_size_compatibility_warning_is_not_fatal() -> None:
    client = object.__new__(_IBApiClient)
    client._historical_done = {1001: Event()}
    client._snapshot_done = {}
    client._contract_details_done = {}
    client._option_params_done = {}
    client._request_errors = {1001: []}

    client._complete_on_error(1001, 2176, "fractional size adjusted")

    assert client._request_errors[1001] == []
    assert not client._historical_done[1001].is_set()


def test_oca_transmits_every_peer_with_the_same_group(monkeypatch) -> None:
    captured = []

    class BatchCaptured(Exception):
        pass

    class FakeClient:
        @staticmethod
        def reserve_order_ids(count):
            return list(range(100, 100 + count))

        @staticmethod
        def submit_order_batch(batch):
            captured.extend(batch)
            raise BatchCaptured

    adapter = IBKRAdapter(
        IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11)
    )
    monkeypatch.setattr(adapter, "_resolve_account", lambda _: "DU123456")
    monkeypatch.setattr(adapter, "_assert_order_safe", lambda _: None)
    monkeypatch.setattr(adapter, "_ensure_client", lambda: FakeClient())
    requests = [
        BrokerOrderRequest(
            instrument=InstrumentRef(
                asset_type=AssetType.EQUITY,
                symbol=symbol,
                conid=conid,
            ),
            side="BUY",
            quantity=Decimal("1"),
            limit_price=Decimal("100"),
            account="DU123456",
        )
        for symbol, conid in (("AAPL", 265598), ("MSFT", 272093))
    ]

    with pytest.raises(BatchCaptured):
        adapter.place_oca(requests, oca_group="oca-safe-group", oca_type=1)

    assert [order.transmit for _, _, order in captured] == [True, True]
    assert [order.ocaGroup for _, _, order in captured] == [
        "oca-safe-group",
        "oca-safe-group",
    ]


def test_combo_snapshot_falls_back_to_conservative_leg_nbbo(monkeypatch) -> None:
    expiry = date(2026, 9, 18)
    long_call = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=6001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=expiry,
    )
    short_call = long_call.model_copy(update={"conid": 6002, "strike": Decimal(505)})
    combo = InstrumentRef(
        asset_type=AssetType.COMBO,
        symbol="SPY",
        metadata={
            "combo_legs": [
                {
                    "conid": 6001,
                    "ratio": 1,
                    "action": "BUY",
                    "instrument": long_call.model_dump(mode="json"),
                },
                {
                    "conid": 6002,
                    "ratio": 1,
                    "action": "SELL",
                    "instrument": short_call.model_dump(mode="json"),
                },
            ]
        },
    )
    now = datetime.now(UTC)

    class FakeClient:
        @staticmethod
        def request_snapshot(contract):
            if contract.secType == "BAG":
                raise IBKRRequestError(
                    req_id=1001,
                    code=10197,
                    message="multiple sessions",
                )
            prices = {
                6001: {"bid": "4.00", "ask": "4.20"},
                6002: {"bid": "2.00", "ask": "2.10"},
            }[contract.conId]
            return {
                **prices,
                "quote_ts": now,
                "market_data_type": 1,
                "halted_status": 0,
            }

    adapter = IBKRAdapter(
        IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11)
    )
    monkeypatch.setattr(adapter, "_ensure_client", lambda: FakeClient())

    quote = adapter.snapshot_quote(combo)

    assert quote.bid == Decimal("1.90")
    assert quote.ask == Decimal("2.20")
    assert quote.source == "ibkr-synthetic-combo-nbbo"
    assert quote.market_data_type == 1


def test_combo_qualification_merges_leg_rules_without_requesting_bag_details(
    monkeypatch,
) -> None:
    expiry = date(2026, 9, 18)
    first = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=7001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=expiry,
    )
    second = first.model_copy(update={"conid": 7002, "strike": Decimal(505)})
    combo = InstrumentRef(
        asset_type=AssetType.COMBO,
        symbol="SPY",
        metadata={
            "combo_legs": [
                {
                    "conid": first.conid,
                    "ratio": 1,
                    "action": "BUY",
                    "instrument": first.model_dump(mode="json"),
                },
                {
                    "conid": second.conid,
                    "ratio": 1,
                    "action": "SELL",
                    "instrument": second.model_dump(mode="json"),
                },
            ]
        },
    )
    adapter = IBKRAdapter(
        IBKRAdapterConfig(host="127.0.0.1", port=7497, client_id=11)
    )

    def qualify_leg(leg):
        assert leg.asset_type == AssetType.OPTION
        return QualifiedContract(
            instrument=leg,
            valid_exchanges=["SMART", "CBOE"],
            supported_order_types=["LMT", "MKT"],
            min_tick=Decimal("0.05") if leg.conid == 7001 else Decimal("0.01"),
            min_size=Decimal(1),
            size_increment=Decimal(1),
            time_zone_id="US/Eastern",
        )

    monkeypatch.setattr(adapter, "qualify_contract", qualify_leg)

    qualified = adapter._qualify_combo_contract(combo)

    assert qualified.instrument == combo
    assert qualified.min_tick == Decimal("0.05")
    assert qualified.supported_order_types == ["LMT", "MKT"]
    assert qualified.valid_exchanges == ["CBOE", "SMART"]
