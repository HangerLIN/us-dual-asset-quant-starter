from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from math import ceil, isfinite
from threading import Event, Lock, RLock, Thread
from time import sleep, time
from typing import Any

from platform_core.core import get_settings
from platform_core.schemas import (
    BarEvent,
    BrokerAccountValue,
    BrokerExecution,
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerPnL,
    BrokerPosition,
    MarketQuote,
)
from platform_core.schemas.assets import AssetType, InstrumentRef
from platform_core.sdk.models import (
    AccountRiskSnapshot,
    BrokerEvent,
    BrokerEventType,
    BrokerSessionState,
    QualifiedContract,
)
from platform_core.sdk.safety import TradingSafetyController, TradingSafetyError


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
    account: str | None = None
    market_data_type: int = 4
    request_timeout_seconds: int = 30
    pacing_sleep_seconds: float = 0.25
    minimum_server_version: int = 150


class IBKRRequestError(RuntimeError):
    def __init__(
        self,
        *,
        req_id: int,
        code: int,
        message: str,
        advanced_order_reject_json: str | None = None,
    ) -> None:
        self.req_id = req_id
        self.code = code
        self.message = message
        self.advanced_order_reject_json = advanced_order_reject_json
        detail = f"IBKR request failed: reqId={req_id} code={code} msg={message}"
        if advanced_order_reject_json:
            detail += f" advanced={advanced_order_reject_json}"
        super().__init__(detail)


class IBKRNoDataError(RuntimeError):
    pass


BrokerEventHandler = Callable[[BrokerEvent], None]


