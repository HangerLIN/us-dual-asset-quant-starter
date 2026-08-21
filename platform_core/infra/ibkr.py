from __future__ import annotations

import socket
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from math import ceil
from threading import Event, Lock, Thread
from time import sleep, time
from typing import Any

from platform_core.core import get_settings
from platform_core.schemas import BarEvent, ExecutionRequest, MarketQuote
from platform_core.schemas.assets import AssetType, InstrumentRef


@dataclass(frozen=True, slots=True)
class IBKRRequest:
    instruments: Sequence[InstrumentRef]
    start: datetime
    end: datetime
    request_type: str


@dataclass(frozen=True, slots=True)
class IBKRAdapterConfig:
    host: str
    port: int
    client_id: int
    market_data_type: int = 4
    request_timeout_seconds: int = 30
    pacing_sleep_seconds: float = 0.25


class IBKRAdapter:
    """Small self-contained IBKR adapter for starter projects.

    It intentionally exposes normalized starter models instead of leaking ibapi
    objects beyond this boundary.
    """

    def __init__(self, config: IBKRAdapterConfig | None = None) -> None:
        settings = get_settings()
        self.config = config or IBKRAdapterConfig(
            host=settings.ib_host,
            port=settings.ib_port,
            client_id=settings.ib_client_id,
            market_data_type=settings.ib_market_data_type,
            request_timeout_seconds=settings.ib_request_timeout_seconds,
            pacing_sleep_seconds=settings.ib_pacing_sleep_seconds,
        )
        self._client: _IBApiClient | None = None
        self._thread: Thread | None = None

    def connect(self) -> None:
        if self._client is not None and self._client.isConnected():
            return
        client = _IBApiClient(timeout_seconds=self.config.request_timeout_seconds)
        client.verify_api_handshake(self.config.host, self.config.port)
        client.connect(self.config.host, self.config.port, self.config.client_id)
        thread = Thread(target=client.run, name="starter-ibkr-client", daemon=True)
        thread.start()
        client.wait_connected()
        client.reqMarketDataType(self.config.market_data_type)
        self._client = client
        self._thread = thread

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.disconnect_and_stop()
        self._client = None
        self._thread = None

    def historical_bars(self, request: IBKRRequest) -> list[BarEvent | MarketQuote]:
        output: list[BarEvent | MarketQuote] = []
        for instrument in request.instruments:
            if instrument.asset_type in {AssetType.EQUITY, AssetType.ETF}:
                output.extend(
                    self.historical_equity_bars(
                        instrument.symbol,
                        start=request.start,
                        end=request.end,
                    )
                )
            elif instrument.asset_type == AssetType.OPTION:
                output.extend(self.historical_option_l1(instrument, start=request.start, end=request.end))
        return output

    def historical_equity_bars(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime,
        bar_size: str = "1 min",
    ) -> list[BarEvent]:
        client = self._ensure_client()
        instrument = InstrumentRef(asset_type=_asset_type_for_symbol(symbol), symbol=symbol.upper())
        contract = _stock_contract(symbol)
        raw_bars = client.request_historical_bars(
            contract=contract,
            end=end,
            duration=_duration(start, end),
            bar_size=bar_size,
            what_to_show="TRADES",
            use_rth=0,
        )
        bars: list[BarEvent] = []
        for raw in raw_bars:
            ts = _parse_ib_datetime(raw["date"])
            if ts < start or ts > end:
                continue
            bars.append(
                BarEvent(
                    instrument=instrument,
                    bar_start=ts,
                    bar_end=ts,
                    open=Decimal(str(raw["open"])),
                    high=Decimal(str(raw["high"])),
                    low=Decimal(str(raw["low"])),
                    close=Decimal(str(raw["close"])),
                    volume=int(raw.get("volume") or 0),
                    vwap=Decimal(str(raw["wap"])) if raw.get("wap") is not None else None,
                )
            )
        sleep(self.config.pacing_sleep_seconds)
        return bars

    def historical_option_l1(
        self,
        contract: InstrumentRef,
        *,
        start: datetime,
        end: datetime,
        bar_size: str = "1 min",
    ) -> list[MarketQuote]:
        client = self._ensure_client()
        raw_bars = client.request_historical_bars(
            contract=_option_contract(contract),
            end=end,
            duration=_duration(start, end),
            bar_size=bar_size,
            what_to_show="BID_ASK",
            use_rth=0,
        )
        quotes: list[MarketQuote] = []
        for raw in raw_bars:
            ts = _parse_ib_datetime(raw["date"])
            if ts < start or ts > end:
                continue
            bid = Decimal(str(raw["low"]))
            ask = Decimal(str(raw["high"]))
            mid = Decimal(str(raw["close"]))
            if ask < bid:
                bid, ask = ask, bid
            quotes.append(
                MarketQuote(
                    instrument=contract,
                    quote_ts=ts,
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    last=mid,
                    volume=int(raw.get("volume") or 0),
                    open_interest=contract.metadata.get("open_interest"),
                    source="ibkr-historical-bid-ask",
                )
            )
        sleep(self.config.pacing_sleep_seconds)
        return quotes

    def snapshot_quote(self, instrument: InstrumentRef) -> MarketQuote:
        client = self._ensure_client()
        quote = client.request_snapshot(_contract_for_instrument(instrument))
        return MarketQuote(
            instrument=instrument,
            quote_ts=datetime.now(UTC),
            bid=_decimal_or_none(quote.get("bid")),
            ask=_decimal_or_none(quote.get("ask")),
            last=_decimal_or_none(quote.get("last")),
            volume=int(quote["volume"]) if quote.get("volume") is not None else None,
            source="ibkr-snapshot",
        )

    def probe_option_contract(
        self,
        symbol: str,
        *,
        as_of: date,
        dte_min: int = 7,
        dte_max: int = 45,
    ) -> InstrumentRef | None:
        contracts = self.option_chain(
            symbol,
            as_of=as_of,
            dte_min=dte_min,
            dte_max=dte_max,
            max_per_side=1,
            max_expiries=1,
            include_quotes=False,
        )
        return contracts[0] if contracts else None

    def contract_details(self, instrument: InstrumentRef) -> list[dict[str, Any]]:
        client = self._ensure_client()
        details = client.request_contract_details(_contract_for_instrument(instrument))
        output: list[dict[str, Any]] = []
        for item in details:
            contract = item.contract
            output.append(
                {
                    "conid": getattr(contract, "conId", None),
                    "symbol": getattr(contract, "symbol", instrument.symbol),
                    "sec_type": getattr(contract, "secType", None),
                    "exchange": getattr(contract, "exchange", None),
                    "primary_exchange": getattr(contract, "primaryExchange", None),
                    "currency": getattr(contract, "currency", None),
                    "local_symbol": getattr(contract, "localSymbol", None),
                }
            )
        return output

    def submit_order(self, request: ExecutionRequest, *, account_id: str) -> int:
        """Submit an already risk-approved request to the configured IBKR session."""
        client = self._ensure_client()
        return client.submit_order(
            contract=_contract_for_instrument(request.instrument),
            order=_ib_order(request, account_id=account_id),
        )

    def cancel_order(self, broker_order_id: int) -> None:
        self._ensure_client().cancel_order(broker_order_id)

    def order_updates(self) -> list[dict[str, Any]]:
        return self._ensure_client().order_updates()

    def execution_updates(self) -> list[dict[str, Any]]:
        return self._ensure_client().execution_updates()

    def account_positions(self, *, account_id: str) -> list[dict[str, Any]]:
        return self._ensure_client().request_positions(account_id=account_id)

    def account_values(self, *, account_id: str) -> dict[str, str]:
        return self._ensure_client().request_account_summary(account_id=account_id)

    def open_orders(self) -> list[dict[str, Any]]:
        return self._ensure_client().request_open_orders()

    def option_chain(
        self,
        symbol: str,
        *,
        as_of: date,
        dte_min: int = 7,
        dte_max: int = 45,
        max_per_side: int = 3,
        max_expiries: int = 1,
        include_quotes: bool = False,
    ) -> list[InstrumentRef]:
        client = self._ensure_client()
        stock = _stock_contract(symbol)
        underlying_details = client.request_contract_details(stock)
        if not underlying_details:
            raise RuntimeError(f"IBKR did not return contract details for {symbol}")
        underlying_conid = int(underlying_details[0].contract.conId)
        params = client.request_sec_def_option_params(
            symbol=symbol.upper(),
            underlying_conid=underlying_conid,
            underlying_sec_type="STK",
        )
        underlying_quote = self.snapshot_quote(InstrumentRef(asset_type=_asset_type_for_symbol(symbol), symbol=symbol.upper()))
        underlying_price = underlying_quote.last or underlying_quote.mid or underlying_quote.bid or underlying_quote.ask
        contracts: list[InstrumentRef] = []
        for param in params:
            expirations = sorted(
                expiry
                for expiry in (_parse_expiry(value) for value in param.get("expirations", []))
                if dte_min <= (expiry - as_of).days <= dte_max
            )
            for expiry in expirations[:max_expiries]:
                strikes = _select_strikes(param.get("strikes", []), underlying_price, max_per_side=max_per_side)
                for right in ("CALL", "PUT"):
                    for strike in strikes:
                        instrument = InstrumentRef(
                            asset_type=AssetType.OPTION,
                            symbol=symbol.upper(),
                            option_right=right,
                            strike=Decimal(str(strike)),
                            expiry=expiry,
                            metadata={"dte": (expiry - as_of).days, "trading_class": param.get("trading_class")},
                        )
                        details = client.request_contract_details(_option_contract(instrument))
                        if not details:
                            continue
                        conid = int(details[0].contract.conId)
                        enriched = instrument.model_copy(update={"conid": conid})
                        if include_quotes:
                            try:
                                quote = self.snapshot_quote(enriched)
                                enriched.metadata.update(
                                    {
                                        "bid": str(quote.bid) if quote.bid is not None else None,
                                        "ask": str(quote.ask) if quote.ask is not None else None,
                                        "mid": str(quote.mid) if quote.mid is not None else None,
                                    }
                                )
                            except Exception as exc:  # noqa: BLE001 - quote is best-effort during chain discovery.
                                enriched.metadata["quote_error"] = str(exc)
                        contracts.append(enriched)
                        sleep(self.config.pacing_sleep_seconds)
        return contracts

    def _ensure_client(self) -> _IBApiClient:
        if self._client is None or not self._client.isConnected():
            self.connect()
        assert self._client is not None
        return self._client


