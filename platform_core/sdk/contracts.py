from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from platform_core.schemas import BrokerOrderRequest
from platform_core.schemas.assets import AssetType

from .models import QualifiedContract


class ContractRuleError(ValueError):
    pass


class ContractRulesSDK:
    """路由前解析合约，并强制执行经纪商价格与数量增量规则。"""

    def __init__(self, broker: Any, *, cache_ttl_seconds: int = 3600) -> None:
        self.broker = broker
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self._lock = RLock()
        self._cache: dict[str, tuple[datetime, QualifiedContract]] = {}
        self._market_rules: dict[int, list[dict[str, Decimal]]] = {}

    def qualify_and_validate(
        self,
        request: BrokerOrderRequest,
        *,
        require_complete: bool,
        require_open_session: bool = False,
        now: datetime | None = None,
    ) -> BrokerOrderRequest:
        qualified = self.qualify(request)
        metadata = {**request.instrument.metadata, **qualified.instrument.metadata}
        if request.instrument.asset_type == AssetType.COMBO:
            metadata["combo_legs"] = request.instrument.metadata["combo_legs"]
            metadata["max_loss_per_unit"] = request.instrument.metadata[
                "max_loss_per_unit"
            ]
        instrument = qualified.instrument.model_copy(
            update={
                "asset_type": request.instrument.asset_type,
                "metadata": metadata,
            }
        )
        if (
            require_complete
            and instrument.asset_type != AssetType.COMBO
            and not instrument.conid
        ):
            raise ContractRuleError("live routing requires an unambiguous IBKR conid")
        if request.instrument.conid and request.instrument.conid != instrument.conid:
            raise ContractRuleError("qualified conid differs from requested conid")
        supported = {
            _canonical_order_type(value) for value in qualified.supported_order_types
        }
        if require_complete and not supported:
            raise ContractRuleError("IBKR did not return supported order types")
        if supported and _canonical_order_type(request.order_type) not in supported:
            raise ContractRuleError(
                f"{request.order_type} is not supported for qualified contract {instrument.conid}"
            )
        self._validate_size(
            request.quantity,
            minimum=qualified.min_size,
            increment=qualified.size_increment,
        )
        for label, price in (
            ("limit_price", request.limit_price),
            ("stop_price", request.stop_price),
        ):
            if price is not None:
                increment = self._price_increment(abs(price), qualified)
                if not _is_multiple(price, increment):
                    raise ContractRuleError(
                        f"{label}={price} is not aligned to tick increment {increment}"
                    )
        if require_open_session:
            hours = qualified.trading_hours if request.outside_rth else qualified.liquid_hours
            if not _is_in_trading_session(
                now or datetime.now(UTC),
                hours=hours,
                time_zone_id=qualified.time_zone_id,
            ):
                raise ContractRuleError("contract is outside its permitted trading session")
        return request.model_copy(update={"instrument": instrument})

    def qualify(self, request: BrokerOrderRequest) -> QualifiedContract:
        key = request.instrument.model_dump_json()
        now = datetime.now(UTC)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < self.cache_ttl:
                return cached[1]
        qualified = self.broker.qualify_contract(request.instrument)
        with self._lock:
            self._cache[key] = (now, qualified)
        return qualified

    def _price_increment(
        self, price: Decimal, qualified: QualifiedContract
    ) -> Decimal:
        increments: list[Decimal] = []
        for rule_id in qualified.market_rule_ids:
            if rule_id <= 0:
                continue
            with self._lock:
                rows = self._market_rules.get(rule_id)
            if rows is None:
                rows = self.broker.market_rule(rule_id)
                with self._lock:
                    self._market_rules[rule_id] = rows
            eligible = [row for row in rows if row["low_edge"] <= price]
            if eligible:
                selected = max(eligible, key=lambda row: row["low_edge"])
                increments.append(selected["increment"])
        # 各交易所价格增量不一致时采用最大值，确保订单对所有已发布路由都合法，而不依赖 SMART 取整。
        return max([qualified.min_tick, *increments])

    @staticmethod
    def _validate_size(quantity: Decimal, *, minimum: Decimal, increment: Decimal) -> None:
        if quantity < minimum:
            raise ContractRuleError(f"quantity {quantity} is below minimum size {minimum}")
        if not _is_multiple(quantity - minimum, increment):
            raise ContractRuleError(
                f"quantity {quantity} is not aligned to size increment {increment}"
            )


def _is_multiple(value: Decimal, increment: Decimal) -> bool:
    if increment <= 0:
        raise ContractRuleError("broker increment must be positive")
    return value % increment == 0


def _canonical_order_type(value: str) -> str:
    return value.replace(" ", "").replace("_", "").upper()


def _is_in_trading_session(
    now: datetime, *, hours: str | None, time_zone_id: str | None
) -> bool:
    if not hours or not time_zone_id:
        return False
    try:
        local_now = now.astimezone(ZoneInfo(time_zone_id))
    except (KeyError, ValueError):
        return False
    for segment in hours.split(";"):
        if not segment or "CLOSED" in segment.upper() or ":" not in segment:
            continue
        session_date, ranges = segment.split(":", 1)
        for time_range in ranges.split(","):
            if "-" not in time_range:
                continue
            start_text, end_text = time_range.split("-", 1)
            try:
                start = _parse_session_point(start_text, local_now.tzinfo, session_date)
                end = _parse_session_point(end_text, local_now.tzinfo, session_date)
            except ValueError:
                continue
            if start <= local_now < end:
                return True
    return False


def _parse_session_point(value: str, zone: Any, session_date: str) -> datetime:
    normalized = value.strip()
    if len(normalized) == 4:
        normalized = f"{session_date}:{normalized}"
    return datetime.strptime(normalized, "%Y%m%d:%H%M").replace(tzinfo=zone)