class IBKRAdapter:
    """适用于基础项目的轻量自包含 IBKR 适配器。

    该边界只暴露规范化领域模型，不向外泄漏 ibapi 对象。
    """

    def __init__(
        self,
        config: IBKRAdapterConfig | None = None,
        *,
        safety: TradingSafetyController | None = None,
        event_handlers: Sequence[BrokerEventHandler] = (),
    ) -> None:
        settings = get_settings()
        self.config = config or IBKRAdapterConfig(
            host=settings.ib_host,
            port=settings.ib_port,
            # 未指定角色时只使用行情 client ID，避免意外占用唯一执行连接。
            client_id=settings.ib_market_data_client_id,
            account=settings.ib_account if settings.ib_account != "DU0000000" else None,
            market_data_type=settings.ib_market_data_type,
            request_timeout_seconds=settings.ib_request_timeout_seconds,
            pacing_sleep_seconds=settings.ib_pacing_sleep_seconds,
            minimum_server_version=settings.ib_min_server_version,
        )
        self._client: _IBApiClient | None = None
        self._thread: Thread | None = None
        self._safety = safety
        self._event_handlers = list(event_handlers)
        self._state_lock = RLock()
        self._session_state = BrokerSessionState.DISCONNECTED
        self._pnl_subscriptions: dict[int, str] = {}
        self._auto_open_orders_enabled = False
        self._market_data_farm_healthy: bool | None = None
        self._degraded_by_market_data = False
        self._execution_boundary_token: object | None = None

    @property
    def session_state(self) -> BrokerSessionState:
        with self._state_lock:
            return self._session_state

    @property
    def market_data_type(self) -> int | None:
        client = self._client
        return client.current_market_data_type if client is not None else None

    @property
    def market_data_farm_healthy(self) -> bool | None:
        return self._market_data_farm_healthy

    def heartbeat(self) -> datetime:
        timestamp = self._ensure_client().request_current_time()
        return datetime.fromtimestamp(timestamp, tz=UTC)

    def capabilities(self) -> dict[str, int | bool | None]:
        client = self._ensure_client()
        server_version = int(client.serverVersion())
        return {
            "server_version": server_version,
            "minimum_server_version": self.config.minimum_server_version,
            "completed_orders": server_version >= 150,
            "order_binding": server_version >= 144,
            "pnl": server_version >= 127,
            "market_rules": server_version >= 126,
            "market_data_farm_healthy": self.market_data_farm_healthy,
        }

    def add_event_handler(self, handler: BrokerEventHandler) -> None:
        self._event_handlers.append(handler)

    def configure_safety_controller(self, safety: TradingSafetyController) -> None:
        self._safety = safety

    def configure_execution_boundary(self, token: object) -> None:
        """所有订单修改都必须提供由 ExecutionSDK 持有的能力令牌。

        该机制用于纵深防御同进程误调用，真正的安全边界仍是隔离的 exec_svc 进程。
        """

        # capability 主要防止同进程误调用；真正的安全隔离仍由 exec_svc 独占 IBKR 连接实现。
        if token is None:
            raise ValueError("execution boundary token is required")
        if (
            self._execution_boundary_token is not None
            and token is not self._execution_boundary_token
        ):
            raise PermissionError("IBKR execution boundary is already configured")
        self._execution_boundary_token = token

    def connect(self) -> None:
        if self._client is not None and self._client.isConnected():
            return
        if self._client is not None:
            self._client.disconnect_and_stop()
        self._set_session_state(BrokerSessionState.CONNECTING)
        self._market_data_farm_healthy = None
        self._degraded_by_market_data = False
        client = _IBApiClient(
            timeout_seconds=self.config.request_timeout_seconds,
            event_handler=self._handle_client_event,
        )
        client._configured_client_id = self.config.client_id
        try:
            client.verify_api_handshake(self.config.host, self.config.port)
            client.connect(self.config.host, self.config.port, self.config.client_id)
            thread = Thread(target=client.run, name="starter-ibkr-client", daemon=True)
            thread.start()
            client.wait_connected()
            server_version = int(client.serverVersion())
            if server_version < self.config.minimum_server_version:
                raise RuntimeError(
                    f"IBKR server version {server_version} is below required "
                    f"{self.config.minimum_server_version}"
                )
            client.reqMarketDataType(self.config.market_data_type)
            client.reqCurrentTime()
            self._client = client
            self._thread = thread
            # 恢复或对账必须显式把执行边界推进到 READY；期间仍允许只读数据请求。
            self._set_session_state(BrokerSessionState.RECOVERING)
        except Exception:
            client.disconnect_and_stop()
            self._set_session_state(BrokerSessionState.DISCONNECTED)
            raise

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.disconnect_and_stop()
        self._client = None
        self._thread = None
        self._market_data_farm_healthy = None
        self._degraded_by_market_data = False
        self._set_session_state(BrokerSessionState.DISCONNECTED)

    def mark_reconciling(self) -> None:
        self._set_session_state(BrokerSessionState.RECONCILING)

    def mark_reconciled(self) -> None:
        client = self._client
        if client is None or not client.isConnected():
            raise ConnectionError("cannot mark a disconnected IBKR session ready")
        self._set_session_state(BrokerSessionState.READY)

    def mark_degraded(self) -> None:
        self._set_session_state(BrokerSessionState.DEGRADED)

    def resolve_account(self, account: str | None = None) -> str:
        return self._resolve_account(account)

    def mark_killed(self) -> None:
        self._set_session_state(BrokerSessionState.KILLED)

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
                output.extend(
                    self.historical_option_l1(instrument, start=request.start, end=request.end)
                )
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
            if ts < start or ts >= end:
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
                    vwap=_positive_decimal_or_none(raw.get("wap")),
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
            if ts < start or ts >= end:
                continue
            bid = Decimal(str(raw["open"]))
            ask = Decimal(str(raw["close"]))
            if ask < bid:
                bid, ask = ask, bid
            mid = (bid + ask) / Decimal(2)
            quotes.append(
                MarketQuote(
                    instrument=contract,
                    quote_ts=ts,
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    volume=_non_negative_int_or_none(raw.get("volume")),
                    open_interest=contract.metadata.get("open_interest"),
                    source="ibkr-historical-bid-ask",
                )
            )
        sleep(self.config.pacing_sleep_seconds)
        return quotes

    def snapshot_quote(self, instrument: InstrumentRef) -> MarketQuote:
        client = self._ensure_client()
        try:
            quote = client.request_snapshot(_contract_for_instrument(instrument))
        except IBKRNoDataError:
            if instrument.asset_type != AssetType.COMBO:
                raise
            return self._synthetic_combo_quote(instrument)
        except IBKRRequestError as exc:
            if instrument.asset_type != AssetType.COMBO or exc.code not in {
                354,
                10089,
                10197,
            }:
                raise
            return self._synthetic_combo_quote(instrument)
        received_at = datetime.now(UTC)
        normalized = MarketQuote(
            instrument=instrument,
            quote_ts=quote.get("quote_ts") or received_at,
            received_at=received_at,
            bid=_decimal_or_none(quote.get("bid")),
            ask=_decimal_or_none(quote.get("ask")),
            bid_size=_decimal_or_none(quote.get("bid_size")),
            ask_size=_decimal_or_none(quote.get("ask_size")),
            last=_decimal_or_none(quote.get("last")),
            volume=_non_negative_int_or_none(quote.get("volume")),
            source="ibkr-snapshot",
            market_data_type=quote.get("market_data_type"),
            timestamp_source="broker" if quote.get("quote_ts") else "received",
            halted_status=quote.get("halted_status"),
            shortable=_decimal_or_none(quote.get("shortable")),
        )
        if (
            instrument.asset_type == AssetType.COMBO
            and (
                normalized.bid is None
                or normalized.ask is None
                or normalized.ask < normalized.bid
            )
        ):
            return self._synthetic_combo_quote(instrument)
        return normalized

    def _synthetic_combo_quote(self, instrument: InstrumentRef) -> MarketQuote:
        """根据各组合腿的新鲜报价构造保守的组合 NBBO。"""

        raw_legs = instrument.metadata.get("combo_legs")
        if not isinstance(raw_legs, list) or len(raw_legs) < 2:
            raise ValueError("synthetic combo quote requires embedded combo legs")
        combo_bid = Decimal(0)
        combo_ask = Decimal(0)
        quote_times: list[datetime] = []
        market_data_types: list[int | None] = []
        halt_states: list[int | None] = []
        for payload in raw_legs:
            if not isinstance(payload, dict) or not isinstance(payload.get("instrument"), dict):
                raise ValueError("synthetic combo quote requires full leg instruments")
            leg = InstrumentRef.model_validate(payload["instrument"])
            ratio = Decimal(str(payload.get("ratio") or 0))
            action = str(payload.get("action") or "").upper()
            if ratio <= 0 or action not in {"BUY", "SELL"}:
                raise ValueError("synthetic combo quote contains an invalid leg")
            quote = self.snapshot_quote(leg)
            if quote.bid is None or quote.ask is None or quote.ask < quote.bid:
                raise RuntimeError(
                    f"IBKR did not return a valid NBBO for combo leg {leg.conid}"
                )
            if action == "BUY":
                combo_bid += ratio * quote.bid
                combo_ask += ratio * quote.ask
            else:
                combo_bid -= ratio * quote.ask
                combo_ask -= ratio * quote.bid
            quote_times.append(quote.quote_ts)
            market_data_types.append(quote.market_data_type)
            halt_states.append(quote.halted_status)
        if combo_ask < combo_bid:
            raise RuntimeError("synthetic combo NBBO is crossed")
        received_at = datetime.now(UTC)
        known_data_types = [value for value in market_data_types if value is not None]
        market_data_type = (
            1
            if known_data_types and all(value == 1 for value in known_data_types)
            else (known_data_types[0] if known_data_types else None)
        )
        halted_status = (
            max(value for value in halt_states if value is not None)
            if all(value is not None for value in halt_states)
            else None
        )
        return MarketQuote(
            instrument=instrument,
            quote_ts=min(quote_times),
            received_at=received_at,
            bid=combo_bid,
            ask=combo_ask,
            source="ibkr-synthetic-combo-nbbo",
            market_data_type=market_data_type,
            timestamp_source="broker-legs",
            halted_status=halted_status,
        )

    def tradeable_quote(self, instrument: InstrumentRef) -> MarketQuote:
        """短时流式报价，必须同时取得买卖价、行情类型和停牌状态。"""

        quote = self._ensure_client().request_tradeable_quote(_contract_for_instrument(instrument))
        received_at = quote.get("received_at") or datetime.now(UTC)
        return MarketQuote(
            instrument=instrument,
            # 短时行情流中的买卖价回调才是新鲜度依据；稍后到达的 LAST_TIMESTAMP 可能描述
            # 更早的成交，尤其在盘前，因此不能覆盖报价新鲜度。
            quote_ts=received_at,
            received_at=received_at,
            bid=_decimal_or_none(quote.get("bid")),
            ask=_decimal_or_none(quote.get("ask")),
            bid_size=_decimal_or_none(quote.get("bid_size")),
            ask_size=_decimal_or_none(quote.get("ask_size")),
            last=_decimal_or_none(quote.get("last")),
            volume=_non_negative_int_or_none(quote.get("volume")),
            source="ibkr-stream-guard",
            market_data_type=quote.get("market_data_type"),
            timestamp_source="received",
            halted_status=quote.get("halted_status"),
            shortable=_decimal_or_none(quote.get("shortable")),
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
                    "min_tick": getattr(item, "minTick", None),
                    "min_size": getattr(item, "minSize", None),
                    "size_increment": getattr(item, "sizeIncrement", None),
                    "valid_exchanges": getattr(item, "validExchanges", None),
                    "market_rule_ids": getattr(item, "marketRuleIds", None),
                    "order_types": getattr(item, "orderTypes", None),
                    "time_zone_id": getattr(item, "timeZoneId", None),
                    "trading_hours": getattr(item, "tradingHours", None),
                    "liquid_hours": getattr(item, "liquidHours", None),
                }
            )
        return output

    def qualify_contract(self, instrument: InstrumentRef) -> QualifiedContract:
        if instrument.asset_type == AssetType.COMBO:
            return self._qualify_combo_contract(instrument)
        details = self._ensure_client().request_contract_details(
            _contract_for_instrument(instrument)
        )
        if len(details) != 1:
            raise ValueError(
                f"expected exactly one IBKR contract for {instrument.symbol}, got {len(details)}"
            )
        detail = details[0]
        contract = detail.contract
        qualified = _instrument_from_contract(contract)
        exchanges = _csv_values(getattr(detail, "validExchanges", ""))
        rule_ids = [int(value) for value in _csv_values(getattr(detail, "marketRuleIds", ""))]
        min_tick = _finite_positive_decimal_or_none(getattr(detail, "minTick", None))
        if min_tick is None:
            raise RuntimeError("IBKR contract details did not provide a positive minTick")
        return QualifiedContract(
            instrument=qualified,
            primary_exchange=str(getattr(contract, "primaryExchange", "") or "") or None,
            valid_exchanges=exchanges,
            supported_order_types=_csv_values(getattr(detail, "orderTypes", "")),
            min_tick=min_tick,
            min_size=_finite_positive_decimal_or_none(getattr(detail, "minSize", None))
            or Decimal(1),
            size_increment=_finite_positive_decimal_or_none(getattr(detail, "sizeIncrement", None))
            or Decimal(1),
            market_rule_ids=list(dict.fromkeys(rule_ids)),
            time_zone_id=str(getattr(detail, "timeZoneId", "") or "") or None,
            trading_hours=str(getattr(detail, "tradingHours", "") or "") or None,
            liquid_hours=str(getattr(detail, "liquidHours", "") or "") or None,
        )

    def _qualify_combo_contract(self, instrument: InstrumentRef) -> QualifiedContract:
        raw_legs = instrument.metadata.get("combo_legs")
        if not isinstance(raw_legs, list) or len(raw_legs) < 2:
            raise ValueError("combo qualification requires embedded legs")
        qualified_legs = []
        for payload in raw_legs:
            if not isinstance(payload, dict) or not isinstance(payload.get("instrument"), dict):
                raise ValueError("combo qualification requires full leg instruments")
            leg = InstrumentRef.model_validate(payload["instrument"])
            if not leg.conid:
                raise ValueError("combo qualification requires qualified leg conids")
            qualified_legs.append(self.qualify_contract(leg))
        supported_sets = [set(item.supported_order_types) for item in qualified_legs]
        supported = sorted(set.intersection(*supported_sets)) if supported_sets else []
        exchange_sets = [set(item.valid_exchanges) for item in qualified_legs]
        exchanges = sorted(set.intersection(*exchange_sets)) if exchange_sets else []
        time_zones = {item.time_zone_id for item in qualified_legs}
        trading_hours = {item.trading_hours for item in qualified_legs}
        liquid_hours = {item.liquid_hours for item in qualified_legs}
        return QualifiedContract(
            instrument=instrument,
            valid_exchanges=exchanges,
            supported_order_types=supported,
            # IBKR 不支持直接查询 BAG 合约详情；采用各腿中最大的价格增量可保守地避免非法组合价格。
            min_tick=max(item.min_tick for item in qualified_legs),
            min_size=max(item.min_size for item in qualified_legs),
            size_increment=max(item.size_increment for item in qualified_legs),
            time_zone_id=time_zones.pop() if len(time_zones) == 1 else None,
            trading_hours=trading_hours.pop() if len(trading_hours) == 1 else None,
            liquid_hours=liquid_hours.pop() if len(liquid_hours) == 1 else None,
        )

    def market_rule(self, market_rule_id: int) -> list[dict[str, Decimal]]:
        rows = self._ensure_client().request_market_rule(market_rule_id)
        return [
            {
                "low_edge": Decimal(str(row["low_edge"])),
                "increment": Decimal(str(row["increment"])),
            }
            for row in rows
        ]

    def exercise_option(
        self,
        *,
        instrument: InstrumentRef,
        action: str,
        quantity: Decimal,
        account: str | None = None,
        override: bool = False,
        confirmation: str,
        execution_token: object | None = None,
    ) -> int:
        self._assert_execution_boundary(execution_token)
        if instrument.asset_type != AssetType.OPTION or not instrument.conid:
            raise ValueError("exercise/lapse requires a qualified option conid")
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise ValueError("option exercise quantity must be a positive whole number")
        normalized_action = action.upper()
        if normalized_action not in {"EXERCISE", "LAPSE"}:
            raise ValueError("option action must be EXERCISE or LAPSE")
        selected_account = self._resolve_account(account)
        expected = f"{normalized_action}:{selected_account}:{instrument.conid}:{quantity}"
        if confirmation != expected:
            raise PermissionError("option lifecycle confirmation does not match request")
        if self._safety is None:
            if not selected_account.upper().startswith("DU"):
                raise TradingSafetyError("live option exercise requires configured safety controls")
        else:
            self._safety.assert_can_transmit(account=selected_account)
        return self._ensure_client().request_exercise_option(
            contract=_option_contract(instrument),
            action=1 if normalized_action == "EXERCISE" else 2,
            quantity=int(quantity),
            account=selected_account,
            override=override,
        )

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
        smart_params = [
            param
            for param in params
            if str(param.get("exchange") or "").upper() == "SMART"
        ]
        # 同一期权链会由多个目的交易所重复发布；合约最终通过 SMART 解析，因此遍历重复场所只会
        # 增加数分钟延迟并产生大量预期内的 200 错误。
        params = smart_params or params[:1]
        underlying_quote = self.snapshot_quote(
            InstrumentRef(asset_type=_asset_type_for_symbol(symbol), symbol=symbol.upper())
        )
        underlying_price = (
            underlying_quote.last
            or underlying_quote.mid
            or underlying_quote.bid
            or underlying_quote.ask
        )
        contracts: list[InstrumentRef] = []
        seen_conids: set[int] = set()
        for param in params:
            expirations = sorted(
                expiry
                for expiry in (_parse_expiry(value) for value in param.get("expirations", []))
                if dte_min <= (expiry - as_of).days <= dte_max
            )
            for expiry in expirations[:max_expiries]:
                strikes = _select_strikes(
                    param.get("strikes", []), underlying_price, max_per_side=max_per_side
                )
                for right in ("CALL", "PUT"):
                    for strike in strikes:
                        instrument = InstrumentRef(
                            asset_type=AssetType.OPTION,
                            symbol=symbol.upper(),
                            option_right=right,
                            strike=Decimal(str(strike)),
                            expiry=expiry,
                            metadata={
                                "dte": (expiry - as_of).days,
                                "trading_class": param.get("trading_class"),
                                "multiplier": param.get("multiplier") or "100",
                            },
                        )
                        try:
                            details = client.request_contract_details(
                                _option_contract(instrument)
                            )
                        except IBKRRequestError as exc:
                            # 证券定义参数可能包含无法在 SMART 解析的交易所与交易类别组合；仅当
                            # 经纪商返回标准的“无证券定义”错误时才继续尝试下一候选项。
                            if exc.code == 200:
                                continue
                            raise
                        if not details:
                            continue
                        conid = int(details[0].contract.conId)
                        if conid in seen_conids:
                            continue
                        seen_conids.add(conid)
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
                            except Exception as exc:  # noqa: BLE001 - 期权链发现期间仅尽力获取报价。
                                enriched.metadata["quote_error"] = str(exc)
                        contracts.append(enriched)
                        sleep(self.config.pacing_sleep_seconds)
        return contracts

    def managed_accounts(self) -> list[str]:
        return self._ensure_client().request_managed_accounts()

    def account_summary(
        self,
        *,
        account: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> list[BrokerAccountValue]:
        selected_account = self._resolve_account(account)
        requested_tags = tags or (
            "AccountType",
            "NetLiquidation",
            "TotalCashValue",
            "BuyingPower",
            "AvailableFunds",
            "MaintMarginReq",
        )
        rows = self._ensure_client().request_account_summary(tags=requested_tags)
        return [BrokerAccountValue(**row) for row in rows if row["account"] == selected_account]

    def pnl_snapshot(self, *, account: str | None = None) -> BrokerPnL:
        selected_account = self._resolve_account(account)
        row = self._ensure_client().request_pnl_snapshot(account=selected_account)
        return BrokerPnL(account=selected_account, **row)

    def subscribe_pnl(self, *, account: str | None = None) -> int:
        selected_account = self._resolve_account(account)
        existing = next(
            (
                subscription_id
                for subscription_id, subscribed_account in self._pnl_subscriptions.items()
                if subscribed_account == selected_account
            ),
            None,
        )
        if existing is not None:
            return existing
        subscription_id = self._ensure_client().start_pnl_subscription(account=selected_account)
        self._pnl_subscriptions[subscription_id] = selected_account
        return subscription_id

    def unsubscribe_pnl(self, subscription_id: int) -> None:
        self._ensure_client().stop_pnl_subscription(subscription_id)
        self._pnl_subscriptions.pop(subscription_id, None)

    def resubscribe_streams(self) -> dict[int, int]:
        client = self._ensure_client()
        prior = dict(self._pnl_subscriptions)
        self._pnl_subscriptions.clear()
        replacements: dict[int, int] = {}
        for old_id, account in prior.items():
            with suppress(Exception):
                client.stop_pnl_subscription(old_id)
            new_id = client.start_pnl_subscription(account=account)
            self._pnl_subscriptions[new_id] = account
            replacements[old_id] = new_id
        if self._auto_open_orders_enabled:
            client.reqAutoOpenOrders(True)
        return replacements

    def account_risk_snapshot(self, *, account: str | None = None) -> AccountRiskSnapshot:
        selected_account = self._resolve_account(account)
        summary = self.account_summary(
            account=selected_account,
            tags=(
                "NetLiquidation",
                "BuyingPower",
                "AvailableFunds",
                "MaintMarginReq",
                "GrossPositionValue",
                "UnrealizedPnL",
                "RealizedPnL",
            ),
        )
        values = {row.tag: _required_account_decimal(row.tag, row.value) for row in summary}
        required = {
            "NetLiquidation",
            "BuyingPower",
            "AvailableFunds",
            "MaintMarginReq",
            "GrossPositionValue",
        }
        missing = sorted(required - values.keys())
        if missing:
            raise RuntimeError(f"IBKR account summary is missing: {', '.join(missing)}")
        pnl = self.pnl_snapshot(account=selected_account)
        positions = self.positions(account=selected_account)
        symbol_notional: dict[str, Decimal] = {}
        instrument_notional: dict[str, Decimal] = {}
        instrument_quantity: dict[str, Decimal] = {}
        for position in positions:
            if not position.instrument.conid:
                raise RuntimeError(
                    f"cannot value {position.instrument.symbol} position without a conid"
                )
            position_pnl = self._ensure_client().request_pnl_single_snapshot(
                account=selected_account,
                conid=position.instrument.conid,
            )
            estimate = position_pnl.get("value")
            if estimate is None:
                raise RuntimeError(
                    f"IBKR did not return position value for {position.instrument.symbol}"
                )
            symbol_notional[position.instrument.symbol] = (
                symbol_notional.get(position.instrument.symbol, Decimal(0)) + estimate
            )
            risk_key = _instrument_risk_key(position.instrument)
            instrument_notional[risk_key] = estimate
            instrument_quantity[risk_key] = (
                instrument_quantity.get(risk_key, Decimal(0)) + position.quantity
            )
        open_order_notional = Decimal(0)
        for order in self.open_orders(all_clients=True):
            if order.account != selected_account or order.quantity is None:
                continue
            price = order.limit_price or order.stop_price or order.avg_fill_price
            if order.instrument and order.instrument.asset_type == AssetType.COMBO:
                price = abs(price)
            if price <= 0:
                if order.instrument is None:
                    raise RuntimeError(
                        f"cannot value open market order {order.order_id} without its contract"
                    )
                quote = self.snapshot_quote(order.instrument)
                price = quote.ask if order.side == "BUY" else quote.bid
                if price is None or price <= 0:
                    raise RuntimeError(
                        f"cannot value open market order {order.order_id} from a fresh quote"
                    )
            multiplier = Decimal(1)
            if order.instrument:
                multiplier = Decimal(
                    str(
                        order.instrument.metadata.get(
                            "multiplier",
                            "100" if order.instrument.asset_type == AssetType.OPTION else "1",
                        )
                    )
                )
            open_order_notional += order.remaining * price * multiplier
        return AccountRiskSnapshot(
            account=selected_account,
            captured_at=pnl.captured_at,
            net_liquidation=values["NetLiquidation"],
            available_funds=values["AvailableFunds"],
            buying_power=values["BuyingPower"],
            maintenance_margin=values["MaintMarginReq"],
            daily_pnl=pnl.daily_pnl,
            realized_pnl=(
                pnl.realized_pnl if pnl.realized_pnl is not None else values.get("RealizedPnL")
            ),
            unrealized_pnl=(
                pnl.unrealized_pnl
                if pnl.unrealized_pnl is not None
                else values.get("UnrealizedPnL")
            ),
            gross_position_notional=abs(values["GrossPositionValue"]),
            open_order_notional=open_order_notional,
            symbol_position_notional=symbol_notional,
            instrument_position_notional=instrument_notional,
            instrument_position_quantity=instrument_quantity,
            market_data_type=self.market_data_type,
        )

    def positions(self, *, account: str | None = None) -> list[BrokerPosition]:
        selected_account = self._resolve_account(account)
        rows = self._ensure_client().request_positions()
        return [
            BrokerPosition(
                account=row["account"],
                instrument=_instrument_from_contract(row["contract"]),
                quantity=Decimal(str(row["quantity"])),
                avg_cost=Decimal(str(row["avg_cost"])),
            )
            for row in rows
            if row["account"] == selected_account
        ]

    def place_order(
        self,
        request: BrokerOrderRequest,
        *,
        order_id: int | None = None,
        execution_token: object | None = None,
    ) -> BrokerOrderStatus:
        self._assert_execution_boundary(execution_token)
        account = self._resolve_account(request.account)
        normalized = request.model_copy(update={"account": account})
        self._assert_order_safe(normalized)
        raw = self._ensure_client().submit_order(
            contract=_contract_for_instrument(normalized.instrument),
            order=_broker_order(normalized),
            order_id=order_id,
        )
        return _broker_order_status(raw)

    def place_bracket(
        self,
        *,
        entry: BrokerOrderRequest,
        take_profit: BrokerOrderRequest,
        stop_loss: BrokerOrderRequest,
        execution_token: object | None = None,
    ) -> list[BrokerOrderStatus]:
        self._assert_execution_boundary(execution_token)
        requests = self._normalize_linked_orders([entry, take_profit, stop_loss])
        entry_request, take_profit_request, stop_request = requests
        if any(request.what_if for request in requests):
            raise ValueError("IBKR what-if is not supported for an attached bracket batch")
        if (
            take_profit_request.side == entry_request.side
            or stop_request.side == entry_request.side
        ):
            raise ValueError("bracket exits must be opposite the entry side")
        if (
            take_profit_request.quantity != entry_request.quantity
            or stop_request.quantity != entry_request.quantity
        ):
            raise ValueError("bracket child quantities must equal the parent quantity")
        client = self._ensure_client()
        order_ids = client.reserve_order_ids(3)
        parent_id = order_ids[0]
        entry_order = _broker_order(entry_request.model_copy(update={"transmit": False}))
        profit_order = _broker_order(
            take_profit_request.model_copy(update={"parent_order_id": parent_id, "transmit": False})
        )
        stop_order = _broker_order(
            stop_request.model_copy(update={"parent_order_id": parent_id, "transmit": True})
        )
        contract = _contract_for_instrument(entry_request.instrument)
        rows = client.submit_order_batch(
            [
                (order_ids[0], contract, entry_order),
                (order_ids[1], contract, profit_order),
                (order_ids[2], contract, stop_order),
            ]
        )
        return [_broker_order_status(row) for row in rows]

    def place_oca(
        self,
        requests: Sequence[BrokerOrderRequest],
        *,
        oca_group: str,
        oca_type: int = 1,
        execution_token: object | None = None,
    ) -> list[BrokerOrderStatus]:
        self._assert_execution_boundary(execution_token)
        if len(requests) < 2:
            raise ValueError("OCA group requires at least two orders")
        if not oca_group.strip():
            raise ValueError("oca_group is required")
        if oca_type not in {1, 2, 3}:
            raise ValueError("oca_type must be 1, 2, or 3")
        normalized = self._normalize_linked_orders(list(requests), require_same_contract=False)
        if any(request.what_if for request in normalized):
            raise ValueError("IBKR what-if is not supported for an OCA batch")
        client = self._ensure_client()
        order_ids = client.reserve_order_ids(len(normalized))
        batch = []
        for order_id, request in zip(order_ids, normalized, strict=True):
            configured = request.model_copy(
                update={
                    "oca_group": oca_group,
                    "oca_type": oca_type,
                    # OCA 成员与附属 Bracket 不同，彼此是独立订单；每个成员都必须发送，组关系由
                    # ocaGroup 和 ocaType 表示，剩余成员由 IBKR 自动撤销。
                    "transmit": True,
                }
            )
            batch.append(
                (
                    order_id,
                    _contract_for_instrument(configured.instrument),
                    _broker_order(configured),
                )
            )
        return [_broker_order_status(row) for row in client.submit_order_batch(batch)]

    def replace_order(
        self,
        order_id: int,
        request: BrokerOrderRequest,
        *,
        expected_permanent_id: int | None = None,
        execution_token: object | None = None,
    ) -> BrokerOrderStatus:
        self._assert_execution_boundary(execution_token)
        account = self._resolve_account(request.account)
        current = self.order_status(order_id, account=account, client_id=self.config.client_id)
        self._assert_owned_order(
            current,
            account=account,
            order_id=order_id,
            expected_permanent_id=expected_permanent_id,
            expected_order_ref=request.order_ref,
        )
        return self.place_order(
            request.model_copy(update={"account": account}),
            order_id=order_id,
            execution_token=execution_token,
        )

    def cancel_order(
        self,
        order_id: int,
        *,
        account: str | None = None,
        permanent_id: int | None = None,
        order_ref: str | None = None,
        execution_token: object | None = None,
    ) -> BrokerOrderStatus:
        self._assert_execution_boundary(execution_token)
        selected_account = self._resolve_account(account)
        current = self.order_status(
            order_id,
            account=selected_account,
            client_id=self.config.client_id,
        )
        self._assert_owned_order(
            current,
            account=selected_account,
            order_id=order_id,
            expected_permanent_id=permanent_id,
            expected_order_ref=order_ref,
        )
        raw = self._ensure_client().request_cancel_order(order_id)
        return _broker_order_status(raw)

    def cancel_all_orders(
        self,
        *,
        account: str | None = None,
        include_other_clients: bool = False,
        confirmation: str,
        execution_token: object | None = None,
    ) -> list[BrokerOrderStatus]:
        self._assert_execution_boundary(execution_token)
        selected_account = self._resolve_account(account)
        expected = (
            f"GLOBAL-CANCEL:{selected_account}"
            if include_other_clients
            else f"CANCEL-OWNED:{selected_account}"
        )
        if confirmation != expected:
            raise PermissionError("cancel-all confirmation does not match scope and account")
        orders = [
            order
            for order in self.open_orders(all_clients=True)
            if order.account == selected_account
        ]
        if include_other_clients:
            self._ensure_client().request_global_cancel()
            return orders
        cancelled: list[BrokerOrderStatus] = []
        for order in orders:
            if order.client_id != self.config.client_id:
                continue
            cancelled.append(
                self.cancel_order(
                    order.order_id,
                    account=selected_account,
                    permanent_id=order.permanent_id,
                    order_ref=order.order_ref,
                )
            )
        return cancelled

    def open_orders(self, *, all_clients: bool = False) -> list[BrokerOrderStatus]:
        rows = self._ensure_client().request_open_orders(all_clients=all_clients)
        return [_broker_order_status(row) for row in rows]

    def order_status(
        self,
        order_id: int,
        *,
        account: str | None = None,
        client_id: int | None = None,
    ) -> BrokerOrderStatus | None:
        client = self._ensure_client()
        for row in client.request_open_orders(all_clients=True):
            if _order_identity_matches(
                row, order_id=order_id, account=account, client_id=client_id
            ):
                return _broker_order_status(row)
        for row in client.request_completed_orders(api_only=False):
            if _order_identity_matches(
                row, order_id=order_id, account=account, client_id=client_id
            ):
                return _broker_order_status(row)
        cached = client.order_snapshot(order_id)
        if cached is not None and _order_identity_matches(
            cached, order_id=order_id, account=account, client_id=client_id
        ):
            return _broker_order_status(cached)
        return None

    def completed_orders(self, *, api_only: bool = True) -> list[BrokerOrderStatus]:
        rows = self._ensure_client().request_completed_orders(api_only=api_only)
        return [_broker_order_status(row) for row in rows]

    def executions(
        self,
        *,
        account: str | None = None,
        since: datetime | None = None,
        symbol: str | None = None,
        all_clients: bool = True,
    ) -> list[BrokerExecution]:
        selected_account = self._resolve_account(account)
        rows = self._ensure_client().request_executions(
            account=selected_account,
            since=since,
            symbol=symbol,
            client_id=None if all_clients else self.config.client_id,
        )
        return [_broker_execution(row) for row in rows]

    def bind_manual_orders(self) -> None:
        if self.config.client_id != 0:
            raise PermissionError("IBKR reqAutoOpenOrders is restricted to API client ID 0")
        self._ensure_client().reqAutoOpenOrders(True)
        self._auto_open_orders_enabled = True

    def require_paper_account(self, account: str | None = None) -> str:
        selected_account = self._resolve_account(account)
        if not selected_account.upper().startswith("DU"):
            raise PermissionError(
                f"Refusing paper-only operation for non-paper IBKR account {selected_account!r}"
            )
        return selected_account

    def _resolve_account(self, account: str | None) -> str:
        accounts = self.managed_accounts()
        selected = account or self.config.account
        if selected is not None:
            if selected not in accounts:
                raise ValueError(f"IBKR account {selected!r} is not managed by this API session")
            return selected
        if len(accounts) != 1:
            raise ValueError(
                "IBKR account must be specified when the API session manages multiple accounts"
            )
        return accounts[0]

    def _assert_order_safe(self, request: BrokerOrderRequest) -> None:
        account = request.account
        if not account:
            raise TradingSafetyError("resolved account is required before order submission")
        if self._safety is None:
            if not account.upper().startswith("DU"):
                raise TradingSafetyError(
                    "direct live routing is disabled; configure TradingSafetyController and use ExecutionSDK"
                )
        else:
            self._safety.assert_can_transmit(
                account=account,
                what_if=request.what_if,
                reduce_only=request.reduce_only,
            )
        if not account.upper().startswith("DU"):
            if self.session_state not in {
                BrokerSessionState.READY,
                BrokerSessionState.KILLED,
            } or (self.session_state == BrokerSessionState.KILLED and not request.reduce_only):
                raise TradingSafetyError(
                    f"live routing requires a reconciled READY session, got {self.session_state.value}"
                )
            if not request.order_ref:
                raise TradingSafetyError("live orders require a stable order_ref")

    def _assert_execution_boundary(self, token: object | None) -> None:
        expected = self._execution_boundary_token
        if expected is not None and token is not expected:
            raise TradingSafetyError(
                "direct IBKR order routing is disabled; submit through ExecutionSDK"
            )

    def _normalize_linked_orders(
        self,
        requests: Sequence[BrokerOrderRequest],
        *,
        require_same_contract: bool = True,
    ) -> list[BrokerOrderRequest]:
        if not requests:
            raise ValueError("linked order batch cannot be empty")
        account = self._resolve_account(requests[0].account)
        instrument = requests[0].instrument
        normalized: list[BrokerOrderRequest] = []
        for request in requests:
            if require_same_contract and request.instrument != instrument:
                raise ValueError("all linked orders must target exactly the same contract")
            selected = self._resolve_account(request.account)
            if selected != account:
                raise ValueError("all linked orders must use the same account")
            item = request.model_copy(update={"account": account})
            self._assert_order_safe(item)
            normalized.append(item)
        return normalized

    def _assert_owned_order(
        self,
        status: BrokerOrderStatus | None,
        *,
        account: str,
        order_id: int,
        expected_permanent_id: int | None = None,
        expected_order_ref: str | None = None,
    ) -> None:
        if status is None:
            raise LookupError(f"cannot verify ownership of IBKR order {order_id}")
        if status.account != account:
            raise PermissionError(f"order {order_id} belongs to another account")
        if status.client_id != self.config.client_id:
            raise PermissionError(
                f"order {order_id} belongs to API client {status.client_id}, not {self.config.client_id}"
            )
        if expected_permanent_id is not None and status.permanent_id != expected_permanent_id:
            raise PermissionError(f"order {order_id} permanent ID does not match the ledger")
        if expected_order_ref is not None and status.order_ref != expected_order_ref:
            raise PermissionError(f"order {order_id} orderRef does not match the ledger")

    def _handle_client_event(self, event: BrokerEvent) -> None:
        if event.event_type == BrokerEventType.CONNECTION:
            state = event.payload.get("state")
            if state in {"DISCONNECTED", "SOCKET_CLOSED"}:
                self._set_session_state(BrokerSessionState.DISCONNECTED)
            elif state in {"LOST", "DEGRADED"}:
                self._set_session_state(BrokerSessionState.DEGRADED)
            elif state in {"RESTORED_DATA_LOST", "RESTORED"}:
                self._set_session_state(BrokerSessionState.RECOVERING)
            elif state == "MARKET_DATA_FARM_LOST":
                self._market_data_farm_healthy = False
                self._degraded_by_market_data = True
                self._set_session_state(BrokerSessionState.DEGRADED)
            elif state == "MARKET_DATA_FARM_OK":
                self._market_data_farm_healthy = True
                if self._degraded_by_market_data:
                    self._degraded_by_market_data = False
                    self._set_session_state(BrokerSessionState.RECOVERING)
        self._dispatch_event(event)

    def _dispatch_event(self, event: BrokerEvent) -> None:
        for handler in tuple(self._event_handlers):
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - 回调异常不得终止 IB API 读取线程。
                continue

    def _set_session_state(self, state: BrokerSessionState) -> None:
        with self._state_lock:
            self._session_state = state

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
        raise RuntimeError(
            "Install the starter with the 'ibkr' extra to use IBKRAdapter: pip install -e '.[ibkr]'"
        ) from exc
    return EWrapper, EClient


class _IBApiClient(*_ibapi_base_classes()):
    def __init__(
        self,
        *,
        timeout_seconds: int,
        event_handler: BrokerEventHandler | None = None,
    ) -> None:
        wrapper, client = _ibapi_classes()
        wrapper.__init__(self)
        client.__init__(self, self)
        self.timeout_seconds = timeout_seconds
        self._event_handler = event_handler
        self._next_req_id = 1000
        self._next_order_id: int | None = None
        self._lock = Lock()
        self._positions_query_lock = Lock()
        self._open_orders_query_lock = Lock()
        self._completed_orders_query_lock = Lock()
        self._connected_event = Event()
        self._current_time_event = Event()
        self._last_broker_time: int | None = None
        self._order_id_event = Event()
        self._managed_accounts_event = Event()
        self._managed_accounts: list[str] = []
        self._historical: dict[int, list[dict[str, Any]]] = {}
        self._historical_done: dict[int, Event] = {}
        self._snapshots: dict[int, dict[str, Any]] = {}
        self._snapshot_done: dict[int, Event] = {}
        self._tradeable_quotes: dict[int, dict[str, Any]] = {}
        self._tradeable_quote_done: dict[int, Event] = {}
        self.current_market_data_type: int | None = None
        self._contract_details: dict[int, list[Any]] = {}
        self._contract_details_done: dict[int, Event] = {}
        self._option_params: dict[int, list[dict[str, Any]]] = {}
        self._option_params_done: dict[int, Event] = {}
        self._account_summaries: dict[int, list[dict[str, str]]] = {}
        self._account_summaries_done: dict[int, Event] = {}
        self._positions: list[dict[str, Any]] = []
        self._positions_done = Event()
        self._orders: dict[int, dict[str, Any]] = {}
        self._order_events: dict[int, Event] = {}
        self._open_orders: dict[int, dict[str, Any]] = {}
        self._open_orders_done = Event()
        self._collecting_open_orders = False
        self._completed_orders: dict[int, dict[str, Any]] = {}
        self._completed_orders_done = Event()
        self._collecting_completed_orders = False
        self._executions: dict[int, list[dict[str, Any]]] = {}
        self._executions_done: dict[int, Event] = {}
        self._live_executions: dict[str, dict[str, Any]] = {}
        self._commissions: dict[str, dict[str, Any]] = {}
        self._pnl: dict[int, dict[str, Any]] = {}
        self._pnl_done: dict[int, Event] = {}
        self._pnl_accounts: dict[int, str] = {}
        self._pnl_single: dict[int, dict[str, Any]] = {}
        self._pnl_single_done: dict[int, Event] = {}
        self._pnl_single_accounts: dict[int, str] = {}
        self._market_rules: dict[int, list[dict[str, Any]]] = {}
        self._market_rule_done: dict[int, Event] = {}
        self._request_errors: dict[int, list[tuple[int, str, str | None]]] = {}
        self.errors: list[tuple[int, int | None, int, str]] = []

    def nextValidId(self, orderId: int) -> None:
        with self._lock:
            if self._next_order_id is None or orderId > self._next_order_id:
                self._next_order_id = int(orderId)
        self._order_id_event.set()
        self._connected_event.set()
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.CONNECTION,
                payload={"state": "CONNECTED", "next_order_id": int(orderId)},
            )
        )

    def managedAccounts(self, accountsList: str) -> None:
        self._managed_accounts = [
            value.strip() for value in accountsList.split(",") if value.strip()
        ]
        self._managed_accounts_event.set()

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
        connection_states = {
            1100: "LOST",
            1101: "RESTORED_DATA_LOST",
            1102: "RESTORED",
            1300: "SOCKET_CLOSED",
            2103: "MARKET_DATA_FARM_LOST",
            2104: "MARKET_DATA_FARM_OK",
        }
        event_type = (
            BrokerEventType.CONNECTION
            if normalized_code in connection_states
            else BrokerEventType.REJECTION
        )
        order_record = getattr(self, "_orders", {}).get(int(reqId), {})
        broker_client_id = order_record.get("client_id")
        if broker_client_id is None:
            broker_client_id = getattr(self, "_configured_client_id", None)
        self._emit(
            BrokerEvent(
                event_type=event_type,
                account=order_record.get("account"),
                client_order_id=order_record.get("order_ref"),
                broker_order_id=int(reqId) if int(reqId) >= 0 else None,
                permanent_id=order_record.get("permanent_id"),
                payload={
                    "state": connection_states.get(normalized_code),
                    "code": normalized_code,
                    "message": normalized_text,
                    "client_id": broker_client_id,
                    "advanced_order_reject_json": advancedOrderRejectJson or None,
                },
            )
        )
        self._complete_on_error(
            int(reqId),
            normalized_code,
            normalized_text,
            advancedOrderRejectJson or None,
        )

    def historicalData(self, reqId: int, bar: Any) -> None:
        if reqId not in self._historical_done:
            return
        self._historical.setdefault(reqId, []).append(
            {
                "date": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "wap": getattr(bar, "average", getattr(bar, "wap", None)),
            }
        )

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        event = self._historical_done.get(reqId)
        if event is not None:
            event.set()

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
        target = self._quote_target(reqId)
        if target is None:
            return
        if price is None or price < 0:
            return
        field = {
            1: "bid",
            2: "ask",
            4: "last",
            9: "close",
            66: "bid",
            67: "ask",
            68: "last",
            75: "close",
        }.get(tickType)
        if field:
            target[field] = price
            target["received_at"] = datetime.now(UTC)
            if field in {"bid", "ask"}:
                target["quote_ts"] = target["received_at"]
            self._maybe_complete_tradeable_quote(reqId)

    def tickSize(self, reqId: int, tickType: int, size: int) -> None:
        target = self._quote_target(reqId)
        if target is None:
            return
        size_field = {0: "bid_size", 3: "ask_size", 69: "bid_size", 70: "ask_size"}.get(tickType)
        if size_field:
            target[size_field] = size
        if tickType in {8, 27, 29, 74}:
            target["volume"] = size

    def tickString(self, reqId: int, tickType: int, value: str) -> None:
        target = self._quote_target(reqId)
        if target is None or tickType not in {45, 48, 77, 88}:
            return
        try:
            timestamp = value.split(";")[2] if tickType in {48, 77} else value
            target["quote_ts"] = datetime.fromtimestamp(int(timestamp), tz=UTC)
        except (TypeError, ValueError, OSError):
            return

    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        self.current_market_data_type = int(marketDataType)
        target = self._quote_target(reqId)
        if target is not None:
            target["market_data_type"] = int(marketDataType)
            self._maybe_complete_tradeable_quote(reqId)

    def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:
        target = self._quote_target(reqId)
        if target is None:
            return
        if tickType == 46:
            target["shortable"] = value
        elif tickType == 49:
            target["halted_status"] = int(value)
            self._maybe_complete_tradeable_quote(reqId)

    def tickSnapshotEnd(self, reqId: int) -> None:
        event = self._snapshot_done.get(reqId)
        if event is not None:
            event.set()

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:
        self._contract_details.setdefault(reqId, []).append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:
        event = self._contract_details_done.get(reqId)
        if event is not None:
            event.set()

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
        event = self._option_params_done.get(reqId)
        if event is not None:
            event.set()

    def accountSummary(
        self,
        reqId: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:
        if reqId not in self._account_summaries_done:
            return
        self._account_summaries.setdefault(reqId, []).append(
            {"account": account, "tag": tag, "value": value, "currency": currency or None}
        )

    def accountSummaryEnd(self, reqId: int) -> None:
        event = self._account_summaries_done.get(reqId)
        if event is not None:
            event.set()

    def position(
        self,
        account: str,
        contract: Any,
        position: Any,
        avgCost: float,
    ) -> None:
        row = {
            "account": account,
            "contract": contract,
            "quantity": position,
            "avg_cost": avgCost,
        }
        self._positions.append(row)
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.POSITION,
                account=account,
                payload={
                    "instrument": _instrument_from_contract(contract),
                    "quantity": str(position),
                    "avg_cost": str(avgCost),
                },
            )
        )

    def positionEnd(self) -> None:
        self._positions_done.set()

    def openOrder(self, orderId: int, contract: Any, order: Any, orderState: Any) -> None:
        self._observe_order_id(orderId)
        record = self._orders.setdefault(orderId, {"order_id": orderId})
        record.update(_raw_order_record(orderId, contract, order, orderState))
        record["acknowledged"] = True
        event = self._order_events.get(orderId)
        if event is not None:
            event.set()
        if self._collecting_open_orders:
            self._open_orders[orderId] = record
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.OPEN_ORDER,
                account=record.get("account"),
                client_order_id=record.get("order_ref"),
                broker_order_id=orderId,
                permanent_id=record.get("permanent_id"),
                payload=_order_event_payload(record),
            )
        )

    def openOrderEnd(self) -> None:
        if self._collecting_open_orders:
            self._open_orders_done.set()

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: Any,
        remaining: Any,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float,
    ) -> None:
        self._observe_order_id(orderId)
        record = self._orders.setdefault(orderId, {"order_id": orderId})
        record.update(
            {
                "acknowledged": True,
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "avg_fill_price": avgFillPrice,
                "permanent_id": permId or None,
                "parent_id": parentId or None,
                "last_fill_price": lastFillPrice,
                "client_id": clientId,
                "why_held": whyHeld or None,
                "updated_at": datetime.now(UTC),
            }
        )
        event = self._order_events.get(orderId)
        if event is not None:
            event.set()
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.ORDER_STATUS,
                account=record.get("account"),
                client_order_id=record.get("order_ref"),
                broker_order_id=orderId,
                permanent_id=record.get("permanent_id"),
                payload=_order_event_payload(record),
            )
        )

    def completedOrder(self, contract: Any, order: Any, orderState: Any) -> None:
        order_id = int(getattr(order, "orderId", 0))
        self._observe_order_id(order_id)
        record = _raw_order_record(order_id, contract, order, orderState)
        self._completed_orders[order_id] = record
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.ORDER_STATUS,
                account=record.get("account"),
                client_order_id=record.get("order_ref"),
                broker_order_id=order_id,
                permanent_id=record.get("permanent_id"),
                payload=_order_event_payload(record),
            )
        )

    def completedOrdersEnd(self) -> None:
        if self._collecting_completed_orders:
            self._completed_orders_done.set()

    def execDetails(self, reqId: int, contract: Any, execution: Any) -> None:
        row = {"execution": execution, "contract": contract}
        execution_id = str(execution.execId)
        self._live_executions[execution_id] = row
        if reqId in self._executions_done:
            self._executions.setdefault(reqId, []).append(row)
        normalized = _broker_execution(
            {**row, "commission_report": self._commissions.get(execution_id)}
        )
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.EXECUTION,
                account=normalized.account,
                client_order_id=normalized.order_ref,
                broker_order_id=normalized.order_id,
                permanent_id=normalized.permanent_id,
                execution_id=normalized.execution_id,
                payload=normalized.model_dump(mode="json"),
            )
        )

    def execDetailsEnd(self, reqId: int) -> None:
        event = self._executions_done.get(reqId)
        if event is not None:
            event.set()

    def commissionReport(self, commissionReport: Any) -> None:
        execution_id = str(commissionReport.execId)
        payload = {
            "commission": commissionReport.commission,
            "currency": commissionReport.currency,
            "realized_pnl": commissionReport.realizedPNL,
        }
        self._commissions[execution_id] = payload
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.COMMISSION,
                execution_id=execution_id,
                payload=payload,
            )
        )

    def pnl(
        self,
        reqId: int,
        dailyPnL: float,
        unrealizedPnL: float,
        realizedPnL: float,
    ) -> None:
        row = {
            "daily_pnl": _finite_decimal_or_none(dailyPnL),
            "unrealized_pnl": _finite_decimal_or_none(unrealizedPnL),
            "realized_pnl": _finite_decimal_or_none(realizedPnL),
            "captured_at": datetime.now(UTC),
        }
        self._pnl[reqId] = row
        event = self._pnl_done.get(reqId)
        if event is not None:
            event.set()
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.PNL,
                account=self._pnl_accounts.get(reqId),
                payload={key: str(value) for key, value in row.items()},
            )
        )

    def pnlSingle(
        self,
        reqId: int,
        pos: Any,
        dailyPnL: float,
        unrealizedPnL: float,
        realizedPnL: float,
        value: float,
    ) -> None:
        row = {
            "position": _finite_decimal_or_none(pos),
            "daily_pnl": _finite_decimal_or_none(dailyPnL),
            "unrealized_pnl": _finite_decimal_or_none(unrealizedPnL),
            "realized_pnl": _finite_decimal_or_none(realizedPnL),
            "value": _finite_decimal_or_none(value),
            "captured_at": datetime.now(UTC),
        }
        self._pnl_single[reqId] = row
        event = self._pnl_single_done.get(reqId)
        if event is not None:
            event.set()
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.PNL,
                account=self._pnl_single_accounts.get(reqId),
                payload={"scope": "POSITION", **{key: str(item) for key, item in row.items()}},
            )
        )

    def marketRule(self, marketRuleId: int, priceIncrements: Sequence[Any]) -> None:
        self._market_rules[marketRuleId] = [
            {
                "low_edge": increment.lowEdge,
                "increment": increment.increment,
            }
            for increment in priceIncrements
        ]
        event = self._market_rule_done.get(marketRuleId)
        if event is not None:
            event.set()

    def orderBound(self, orderId: int, apiClientId: int, apiOrderId: int) -> None:
        self._observe_order_id(apiOrderId)
        record = self._orders.setdefault(apiOrderId, {"order_id": apiOrderId})
        record.update({"permanent_id": orderId or None, "client_id": apiClientId})

    def connectAck(self) -> None:
        self._emit(BrokerEvent(event_type=BrokerEventType.CONNECTION, payload={"state": "ACK"}))

    def connectionClosed(self) -> None:
        self._connected_event.clear()
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.CONNECTION,
                payload={"state": "DISCONNECTED"},
            )
        )

    def wait_connected(self) -> None:
        if not self._connected_event.wait(self.timeout_seconds):
            detail = self._last_error_text()
            if detail:
                raise TimeoutError(f"IBKR connection timed out before nextValidId: {detail}")
            raise TimeoutError("IBKR connection timed out before nextValidId")

    def currentTime(self, time_: int) -> None:
        self._last_broker_time = int(time_)
        self._current_time_event.set()
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.CONNECTION,
                event_time=datetime.fromtimestamp(time_, tz=UTC),
                payload={"state": "HEARTBEAT", "broker_time": time_},
            )
        )

    def request_current_time(self) -> int:
        self._current_time_event.clear()
        self.reqCurrentTime()
        self._wait_event(self._current_time_event, "broker heartbeat")
        if self._last_broker_time is None:
            raise RuntimeError("IBKR heartbeat callback did not include broker time")
        return self._last_broker_time

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
        self._historical[req_id] = []
        self._historical_done[req_id] = Event()
        self._request_errors[req_id] = []
        try:
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
            self._wait(
                self._historical_done[req_id],
                f"historical bars {what_to_show}",
                req_id=req_id,
            )
            rows = list(self._historical[req_id])
            if not rows:
                raise IBKRNoDataError(f"IBKR returned no historical bars for request {req_id}")
            return rows
        except Exception:
            with suppress(Exception):
                self.cancelHistoricalData(req_id)
            raise
        finally:
            self._historical.pop(req_id, None)
            self._historical_done.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def request_snapshot(self, contract: Any) -> dict[str, Any]:
        req_id = self._next_id()
        self._snapshots[req_id] = {}
        self._snapshot_done[req_id] = Event()
        self._request_errors[req_id] = []
        try:
            # 设置快照标志时 IBKR 会拒绝通用 tick，例如 RTVolume 233；这里保持合法快照，并在
            # 规范化报价中明确标记以接收时间作为后备时间戳。
            self.reqMktData(req_id, contract, "", True, False, [])
            self._wait(self._snapshot_done[req_id], "snapshot quote", req_id=req_id)
            snapshot = dict(self._snapshots[req_id])
            if not snapshot:
                raise IBKRNoDataError(f"IBKR returned an empty snapshot for request {req_id}")
            snapshot.setdefault("market_data_type", self.current_market_data_type)
            return snapshot
        except Exception:
            with suppress(Exception):
                self.cancelMktData(req_id)
            raise
        finally:
            self._snapshots.pop(req_id, None)
            self._snapshot_done.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def request_tradeable_quote(self, contract: Any) -> dict[str, Any]:
        req_id = self._next_id()
        self._tradeable_quotes[req_id] = {}
        self._tradeable_quote_done[req_id] = Event()
        self._request_errors[req_id] = []
        try:
            # 236 请求可卖空指标，49 是默认观察列表的停牌状态 tick；使用流式订阅是因为快照
            # 无法稳定返回可用的停牌状态。
            self.reqMktData(req_id, contract, "236", False, False, [])
            self._wait(
                self._tradeable_quote_done[req_id],
                "tradeable streaming quote",
                req_id=req_id,
            )
            return dict(self._tradeable_quotes[req_id])
        finally:
            with suppress(Exception):
                self.cancelMktData(req_id)
            self._tradeable_quotes.pop(req_id, None)
            self._tradeable_quote_done.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def _quote_target(self, req_id: int) -> dict[str, Any] | None:
        if req_id in self._snapshot_done:
            return self._snapshots.setdefault(req_id, {})
        if req_id in self._tradeable_quote_done:
            return self._tradeable_quotes.setdefault(req_id, {})
        return None

    def _maybe_complete_tradeable_quote(self, req_id: int) -> None:
        event = getattr(self, "_tradeable_quote_done", {}).get(req_id)
        if event is None:
            return
        quote = self._tradeable_quotes.get(req_id, {})
        if all(
            quote.get(field) is not None
            for field in ("bid", "ask", "market_data_type", "halted_status")
        ):
            event.set()

    def request_contract_details(self, contract: Any) -> list[Any]:
        req_id = self._next_id()
        self._contract_details[req_id] = []
        self._contract_details_done[req_id] = Event()
        self._request_errors[req_id] = []
        try:
            self.reqContractDetails(req_id, contract)
            self._wait(self._contract_details_done[req_id], "contract details", req_id=req_id)
            return list(self._contract_details[req_id])
        finally:
            self._contract_details.pop(req_id, None)
            self._contract_details_done.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def request_sec_def_option_params(
        self,
        *,
        symbol: str,
        underlying_conid: int,
        underlying_sec_type: str,
    ) -> list[dict[str, Any]]:
        req_id = self._next_id()
        self._option_params[req_id] = []
        self._option_params_done[req_id] = Event()
        self._request_errors[req_id] = []
        try:
            self.reqSecDefOptParams(req_id, symbol, "", underlying_sec_type, underlying_conid)
            self._wait(self._option_params_done[req_id], "option parameters", req_id=req_id)
            return list(self._option_params[req_id])
        finally:
            self._option_params.pop(req_id, None)
            self._option_params_done.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def request_managed_accounts(self) -> list[str]:
        if not self._managed_accounts_event.wait(self.timeout_seconds):
            raise TimeoutError("IBKR managed accounts request timed out")
        if not self._managed_accounts:
            raise RuntimeError("IBKR API session did not return any managed accounts")
        return list(self._managed_accounts)

    def request_account_summary(self, *, tags: Sequence[str]) -> list[dict[str, str]]:
        req_id = self._next_id()
        self._account_summaries[req_id] = []
        self._account_summaries_done[req_id] = Event()
        self._request_errors[req_id] = []
        try:
            self.reqAccountSummary(req_id, "All", ",".join(tags))
            self._wait(self._account_summaries_done[req_id], "account summary", req_id=req_id)
            return list(self._account_summaries[req_id])
        finally:
            with suppress(Exception):
                self.cancelAccountSummary(req_id)
            self._account_summaries.pop(req_id, None)
            self._account_summaries_done.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def request_positions(self) -> list[dict[str, Any]]:
        with self._positions_query_lock:
            self._positions = []
            self._positions_done.clear()
            self.reqPositions()
            try:
                self._wait_event(self._positions_done, "positions")
                return list(self._positions)
            finally:
                with suppress(Exception):
                    self.cancelPositions()

    def request_pnl_snapshot(self, *, account: str) -> dict[str, Any]:
        req_id = self._next_id()
        self._pnl[req_id] = {}
        self._pnl_done[req_id] = Event()
        self._pnl_accounts[req_id] = account
        self._request_errors[req_id] = []
        try:
            self.reqPnL(req_id, account, "")
            self._wait(self._pnl_done[req_id], "account PnL", req_id=req_id)
            row = dict(self._pnl[req_id])
            if not row:
                raise IBKRNoDataError(f"IBKR returned no PnL for account {account}")
            return row
        finally:
            with suppress(Exception):
                self.cancelPnL(req_id)
            self._pnl.pop(req_id, None)
            self._pnl_done.pop(req_id, None)
            self._pnl_accounts.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def request_pnl_single_snapshot(self, *, account: str, conid: int) -> dict[str, Any]:
        req_id = self._next_id()
        self._pnl_single[req_id] = {}
        self._pnl_single_done[req_id] = Event()
        self._pnl_single_accounts[req_id] = account
        self._request_errors[req_id] = []
        try:
            self.reqPnLSingle(req_id, account, "", conid)
            self._wait(self._pnl_single_done[req_id], "position PnL", req_id=req_id)
            row = dict(self._pnl_single[req_id])
            if not row:
                raise IBKRNoDataError(
                    f"IBKR returned no position PnL for account {account}, conid {conid}"
                )
            return row
        finally:
            with suppress(Exception):
                self.cancelPnLSingle(req_id)
            self._pnl_single.pop(req_id, None)
            self._pnl_single_done.pop(req_id, None)
            self._pnl_single_accounts.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def start_pnl_subscription(self, *, account: str) -> int:
        req_id = self._next_id()
        self._pnl[req_id] = {}
        self._pnl_done[req_id] = Event()
        self._pnl_accounts[req_id] = account
        self._request_errors[req_id] = []
        self.reqPnL(req_id, account, "")
        return req_id

    def stop_pnl_subscription(self, req_id: int) -> None:
        if req_id not in self._pnl_accounts:
            return
        self.cancelPnL(req_id)
        self._pnl.pop(req_id, None)
        self._pnl_done.pop(req_id, None)
        self._pnl_accounts.pop(req_id, None)
        self._request_errors.pop(req_id, None)

    def request_market_rule(self, market_rule_id: int) -> list[dict[str, Any]]:
        if market_rule_id <= 0:
            raise ValueError("market_rule_id must be positive")
        self._market_rules[market_rule_id] = []
        self._market_rule_done[market_rule_id] = Event()
        self.reqMarketRule(market_rule_id)
        try:
            self._wait_event(self._market_rule_done[market_rule_id], "market rule")
            return list(self._market_rules[market_rule_id])
        finally:
            self._market_rules.pop(market_rule_id, None)
            self._market_rule_done.pop(market_rule_id, None)

    def request_global_cancel(self) -> None:
        self.reqGlobalCancel()

    def request_exercise_option(
        self,
        *,
        contract: Any,
        action: int,
        quantity: int,
        account: str,
        override: bool,
    ) -> int:
        req_id = self._next_id()
        self._request_errors[req_id] = []
        self.exerciseOptions(
            req_id,
            contract,
            action,
            quantity,
            account,
            1 if override else 0,
        )
        self._emit(
            BrokerEvent(
                event_type=BrokerEventType.OPTION_LIFECYCLE,
                account=account,
                payload={
                    "request_id": req_id,
                    "conid": getattr(contract, "conId", None),
                    "action": "EXERCISE" if action == 1 else "LAPSE",
                    "quantity": quantity,
                    "override": override,
                    "state": "REQUESTED",
                },
            )
        )
        return req_id

    def submit_order(self, *, contract: Any, order: Any, order_id: int | None) -> dict[str, Any]:
        selected_order_id = order_id if order_id is not None else self._allocate_order_id()
        if selected_order_id < 0:
            raise ValueError("IBKR order_id must be non-negative")
        event = self._order_events.setdefault(selected_order_id, Event())
        event.clear()
        self._request_errors[selected_order_id] = []
        record = _raw_order_record(selected_order_id, contract, order, None)
        record["status"] = "PendingSubmit"
        record["acknowledged"] = False
        self._orders[selected_order_id] = record
        self.placeOrder(selected_order_id, contract, order)
        try:
            return self._wait_for_order(
                selected_order_id,
                lambda row: bool(row.get("acknowledged")) and _order_record_matches(row, order),
                label="order acknowledgement",
            )
        except Exception:
            if not bool(getattr(order, "whatIf", False)):
                with suppress(Exception):
                    self.cancelOrder(selected_order_id)
            raise

    def reserve_order_ids(self, count: int) -> list[int]:
        if count <= 0:
            raise ValueError("order ID reservation count must be positive")
        if self._next_order_id is None:
            self._order_id_event.clear()
            self.reqIds(count)
            if not self._order_id_event.wait(self.timeout_seconds):
                raise TimeoutError("IBKR next valid order ID request timed out")
        with self._lock:
            if self._next_order_id is None:
                raise RuntimeError("IBKR did not provide a valid order ID")
            start = self._next_order_id
            self._next_order_id += count
            return list(range(start, start + count))

    def submit_order_batch(self, batch: Sequence[tuple[int, Any, Any]]) -> list[dict[str, Any]]:
        if not batch:
            raise ValueError("order batch cannot be empty")
        order_ids = [order_id for order_id, _, _ in batch]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("order batch contains duplicate order IDs")
        for order_id, contract, order in batch:
            event = self._order_events.setdefault(order_id, Event())
            event.clear()
            self._request_errors[order_id] = []
            record = _raw_order_record(order_id, contract, order, None)
            record.update({"status": "PendingSubmit", "acknowledged": False})
            self._orders[order_id] = record
        try:
            for order_id, contract, order in batch:
                self.placeOrder(order_id, contract, order)
            return [
                self._wait_for_order(
                    order_id,
                    lambda row, expected=order: (
                        bool(row.get("acknowledged")) and _order_record_matches(row, expected)
                    ),
                    label="linked order acknowledgement",
                )
                for order_id, _, order in batch
            ]
        except Exception:
            for order_id in order_ids:
                with suppress(Exception):
                    self.cancelOrder(order_id)
            raise

    def request_cancel_order(self, order_id: int) -> dict[str, Any]:
        record = self._orders.setdefault(
            order_id, {"order_id": order_id, "status": "PendingCancel"}
        )
        if record.get("status") in {"Cancelled", "ApiCancelled", "Filled", "Inactive"}:
            return dict(record)
        event = self._order_events.setdefault(order_id, Event())
        event.clear()
        self._request_errors[order_id] = []
        self.cancelOrder(order_id)
        return self._wait_for_order(
            order_id,
            lambda row: row.get("status") in {"Cancelled", "ApiCancelled", "Filled", "Inactive"},
            label="order cancellation",
        )

    def request_open_orders(self, *, all_clients: bool) -> list[dict[str, Any]]:
        with self._open_orders_query_lock:
            self._open_orders = {}
            self._open_orders_done.clear()
            self._collecting_open_orders = True
            try:
                if all_clients:
                    self.reqAllOpenOrders()
                else:
                    self.reqOpenOrders()
                self._wait_event(self._open_orders_done, "open orders")
                return [dict(row) for row in self._open_orders.values()]
            finally:
                self._collecting_open_orders = False

    def order_snapshot(self, order_id: int) -> dict[str, Any] | None:
        row = self._orders.get(order_id)
        return dict(row) if row is not None else None

    def request_completed_orders(self, *, api_only: bool) -> list[dict[str, Any]]:
        with self._completed_orders_query_lock:
            self._completed_orders = {}
            self._completed_orders_done.clear()
            self._collecting_completed_orders = True
            try:
                self.reqCompletedOrders(api_only)
                self._wait_event(self._completed_orders_done, "completed orders")
                return [dict(row) for row in self._completed_orders.values()]
            finally:
                self._collecting_completed_orders = False

    def request_executions(
        self,
        *,
        account: str,
        since: datetime | None,
        symbol: str | None,
        client_id: int | None,
    ) -> list[dict[str, Any]]:
        from ibapi.execution import ExecutionFilter

        req_id = self._next_id()
        execution_filter = ExecutionFilter()
        if client_id is not None:
            execution_filter.clientId = client_id
        execution_filter.acctCode = account
        execution_filter.symbol = (symbol or "").upper()
        if since is not None:
            execution_filter.time = since.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")
        self._executions[req_id] = []
        self._executions_done[req_id] = Event()
        self._request_errors[req_id] = []
        try:
            self.reqExecutions(req_id, execution_filter)
            self._wait(self._executions_done[req_id], "executions", req_id=req_id)
            rows: list[dict[str, Any]] = []
            for row in self._executions[req_id]:
                execution_id = str(row["execution"].execId)
                rows.append({**row, "commission_report": self._commissions.get(execution_id)})
            return rows
        finally:
            self._executions.pop(req_id, None)
            self._executions_done.pop(req_id, None)
            self._request_errors.pop(req_id, None)

    def _allocate_order_id(self) -> int:
        if self._next_order_id is None:
            self._order_id_event.clear()
            self.reqIds(1)
            if not self._order_id_event.wait(self.timeout_seconds):
                raise TimeoutError("IBKR next valid order ID request timed out")
        with self._lock:
            if self._next_order_id is None:
                raise RuntimeError("IBKR did not provide a valid order ID")
            order_id = self._next_order_id
            self._next_order_id += 1
            return order_id

    def _observe_order_id(self, order_id: int) -> None:
        if order_id < 0:
            return
        lock = getattr(self, "_lock", None)
        if lock is None:
            current = getattr(self, "_next_order_id", None)
            if current is None or order_id >= current:
                self._next_order_id = order_id + 1
            return
        with lock:
            current = getattr(self, "_next_order_id", None)
            if current is None or order_id >= current:
                self._next_order_id = order_id + 1

    def _emit(self, event: BrokerEvent) -> None:
        handler = getattr(self, "_event_handler", None)
        if handler is None:
            return
        try:
            handler(event)
        except Exception:  # noqa: BLE001 - 绝不能终止 IBKR 网络读取线程。
            return

    def _next_id(self) -> int:
        with self._lock:
            self._next_req_id += 1
            return self._next_req_id

    def _wait(self, event: Event, label: str, *, req_id: int) -> None:
        deadline = time() + self.timeout_seconds
        while time() < deadline:
            if event.wait(0.05):
                self._raise_request_error(req_id)
                return
        detail = self._last_error_text()
        if detail:
            raise TimeoutError(f"IBKR request timed out: {label}: {detail}")
        raise TimeoutError(f"IBKR request timed out: {label}")

    def _wait_event(self, event: Event, label: str) -> None:
        if event.wait(self.timeout_seconds):
            return
        detail = self._last_error_text()
        if detail:
            raise TimeoutError(f"IBKR request timed out: {label}: {detail}")
        raise TimeoutError(f"IBKR request timed out: {label}")

    def _wait_for_order(
        self,
        order_id: int,
        predicate: Any,
        *,
        label: str,
    ) -> dict[str, Any]:
        event = self._order_events[order_id]
        deadline = time() + self.timeout_seconds
        while time() < deadline:
            self._raise_request_error(order_id)
            record = self._orders.get(order_id, {})
            if predicate(record):
                return dict(record)
            event.wait(0.05)
            event.clear()
        self._raise_request_error(order_id)
        detail = self._last_error_text()
        if detail:
            raise TimeoutError(f"IBKR request timed out: {label}: {detail}")
        raise TimeoutError(f"IBKR request timed out: {label}")

    def _raise_request_error(self, req_id: int) -> None:
        request_errors = self._request_errors.get(req_id, [])
        if request_errors:
            code, message, advanced = request_errors[-1]
            raise IBKRRequestError(
                req_id=req_id,
                code=code,
                message=message,
                advanced_order_reject_json=advanced,
            )

    def verify_api_handshake(self, host: str, port: int) -> None:
        probe_timeout = min(3.0, max(1.0, float(self.timeout_seconds)))
        try:
            with socket.create_connection(
                (host, int(port)), timeout=min(2.0, probe_timeout)
            ) as sock:
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
                        raise ConnectionError(
                            f"IBKR API socket returned unexpected handshake response: {fields!r}"
                        )
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
        raise ConnectionError(
            f"IBKR API socket closed before completing the API handshake at {host}:{port}."
        )

    def _last_error_text(self) -> str | None:
        if not self.errors:
            return None
        req_id, error_time, code, text = self.errors[-1]
        if error_time is not None:
            return f"reqId={req_id} errorTime={error_time} code={code} msg={text}"
        return f"reqId={req_id} code={code} msg={text}"

    def _complete_on_error(
        self,
        req_id: int,
        code: int,
        text: str,
        advanced_order_reject_json: str | None = None,
    ) -> None:
        ignorable = {2104, 2106, 2158, 2157, 2176}
        if code in ignorable:
            return
        if code == 202 and req_id in getattr(self, "_orders", {}):
            self._orders[req_id].update(
                {
                    "acknowledged": True,
                    "status": "Cancelled",
                    "updated_at": datetime.now(UTC),
                }
            )
            event = getattr(self, "_order_events", {}).get(req_id)
            if event is not None:
                event.set()
            return
        known_request = any(
            req_id in events
            for events in (
                getattr(self, "_historical_done", {}),
                getattr(self, "_snapshot_done", {}),
                getattr(self, "_tradeable_quote_done", {}),
                getattr(self, "_contract_details_done", {}),
                getattr(self, "_option_params_done", {}),
                getattr(self, "_account_summaries_done", {}),
                getattr(self, "_executions_done", {}),
                getattr(self, "_pnl_done", {}),
                getattr(self, "_pnl_single_done", {}),
                getattr(self, "_order_events", {}),
            )
        )
        if known_request:
            self._request_errors.setdefault(req_id, []).append(
                (code, text, advanced_order_reject_json)
            )
        event = self._historical_done.get(req_id)
        if event is not None:
            event.set()
        event = self._snapshot_done.get(req_id)
        if event is not None:
            event.set()
        event = getattr(self, "_tradeable_quote_done", {}).get(req_id)
        if event is not None:
            event.set()
        event = self._contract_details_done.get(req_id)
        if event is not None:
            event.set()
        event = self._option_params_done.get(req_id)
        if event is not None:
            event.set()
        event = getattr(self, "_account_summaries_done", {}).get(req_id)
        if event is not None:
            event.set()
        event = getattr(self, "_executions_done", {}).get(req_id)
        if event is not None:
            event.set()
        event = getattr(self, "_pnl_done", {}).get(req_id)
        if event is not None:
            event.set()
        event = getattr(self, "_pnl_single_done", {}).get(req_id)
        if event is not None:
            event.set()
        event = getattr(self, "_order_events", {}).get(req_id)
        if event is not None:
            event.set()

    def disconnect_and_stop(self) -> None:
        for req_id in list(getattr(self, "_pnl_accounts", {})):
            with suppress(Exception):
                self.cancelPnL(req_id)
        with suppress(Exception):
            if self.isConnected():
                self.disconnect()