def _ibapi_base_classes() -> tuple[type, ...]:
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
    except ImportError:
        return (object,)
    return (EWrapper, EClient)


def _ibapi_classes() -> tuple[type, type]:
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise RuntimeError("Install the starter with the 'ibkr' extra to use IBKRAdapter: pip install -e '.[ibkr]'") from exc
    return EWrapper, EClient


class _IBApiClient(*_ibapi_base_classes()):
    def __init__(self, *, timeout_seconds: int) -> None:
        wrapper, client = _ibapi_classes()
        wrapper.__init__(self)
        client.__init__(self, self)
        self.timeout_seconds = timeout_seconds
        self._next_req_id = 1000
        self._next_order_id: int | None = None
        self._lock = Lock()
        self._connected_event = Event()
        self._historical: dict[int, list[dict[str, Any]]] = {}
        self._historical_done: dict[int, Event] = {}
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._snapshot_done: dict[int, Event] = {}
        self._contract_details: dict[int, list[Any]] = {}
        self._contract_details_done: dict[int, Event] = {}
        self._option_params: dict[int, list[dict[str, Any]]] = {}
        self._option_params_done: dict[int, Event] = {}
        self._order_updates: list[dict[str, Any]] = []
        self._submitted_order_ids: set[int] = set()
        self._open_orders: list[dict[str, Any]] = []
        self._open_orders_done = Event()
        self._execution_updates: list[dict[str, Any]] = []
        self._delivered_execution_ids: set[str] = set()
        self._positions: list[dict[str, Any]] = []
        self._positions_done = Event()
        self._account_values: dict[int, dict[str, str]] = {}
        self._account_values_done: dict[int, Event] = {}
        self._account_value_filters: dict[int, str] = {}
        self.errors: list[tuple[int, int | None, int, str]] = []

    def nextValidId(self, orderId: int) -> None:
        self._next_order_id = int(orderId)
        self._connected_event.set()

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: float,
        remaining: float,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float = 0.0,
    ) -> None:
        self._order_updates.append(
            {
                "broker_order_id": int(orderId),
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "average_fill_price": avgFillPrice,
                "last_fill_price": lastFillPrice,
                "message": whyHeld or None,
            }
        )

    def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
        self._execution_updates.append(
            {
                "broker_order_id": int(execution.orderId),
                "execution_id": str(execution.execId),
                "quantity": execution.shares,
                "fill_price": execution.price,
                "filled_at": str(execution.time),
                "side": str(execution.side),
                "commission": None,
            }
        )

    def openOrder(
        self,
        orderId: int,
        contract: Any,
        order: Any,
        orderState: Any,
    ) -> None:
        self._open_orders.append(
            {
                "broker_order_id": int(orderId),
                "contract": contract,
                "order": order,
                "status": str(getattr(orderState, "status", "Submitted")),
            }
        )

    def openOrderEnd(self) -> None:
        self._open_orders_done.set()

    def commissionReport(self, commissionReport: Any) -> None:
        execution_id = str(commissionReport.execId)
        for execution in reversed(self._execution_updates):
            if execution["execution_id"] == execution_id:
                execution["commission"] = commissionReport.commission
                break

    def position(self, account: str, contract: Any, position: float, avgCost: float) -> None:
        self._positions.append(
            {
                "account_id": account,
                "contract": contract,
                "quantity": position,
                "average_cost": avgCost,
            }
        )

    def positionEnd(self) -> None:
        self._positions_done.set()

    def accountSummary(
        self,
        reqId: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:
        if account != self._account_value_filters.get(reqId):
            return
        self._account_values.setdefault(reqId, {})[tag] = value

    def accountSummaryEnd(self, reqId: int) -> None:
        self._account_values_done[reqId].set()

    def error(
        self,
        reqId: int,
        errorTime: int | None = None,
        errorCode: int | None = None,
        errorString: str | None = None,
        advancedOrderRejectJson: str = "",
        *args: Any,
    ) -> None:
        if errorString is None and errorCode is not None and isinstance(errorCode, str):
            errorString = errorCode
            errorCode = int(errorTime or -1)
            errorTime = None
        normalized_code = int(errorCode or -1)
        normalized_text = str(errorString or "")
        self.errors.append((int(reqId), errorTime, normalized_code, normalized_text))
        if int(reqId) in self._submitted_order_ids:
            self._order_updates.append(
                {
                    "broker_order_id": int(reqId),
                    "status": "Rejected",
                    "filled": 0,
                    "remaining": 0,
                    "average_fill_price": 0,
                    "last_fill_price": 0,
                    "message": f"{normalized_code}:{normalized_text}",
                }
            )
        self._complete_on_error(int(reqId), normalized_code, normalized_text)

    def historicalData(self, reqId: int, bar: Any) -> None:
        self._historical.setdefault(reqId, []).append(
            {
                "date": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "wap": getattr(bar, "wap", None),
            }
        )

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        self._historical_done[reqId].set()

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
        if price is None or price < 0:
            return
        field = {1: "bid", 2: "ask", 4: "last", 9: "close"}.get(tickType)
        if field:
            self._snapshots.setdefault(reqId, {})[field] = price

    def tickSize(self, reqId: int, tickType: int, size: int) -> None:
        if tickType in {8, 27, 29}:
            self._snapshots.setdefault(reqId, {})["volume"] = size

    def tickSnapshotEnd(self, reqId: int) -> None:
        self._snapshot_done[reqId].set()

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:
        self._contract_details.setdefault(reqId, []).append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:
        self._contract_details_done[reqId].set()

    def securityDefinitionOptionParameter(
        self,
        reqId: int,
        exchange: str,
        underlyingConId: int,
        tradingClass: str,
        multiplier: str,
        expirations: set[str],
        strikes: set[float],
    ) -> None:
        self._option_params.setdefault(reqId, []).append(
            {
                "exchange": exchange,
                "underlying_conid": underlyingConId,
                "trading_class": tradingClass,
                "multiplier": multiplier,
                "expirations": sorted(expirations),
                "strikes": sorted(strikes),
            }
        )

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:
        self._option_params_done[reqId].set()

    def wait_connected(self) -> None:
        if not self._connected_event.wait(self.timeout_seconds):
            detail = self._last_error_text()
            if detail:
                raise TimeoutError(f"IBKR connection timed out before nextValidId: {detail}")
            raise TimeoutError("IBKR connection timed out before nextValidId")

    def currentTime(self, time_: int) -> None:
        return None

    def request_historical_bars(
        self,
        *,
        contract: Any,
        end: datetime,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: int,
    ) -> list[dict[str, Any]]:
        req_id = self._next_id()
        self._historical_done[req_id] = Event()
        self.reqHistoricalData(
            req_id,
            contract,
            _ib_datetime(end),
            duration,
            bar_size,
            what_to_show,
            use_rth,
            2,
            False,
            [],
        )
        self._wait(self._historical_done[req_id], f"historical bars {what_to_show}")
        return self._historical.pop(req_id, [])

    def request_snapshot(self, contract: Any) -> dict[str, Any]:
        req_id = self._next_id()
        self._snapshot_done[req_id] = Event()
        self.reqMktData(req_id, contract, "", True, False, [])
        self._wait(self._snapshot_done[req_id], "snapshot quote")
        return self._snapshots.pop(req_id, {})

    def request_contract_details(self, contract: Any) -> list[Any]:
        req_id = self._next_id()
        self._contract_details_done[req_id] = Event()
        self.reqContractDetails(req_id, contract)
        self._wait(self._contract_details_done[req_id], "contract details")
        return self._contract_details.pop(req_id, [])

    def request_sec_def_option_params(
        self,
        *,
        symbol: str,
        underlying_conid: int,
        underlying_sec_type: str,
    ) -> list[dict[str, Any]]:
        req_id = self._next_id()
        self._option_params_done[req_id] = Event()
        self.reqSecDefOptParams(req_id, symbol, "", underlying_sec_type, underlying_conid)
        self._wait(self._option_params_done[req_id], "option parameters")
        return self._option_params.pop(req_id, [])

    def submit_order(self, *, contract: Any, order: Any) -> int:
        with self._lock:
            if self._next_order_id is None:
                raise RuntimeError("IBKR has not supplied a valid order id")
            order_id = self._next_order_id
            self._next_order_id += 1
            self._submitted_order_ids.add(order_id)
        self.placeOrder(order_id, contract, order)
        return order_id

    def cancel_order(self, broker_order_id: int) -> None:
        self.cancelOrder(int(broker_order_id))

    def order_updates(self) -> list[dict[str, Any]]:
        with self._lock:
            updates = list(self._order_updates)
            self._order_updates.clear()
        return updates

    def execution_updates(self) -> list[dict[str, Any]]:
        with self._lock:
            updates = [
                dict(item)
                for item in self._execution_updates
                if item["commission"] is not None
                and item["execution_id"] not in self._delivered_execution_ids
            ]
            self._delivered_execution_ids.update(item["execution_id"] for item in updates)
        return updates

    def request_positions(self, *, account_id: str) -> list[dict[str, Any]]:
        self._positions = []
        self._positions_done.clear()
        self.reqPositions()
        self._wait(self._positions_done, "positions")
        self.cancelPositions()
        return [item for item in self._positions if item["account_id"] == account_id]

    def request_account_summary(self, *, account_id: str) -> dict[str, str]:
        req_id = self._next_id()
        self._account_values_done[req_id] = Event()
        self._account_value_filters[req_id] = account_id
        self.reqAccountSummary(
            req_id,
            "All",
            "NetLiquidation,TotalCashValue,BuyingPower,RealizedPnL,UnrealizedPnL",
        )
        self._wait(self._account_values_done[req_id], "account summary")
        self.cancelAccountSummary(req_id)
        self._account_value_filters.pop(req_id, None)
        return self._account_values.pop(req_id, {})

    def request_open_orders(self) -> list[dict[str, Any]]:
        self._open_orders = []
        self._open_orders_done.clear()
        self.reqOpenOrders()
        self._wait(self._open_orders_done, "open orders")
        return list(self._open_orders)

    def _next_id(self) -> int:
        with self._lock:
            self._next_req_id += 1
            return self._next_req_id

    def _wait(self, event: Event, label: str) -> None:
        deadline = time() + self.timeout_seconds
        while time() < deadline:
            if event.wait(0.05):
                return
        detail = self._last_error_text()
        if detail:
            raise TimeoutError(f"IBKR request timed out: {label}: {detail}")
        raise TimeoutError(f"IBKR request timed out: {label}")

    def verify_api_handshake(self, host: str, port: int) -> None:
        probe_timeout = min(3.0, max(1.0, float(self.timeout_seconds)))
        try:
            with socket.create_connection((host, int(port)), timeout=min(2.0, probe_timeout)) as sock:
                sock.settimeout(probe_timeout)
                version = "v100..208"
                raw = _make_ib_msg(version)
                sock.sendall(b"API\0" + raw)
                buffer = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    _, message, _ = _read_ib_msg(buffer)
                    if message:
                        fields = _read_ib_fields(message)
                        if len(fields) >= 2:
                            first = fields[0]
                            if isinstance(first, bytes):
                                first = first.decode("ascii", errors="ignore")
                            if str(first).isdigit():
                                return
                        raise ConnectionError(f"IBKR API socket returned unexpected handshake response: {fields!r}")
        except TimeoutError as exc:
            raise TimeoutError(
                f"IBKR API socket accepted TCP but did not respond to the API handshake at {host}:{port}. "
                "Verify TWS/IB Gateway is fully logged in and API socket clients are enabled."
            ) from exc
        except OSError as exc:
            raise ConnectionError(
                f"IBKR API socket is not reachable at {host}:{port}. "
                "Enable ActiveX and Socket EClients in TWS/IB Gateway and verify the socket port."
            ) from exc
        raise ConnectionError(f"IBKR API socket closed before completing the API handshake at {host}:{port}.")

    def _last_error_text(self) -> str | None:
        if not self.errors:
            return None
        req_id, error_time, code, text = self.errors[-1]
        if error_time is not None:
            return f"reqId={req_id} errorTime={error_time} code={code} msg={text}"
        return f"reqId={req_id} code={code} msg={text}"

    def _complete_on_error(self, req_id: int, code: int, text: str) -> None:
        ignorable = {2104, 2106, 2158, 2157}
        if code in ignorable:
            return
        event = self._historical_done.get(req_id)
        if event is not None:
            event.set()
        event = self._snapshot_done.get(req_id)
        if event is not None:
            event.set()
        event = self._contract_details_done.get(req_id)
        if event is not None:
            event.set()
        event = self._option_params_done.get(req_id)
        if event is not None:
            event.set()

    def disconnect_and_stop(self) -> None:
        with suppress(Exception):
            if self.isConnected():
                self.disconnect()


def _stock_contract(symbol: str) -> Any:
    Contract = _contract_class()
    contract = Contract()
    contract.symbol = symbol.upper()
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    return contract


def _option_contract(instrument: InstrumentRef) -> Any:
    Contract = _contract_class()
    contract = Contract()
    contract.symbol = instrument.symbol.upper()
    contract.secType = "OPT"
    contract.exchange = "SMART"
    contract.currency = instrument.currency
    contract.lastTradeDateOrContractMonth = _expiry_yyyymmdd(instrument.expiry)
    contract.strike = float(instrument.strike)
    contract.right = "C" if instrument.option_right == "CALL" else "P"
    contract.multiplier = "100"
    if instrument.conid:
        contract.conId = int(instrument.conid)
    return contract


def _contract_for_instrument(instrument: InstrumentRef) -> Any:
    if instrument.asset_type == AssetType.OPTION:
        return _option_contract(instrument)
    return _stock_contract(instrument.symbol)


def _ib_order(request: ExecutionRequest, *, account_id: str) -> Any:
    try:
        from ibapi.order import Order
    except ImportError as exc:
        raise RuntimeError("Install the 'ibkr' extra to submit orders") from exc
    order = Order()
    order.account = account_id
    order.action = request.side
    order.orderType = "LMT"
    order.totalQuantity = float(request.quantity)
    order.lmtPrice = float(request.limit_price)
    order.tif = request.tif
    order.transmit = True
    order.orderRef = request.client_order_id or request.trace_id or request.strategy_code
    return order


def _contract_class() -> type:
    try:
        from ibapi.contract import Contract
    except ImportError as exc:
        raise RuntimeError("Install the starter with the 'ibkr' extra to use IBKRAdapter: pip install -e '.[ibkr]'") from exc
    return Contract


def _asset_type_for_symbol(symbol: str) -> AssetType:
    return AssetType.ETF if symbol.upper() in {"SPY", "QQQ", "IWM", "DIA"} else AssetType.EQUITY


def _duration(start: datetime, end: datetime) -> str:
    seconds = max(60, int((end - start).total_seconds()))
    if seconds <= 86400:
        return "1 D"
    return f"{ceil(seconds / 86400)} D"


def _ib_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%d %H:%M:%S UTC")


def _parse_ib_datetime(value: Any) -> datetime:
    text = str(value)
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=UTC)
    if len(text) == 8:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
    for fmt in ("%Y%m%d  %H:%M:%S", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return datetime.fromisoformat(text).astimezone(UTC)


def _parse_expiry(value: str) -> date:
    return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")


def _expiry_yyyymmdd(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "")[:8]


def _select_strikes(strikes: Sequence[float], underlying_price: Decimal | None, *, max_per_side: int) -> list[Decimal]:
    decimal_strikes = sorted(Decimal(str(strike)) for strike in strikes if strike and strike > 0)
    if not decimal_strikes:
        return []
    if underlying_price is None:
        return decimal_strikes[: max_per_side * 2]
    below = [strike for strike in decimal_strikes if strike <= underlying_price][-max_per_side:]
    above = [strike for strike in decimal_strikes if strike > underlying_price][:max_per_side]
    return sorted(set(below + above))


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _make_ib_msg(text: str) -> bytes:
    from ibapi import comm

    try:
        return comm.make_msg(text)
    except TypeError:
        return comm.make_msg(0, False, text)


def _read_ib_msg(buffer: bytes) -> tuple[Any, Any, Any]:
    from ibapi import comm

    return comm.read_msg(buffer)


def _read_ib_fields(message: Any) -> Any:
    from ibapi import comm

    return comm.read_fields(message)