def _stock_contract(instrument: str | InstrumentRef) -> Any:
    Contract = _contract_class()
    contract = Contract()
    if isinstance(instrument, str):
        instrument = InstrumentRef(
            asset_type=_asset_type_for_symbol(instrument), symbol=instrument.upper()
        )
    contract.symbol = instrument.symbol.upper()
    contract.secType = "STK"
    contract.exchange = instrument.venue or "SMART"
    contract.currency = instrument.currency
    if instrument.conid:
        contract.conId = int(instrument.conid)
    primary_exchange = instrument.metadata.get("primary_exchange")
    if primary_exchange:
        contract.primaryExchange = str(primary_exchange)
    return contract


def _option_contract(instrument: InstrumentRef) -> Any:
    Contract = _contract_class()
    contract = Contract()
    contract.symbol = instrument.symbol.upper()
    contract.secType = "OPT"
    contract.exchange = instrument.venue or "SMART"
    contract.currency = instrument.currency
    contract.lastTradeDateOrContractMonth = _expiry_yyyymmdd(instrument.expiry)
    contract.strike = float(instrument.strike)
    contract.right = "C" if instrument.option_right == "CALL" else "P"
    contract.multiplier = str(instrument.metadata.get("multiplier", "100"))
    if instrument.metadata.get("trading_class"):
        contract.tradingClass = str(instrument.metadata["trading_class"])
    if instrument.metadata.get("local_symbol"):
        contract.localSymbol = str(instrument.metadata["local_symbol"])
    if instrument.conid:
        contract.conId = int(instrument.conid)
    return contract


def _combo_contract(instrument: InstrumentRef) -> Any:
    try:
        from ibapi.contract import ComboLeg
    except ImportError as exc:
        raise RuntimeError(
            "Install the starter with the 'ibkr' extra to use IBKR combos: pip install -e '.[ibkr]'"
        ) from exc
    Contract = _contract_class()
    contract = Contract()
    contract.symbol = instrument.symbol.upper()
    contract.secType = "BAG"
    contract.exchange = instrument.venue or "SMART"
    contract.currency = instrument.currency
    contract.multiplier = str(instrument.metadata.get("multiplier", "100"))
    contract.comboLegs = []
    for payload in instrument.metadata.get("combo_legs", []):
        leg = ComboLeg()
        leg.conId = int(payload["conid"])
        leg.ratio = int(payload["ratio"])
        leg.action = str(payload["action"]).upper()
        leg.exchange = str(payload.get("exchange") or contract.exchange)
        leg.openClose = int(payload.get("open_close", 0))
        contract.comboLegs.append(leg)
    if len(contract.comboLegs) < 2:
        raise ValueError("IBKR BAG contract requires at least two valid combo legs")
    return contract


def _contract_for_instrument(instrument: InstrumentRef) -> Any:
    if instrument.asset_type == AssetType.COMBO:
        return _combo_contract(instrument)
    if instrument.asset_type == AssetType.OPTION:
        return _option_contract(instrument)
    return _stock_contract(instrument)


def _broker_order(request: BrokerOrderRequest) -> Any:
    try:
        from ibapi.order import Order
    except ImportError as exc:
        raise RuntimeError(
            "Install the starter with the 'ibkr' extra to use IBKRAdapter: pip install -e '.[ibkr]'"
        ) from exc
    order = Order()
    order.account = request.account or ""
    order.action = request.side
    order.orderType = "STP LMT" if request.order_type == "STP_LMT" else request.order_type
    order.totalQuantity = request.quantity
    if request.limit_price is not None:
        order.lmtPrice = float(request.limit_price)
    if request.stop_price is not None:
        order.auxPrice = float(request.stop_price)
    order.tif = request.tif
    order.transmit = request.transmit
    order.whatIf = request.what_if
    order.outsideRth = request.outside_rth
    order.orderRef = request.order_ref or ""
    if request.good_after_time is not None:
        order.goodAfterTime = _ib_order_datetime(request.good_after_time)
    if request.good_till_date is not None:
        order.goodTillDate = _ib_order_datetime(request.good_till_date)
    if request.parent_order_id is not None:
        order.parentId = request.parent_order_id
    if request.oca_group:
        order.ocaGroup = request.oca_group
    if request.oca_type is not None:
        order.ocaType = request.oca_type
    # 较旧 ibapi 默认开启这些遗留标志，但当前 TWS 会拒绝它们。
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    return order


def _raw_order_record(order_id: int, contract: Any, order: Any, order_state: Any) -> dict[str, Any]:
    status = ""
    if order_state is not None:
        status = getattr(order_state, "status", "") or getattr(order_state, "completedStatus", "")
    quantity = _finite_decimal_or_none(getattr(order, "totalQuantity", None))
    filled = _finite_decimal_or_none(getattr(order, "filledQuantity", None)) or Decimal(0)
    remaining = max(Decimal(0), quantity - filled) if quantity is not None else Decimal(0)
    is_combo = str(getattr(contract, "secType", "") or "").upper() == "BAG"
    return {
        "order_id": int(order_id),
        "status": status or "PendingSubmit",
        "instrument": _instrument_from_contract(contract),
        "account": getattr(order, "account", "") or None,
        "side": getattr(order, "action", "") or None,
        "order_type": (getattr(order, "orderType", "") or "").replace(" ", "_") or None,
        "quantity": quantity,
        "limit_price": _order_limit_price(order, is_combo=is_combo),
        "stop_price": _finite_positive_decimal_or_none(getattr(order, "auxPrice", None)),
        "tif": getattr(order, "tif", "") or None,
        "filled": filled,
        "remaining": remaining,
        "avg_fill_price": Decimal(0),
        "last_fill_price": Decimal(0),
        "permanent_id": _positive_int_or_none(getattr(order, "permId", None)),
        "client_id": _non_negative_int_or_none(getattr(order, "clientId", None)),
        "parent_id": _positive_int_or_none(getattr(order, "parentId", None)),
        "why_held": getattr(order_state, "whyHeld", "") or None,
        "order_ref": getattr(order, "orderRef", "") or None,
        "initial_margin_change": _finite_decimal_or_none(
            getattr(order_state, "initMarginChange", None)
        ),
        "maintenance_margin_change": _finite_decimal_or_none(
            getattr(order_state, "maintMarginChange", None)
        ),
        "equity_with_loan_change": _finite_decimal_or_none(
            getattr(order_state, "equityWithLoanChange", None)
        ),
        "warning_text": getattr(order_state, "warningText", "") or None,
        "updated_at": datetime.now(UTC),
    }


def _order_event_payload(record: dict[str, Any]) -> dict[str, Any]:
    """稳定的回调载荷：有意排除本地接收时间戳。"""

    return {
        key: value for key, value in record.items() if key not in {"updated_at", "acknowledged"}
    }


def _order_record_matches(row: dict[str, Any], order: Any) -> bool:
    expected_type = (getattr(order, "orderType", "") or "").replace(" ", "_") or None
    expected_quantity = _finite_decimal_or_none(getattr(order, "totalQuantity", None))
    instrument = row.get("instrument")
    is_combo = instrument is not None and getattr(instrument, "asset_type", None) == AssetType.COMBO
    expected_limit = _order_limit_price(order, is_combo=is_combo)
    expected_stop = _finite_positive_decimal_or_none(getattr(order, "auxPrice", None))
    return all(
        (
            row.get("account") == (getattr(order, "account", "") or None),
            row.get("side") == (getattr(order, "action", "") or None),
            row.get("order_type") == expected_type,
            row.get("quantity") == expected_quantity,
            row.get("limit_price") == expected_limit,
            row.get("stop_price") == expected_stop,
            row.get("tif") == (getattr(order, "tif", "") or None),
            row.get("order_ref") == (getattr(order, "orderRef", "") or None),
        )
    )


def _order_identity_matches(
    row: dict[str, Any],
    *,
    order_id: int,
    account: str | None,
    client_id: int | None,
) -> bool:
    if row.get("order_id") != order_id:
        return False
    if account is not None and row.get("account") != account:
        return False
    if client_id is not None and row.get("client_id") != client_id:
        return False
    return True


def _broker_order_status(row: dict[str, Any]) -> BrokerOrderStatus:
    fields = BrokerOrderStatus.model_fields
    payload = {key: value for key, value in row.items() if key in fields}
    payload.setdefault("status", "Unknown")
    payload.setdefault("updated_at", datetime.now(UTC))
    return BrokerOrderStatus(**payload)


def _instrument_from_contract(contract: Any) -> InstrumentRef:
    symbol = str(getattr(contract, "symbol", "") or getattr(contract, "localSymbol", ""))
    sec_type = str(getattr(contract, "secType", "") or "").upper()
    common = {
        "symbol": symbol.upper(),
        "currency": str(getattr(contract, "currency", "USD") or "USD"),
        "venue": str(getattr(contract, "exchange", "") or "") or None,
        "conid": _positive_int_or_none(getattr(contract, "conId", None)),
    }
    if sec_type == "OPT":
        return InstrumentRef(
            asset_type=AssetType.OPTION,
            option_right="CALL" if str(getattr(contract, "right", "")).upper() == "C" else "PUT",
            strike=Decimal(str(contract.strike)),
            expiry=_parse_expiry(str(contract.lastTradeDateOrContractMonth)[:8]),
            **common,
        )
    if sec_type == "BAG":
        legs = [
            {
                "conid": int(leg.conId),
                "ratio": int(leg.ratio),
                "action": str(leg.action).upper(),
                "exchange": str(getattr(leg, "exchange", "") or "SMART"),
                "open_close": int(getattr(leg, "openClose", 0) or 0),
            }
            for leg in (getattr(contract, "comboLegs", None) or [])
        ]
        return InstrumentRef(
            asset_type=AssetType.COMBO,
            metadata={
                "combo_legs": legs,
                "multiplier": str(getattr(contract, "multiplier", "") or "100"),
                "broker_unresolved_combo": not legs,
            },
            **common,
        )
    return InstrumentRef(asset_type=_asset_type_for_symbol(symbol), **common)


def _broker_execution(row: dict[str, Any]) -> BrokerExecution:
    execution = row["execution"]
    report = row.get("commission_report") or {}
    raw_side = str(execution.side).upper()
    side = {"BOT": "BUY", "SLD": "SELL"}.get(raw_side, raw_side)
    return BrokerExecution(
        execution_id=str(execution.execId),
        order_id=int(execution.orderId),
        permanent_id=_positive_int_or_none(getattr(execution, "permId", None)),
        client_id=_non_negative_int_or_none(getattr(execution, "clientId", None)),
        account=str(execution.acctNumber),
        instrument=_instrument_from_contract(row["contract"]),
        side=side,
        quantity=Decimal(str(execution.shares)),
        price=Decimal(str(execution.price)),
        executed_at=_parse_execution_datetime(str(execution.time)),
        exchange=str(getattr(execution, "exchange", "") or "") or None,
        order_ref=str(getattr(execution, "orderRef", "") or "") or None,
        commission=_finite_decimal_or_none(report.get("commission")),
        commission_currency=report.get("currency") or None,
        realized_pnl=_finite_decimal_or_none(report.get("realized_pnl")),
    )


def _contract_class() -> type:
    try:
        from ibapi.contract import Contract
    except ImportError as exc:
        raise RuntimeError(
            "Install the starter with the 'ibkr' extra to use IBKRAdapter: pip install -e '.[ibkr]'"
        ) from exc
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


def _ib_order_datetime(value: datetime) -> str:
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


def _parse_execution_datetime(value: str) -> datetime:
    normalized = " ".join(value.split())
    parts = normalized.split(" ")
    if len(parts) >= 3:
        try:
            from zoneinfo import ZoneInfo

            parsed = datetime.strptime(" ".join(parts[:2]), "%Y%m%d %H:%M:%S")
            return parsed.replace(tzinfo=ZoneInfo(parts[2])).astimezone(UTC)
        except (ValueError, KeyError):
            pass
    for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _parse_expiry(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _expiry_yyyymmdd(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    return str(value).replace("-", "")[:8]


def _select_strikes(
    strikes: Sequence[float], underlying_price: Decimal | None, *, max_per_side: int
) -> list[Decimal]:
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


def _positive_decimal_or_none(value: Any) -> Decimal | None:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _non_negative_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    return parsed if parsed >= 0 else None


def _positive_int_or_none(value: Any) -> int | None:
    parsed = _non_negative_int_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _finite_decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed_float = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(parsed_float) or abs(parsed_float) > 1e100:
        return None
    return Decimal(str(value))


def _finite_positive_decimal_or_none(value: Any) -> Decimal | None:
    parsed = _finite_decimal_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _order_limit_price(order: Any, *, is_combo: bool) -> Decimal | None:
    value = _finite_decimal_or_none(getattr(order, "lmtPrice", None))
    if is_combo:
        return value if value is not None and value != 0 else None
    return value if value is not None and value > 0 else None


def _required_account_decimal(tag: str, value: Any) -> Decimal:
    parsed = _finite_decimal_or_none(value)
    if parsed is None:
        raise RuntimeError(f"IBKR account value {tag} is unavailable or non-finite")
    return parsed


def _csv_values(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _instrument_risk_key(instrument: InstrumentRef) -> str:
    if instrument.conid:
        return f"conid:{instrument.conid}"
    return ":".join(
        str(value or "")
        for value in (
            instrument.asset_type.value,
            instrument.symbol,
            instrument.currency,
            instrument.venue,
            instrument.expiry,
            instrument.option_right,
            instrument.strike,
        )
    )


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
