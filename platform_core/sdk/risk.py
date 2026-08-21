from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Callable
from uuid import uuid4

from platform_core.schemas import MarketQuote
from platform_core.schemas.assets import AssetType

from .ledger import canonical_order_hash
from .models import AccountRiskSnapshot, LiveOrderIntent, RiskAuthorization


@dataclass(frozen=True, slots=True)
class LiveRiskPolicy:
    max_order_notional: Decimal
    max_symbol_notional: Decimal
    max_gross_notional: Decimal
    daily_loss_limit: Decimal
    max_daily_order_count: int = 100
    max_daily_traded_notional: Decimal = Decimal("250000")
    max_account_snapshot_age_seconds: int = 10
    max_quote_age_seconds: int = 5
    max_price_deviation_pct: Decimal = Decimal("0.03")
    max_option_spread_pct: Decimal = Decimal("0.10")
    authorization_ttl_seconds: int = 5
    live_market_data_types: frozenset[int] = frozenset({1})
    allow_outside_rth: bool = False
    allow_market_closed_orders: bool = False
    allow_opening_equity_shorts: bool = False
    allow_naked_short_options: bool = False
    require_halt_status_for_live: bool = True
    minimum_shortable_rating: Decimal = Decimal("2.5")

    def __post_init__(self) -> None:
        positive = {
            "max_order_notional": self.max_order_notional,
            "max_symbol_notional": self.max_symbol_notional,
            "max_gross_notional": self.max_gross_notional,
            "daily_loss_limit": self.daily_loss_limit,
        }
        invalid = [name for name, value in positive.items() if value <= 0]
        if (
            self.max_daily_order_count <= 0
            or self.max_daily_traded_notional <= 0
            or self.minimum_shortable_rating <= 0
        ):
            invalid.extend(["max_daily_order_count/max_daily_traded_notional"])
        if (
            self.max_account_snapshot_age_seconds <= 0
            or self.max_quote_age_seconds <= 0
            or self.authorization_ttl_seconds <= 0
        ):
            invalid.append("snapshot/quote/authorization time limits")
        if not Decimal("0") < self.max_price_deviation_pct <= Decimal("1"):
            invalid.append("max_price_deviation_pct")
        if not Decimal("0") < self.max_option_spread_pct <= Decimal("1"):
            invalid.append("max_option_spread_pct")
        if not self.live_market_data_types:
            invalid.append("live_market_data_types")
        if not (
            self.max_order_notional
            <= self.max_symbol_notional
            <= self.max_gross_notional
        ):
            invalid.append("max_order_notional <= max_symbol_notional <= max_gross_notional")
        if invalid:
            raise ValueError(f"risk policy values must be positive: {', '.join(invalid)}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "max_order_notional": str(self.max_order_notional),
            "max_symbol_notional": str(self.max_symbol_notional),
            "max_gross_notional": str(self.max_gross_notional),
            "daily_loss_limit": str(self.daily_loss_limit),
            "max_daily_order_count": self.max_daily_order_count,
            "max_daily_traded_notional": str(self.max_daily_traded_notional),
            "max_account_snapshot_age_seconds": self.max_account_snapshot_age_seconds,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "max_price_deviation_pct": str(self.max_price_deviation_pct),
            "max_option_spread_pct": str(self.max_option_spread_pct),
            "authorization_ttl_seconds": self.authorization_ttl_seconds,
            "live_market_data_types": sorted(self.live_market_data_types),
            "allow_outside_rth": self.allow_outside_rth,
            "allow_market_closed_orders": self.allow_market_closed_orders,
            "allow_opening_equity_shorts": self.allow_opening_equity_shorts,
            "allow_naked_short_options": self.allow_naked_short_options,
            "require_halt_status_for_live": self.require_halt_status_for_live,
            "minimum_shortable_rating": str(self.minimum_shortable_rating),
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LiveRiskPolicy":
        values = dict(payload)
        for key in (
            "max_order_notional",
            "max_symbol_notional",
            "max_gross_notional",
            "daily_loss_limit",
            "max_daily_traded_notional",
            "max_price_deviation_pct",
            "max_option_spread_pct",
            "minimum_shortable_rating",
        ):
            values[key] = Decimal(str(values[key]))
        values["live_market_data_types"] = frozenset(
            int(value) for value in values["live_market_data_types"]
        )
        return cls(**values)


class LiveRiskGateway:
    """关闭优先且结果确定的交易前授权边界。"""

    def __init__(
        self,
        policy: LiveRiskPolicy,
        *,
        policy_resolver: Callable[[str], LiveRiskPolicy | None] | None = None,
    ) -> None:
        self.policy = policy
        self._policy_resolver = policy_resolver

    def policy_for(self, account: str) -> LiveRiskPolicy:
        if self._policy_resolver is None:
            return self.policy
        return self._policy_resolver(account) or self.policy

    def has_explicit_policy(self, account: str) -> bool:
        return self.explicit_policy_for(account) is not None

    def explicit_policy_for(self, account: str) -> LiveRiskPolicy | None:
        if self._policy_resolver is None:
            return None
        return self._policy_resolver(account)

    def authorize(
        self,
        intent: LiveOrderIntent,
        *,
        account: AccountRiskSnapshot,
        quote: MarketQuote,
        require_live_market_data: bool,
        now: datetime | None = None,
        submission_order_count: int = 1,
        worst_case_fill_count: int = 1,
    ) -> RiskAuthorization:
        if submission_order_count <= 0 or worst_case_fill_count <= 0:
            raise ValueError("risk group counts must be positive")
        decided_at = _as_utc(now or datetime.now(UTC))
        policy = self.policy_for(account.account)
        expires_at = decided_at + timedelta(seconds=policy.authorization_ttl_seconds)
        request = intent.request
        multiplier = _contract_multiplier(intent)
        reference_price = _reference_price(intent, quote)
        notional = _computed_risk_notional(
            intent,
            reference_price=reference_price,
            multiplier=multiplier,
        )
        current_symbol = account.symbol_position_notional.get(request.instrument.symbol, Decimal("0"))
        signed_notional = notional if request.side == "BUY" else -notional
        projected_symbol = current_symbol + signed_notional
        if request.reduce_only:
            projected_gross = (
                max(Decimal("0"), account.gross_position_notional - notional)
                + account.open_order_notional
            )
        else:
            projected_gross = account.gross_position_notional + account.open_order_notional + notional
        reasons = {
            "reference_price": str(reference_price),
            "multiplier": str(multiplier),
            "projected_gross_notional": str(projected_gross),
            "account_snapshot_age_seconds": account.age_seconds(decided_at),
            "quote_age_seconds": _quote_age_seconds(quote, decided_at),
            "market_data_type": quote.market_data_type,
            "policy_fingerprint": policy.fingerprint,
            "submission_order_count": submission_order_count,
            "worst_case_fill_count": worst_case_fill_count,
        }

        rejection = self._first_rejection(
            intent,
            account=account,
            quote=quote,
            decided_at=decided_at,
            notional=notional,
            projected_symbol=projected_symbol,
            projected_gross=projected_gross,
            require_live_market_data=require_live_market_data,
            policy=policy,
            submission_order_count=submission_order_count,
            worst_case_fill_count=worst_case_fill_count,
        )
        if rejection is None:
            approved, code, detail = True, "ALLOW", "pre-trade risk checks passed"
        else:
            approved, code, detail = False, rejection[0], rejection[1]
        return RiskAuthorization(
            decision_id=str(uuid4()),
            approved=approved,
            code=code,
            detail=detail,
            account=account.account,
            client_order_id=intent.client_order_id,
            order_hash=canonical_order_hash(intent),
            decided_at=decided_at,
            expires_at=expires_at,
            computed_notional=notional,
            projected_symbol_notional=projected_symbol,
            reasons=reasons,
        )

    def validate_authorization(
        self,
        authorization: RiskAuthorization,
        intent: LiveOrderIntent,
        *,
        now: datetime | None = None,
    ) -> None:
        current = _as_utc(now or datetime.now(UTC))
        if not authorization.approved:
            raise PermissionError(f"risk authorization rejected: {authorization.code}")
        if current >= _as_utc(authorization.expires_at):
            raise PermissionError("risk authorization expired")
        if authorization.client_order_id != intent.client_order_id:
            raise PermissionError("risk authorization belongs to another client order")
        if authorization.account != intent.request.account:
            raise PermissionError("risk authorization belongs to another account")
        if authorization.order_hash != canonical_order_hash(intent):
            raise PermissionError("order changed after risk authorization")

    def authorize_contingent_exit(
        self,
        child: LiveOrderIntent,
        *,
        entry: LiveOrderIntent,
        entry_authorization: RiskAuthorization,
        quote: MarketQuote,
        now: datetime | None = None,
    ) -> RiskAuthorization:
        decided_at = _as_utc(now or datetime.now(UTC))
        request = child.request
        entry_request = entry.request
        approved = True
        code = "ALLOW:CONTINGENT_EXIT"
        detail = "contingent exit is bounded by its authorized bracket parent"
        if request.instrument != entry_request.instrument:
            approved, code, detail = False, "BLOCK:CONTRACT_MISMATCH", "child contract differs"
        elif request.side == entry_request.side:
            approved, code, detail = False, "BLOCK:EXIT_SIDE", "child does not exit parent"
        elif request.quantity != entry_request.quantity:
            approved, code, detail = False, "BLOCK:EXIT_SIZE", "child size differs from parent"
        entry_price = _reference_price(entry, quote)
        if approved and request.order_type == "LMT":
            assert request.limit_price is not None
            valid = (
                request.limit_price > entry_price
                if entry_request.side == "BUY"
                else request.limit_price < entry_price
            )
            if not valid:
                approved, code, detail = (
                    False,
                    "BLOCK:TAKE_PROFIT_DIRECTION",
                    "take-profit price is on the loss side of the entry",
                )
        if approved and request.order_type in {"STP", "STP_LMT"}:
            assert request.stop_price is not None
            valid = (
                request.stop_price < entry_price
                if entry_request.side == "BUY"
                else request.stop_price > entry_price
            )
            if not valid:
                approved, code, detail = (
                    False,
                    "BLOCK:STOP_DIRECTION",
                    "stop price is not protective for the entry",
                )
        child_price = request.limit_price or request.stop_price or entry_price
        child_notional = request.quantity * child_price * _contract_multiplier(child)
        return RiskAuthorization(
            decision_id=str(uuid4()),
            approved=approved and entry_authorization.approved,
            code=code if entry_authorization.approved else "BLOCK:PARENT_RISK",
            detail=detail if entry_authorization.approved else "parent entry failed risk checks",
            account=entry_authorization.account,
            client_order_id=child.client_order_id,
            order_hash=canonical_order_hash(child),
            decided_at=decided_at,
            expires_at=entry_authorization.expires_at,
            computed_notional=child_notional,
            projected_symbol_notional=entry_authorization.projected_symbol_notional,
            reasons={"parent_decision_id": entry_authorization.decision_id},
        )

    def _first_rejection(
        self,
        intent: LiveOrderIntent,
        *,
        account: AccountRiskSnapshot,
        quote: MarketQuote,
        decided_at: datetime,
        notional: Decimal,
        projected_symbol: Decimal,
        projected_gross: Decimal,
        require_live_market_data: bool,
        policy: LiveRiskPolicy,
        submission_order_count: int,
        worst_case_fill_count: int,
    ) -> tuple[str, str] | None:
        request = intent.request
        if account.account != request.account:
            return "BLOCK:ACCOUNT_MISMATCH", "risk snapshot belongs to another account"
        if intent.expires_at is not None and decided_at >= _as_utc(intent.expires_at):
            return "BLOCK:INTENT_EXPIRED", "order intent expired before authorization"
        if account.age_seconds(decided_at) > policy.max_account_snapshot_age_seconds:
            return "BLOCK:STALE_ACCOUNT", "account and margin snapshot is stale"
        if _quote_age_seconds(quote, decided_at) > policy.max_quote_age_seconds:
            return "BLOCK:STALE_QUOTE", "market quote is stale"
        if quote.instrument != request.instrument:
            return "BLOCK:QUOTE_CONTRACT_MISMATCH", "quote belongs to another contract"
        if require_live_market_data and quote.market_data_type not in policy.live_market_data_types:
            return "BLOCK:NON_LIVE_MARKET_DATA", "live orders require real-time market data"
        if require_live_market_data and policy.require_halt_status_for_live:
            if quote.halted_status is None or quote.halted_status < 0:
                return "BLOCK:HALT_STATUS_UNKNOWN", "live halt status is unavailable"
            if quote.halted_status > 0:
                return "BLOCK:TRADING_HALTED", "contract is currently halted"
        if request.outside_rth and not policy.allow_outside_rth:
            return "BLOCK:OUTSIDE_RTH", "outside-RTH orders are disabled by policy"
        if request.reduce_only:
            current_quantity = account.instrument_position_quantity.get(
                instrument_risk_key(request.instrument)
            )
            if current_quantity is None:
                return (
                    "BLOCK:POSITION_QUANTITY_UNKNOWN",
                    "reduce-only validation requires the current contract quantity",
                )
            wrong_direction = (
                request.side == "SELL" and current_quantity <= 0
            ) or (request.side == "BUY" and current_quantity >= 0)
            if wrong_direction:
                return "BLOCK:NOT_REDUCING", "reduce-only order would not reduce the position"
            if request.quantity > abs(current_quantity):
                return "BLOCK:REDUCE_ONLY_SIZE", "reduce-only order exceeds current exposure"
            return self._price_rejection(intent, quote, policy=policy)
        if account.daily_pnl <= -policy.daily_loss_limit:
            return "BLOCK:DAILY_LOSS", "account daily loss limit has been reached"
        if (
            account.daily_order_count + submission_order_count
            > policy.max_daily_order_count
        ):
            return (
                "BLOCK:DAILY_ORDER_COUNT",
                "order group would exceed the daily order-count limit",
            )
        if (
            account.daily_traded_notional + (notional * worst_case_fill_count)
            > policy.max_daily_traded_notional
        ):
            return "BLOCK:DAILY_TRADED_NOTIONAL", "daily traded-notional cap would be exceeded"
        if notional > policy.max_order_notional:
            return "BLOCK:ORDER_NOTIONAL", "order notional exceeds the per-order cap"
        if abs(projected_symbol) > policy.max_symbol_notional:
            return "BLOCK:SYMBOL_NOTIONAL", "projected symbol exposure exceeds its cap"
        if projected_gross > policy.max_gross_notional:
            return "BLOCK:GROSS_NOTIONAL", "projected gross exposure exceeds its cap"
        if request.side == "BUY" and not request.reduce_only and notional > account.available_funds:
            return "BLOCK:AVAILABLE_FUNDS", "order notional exceeds available funds"
        if (
            request.instrument.asset_type == AssetType.OPTION
            and request.side == "SELL"
            and not request.reduce_only
            and not policy.allow_naked_short_options
        ):
            return (
                "BLOCK:NAKED_OPTION",
                "single-leg option sells must be reduce-only or use a defined-risk BAG",
            )
        if (
            request.instrument.asset_type in {AssetType.EQUITY, AssetType.ETF}
            and request.side == "SELL"
            and not request.reduce_only
        ):
            current = account.instrument_position_notional.get(
                instrument_risk_key(request.instrument),
                account.symbol_position_notional.get(request.instrument.symbol, Decimal("0")),
            )
            if current <= 0 or notional > current:
                if not policy.allow_opening_equity_shorts:
                    return (
                        "BLOCK:EQUITY_SHORT",
                        "order could open or enlarge a short position without locate controls",
                    )
                if (
                    quote.shortable is None
                    or quote.shortable <= policy.minimum_shortable_rating
                ):
                    return (
                        "BLOCK:SHORT_NOT_LOCATABLE",
                        "IBKR shortable indicator does not confirm borrow availability",
                    )
        price_rejection = self._price_rejection(intent, quote, policy=policy)
        if price_rejection is not None:
            return price_rejection
        return None

    def _price_rejection(
        self,
        intent: LiveOrderIntent,
        quote: MarketQuote,
        *,
        policy: LiveRiskPolicy,
    ) -> tuple[str, str] | None:
        request = intent.request
        if quote.bid is None or quote.ask is None or quote.ask < quote.bid:
            return "BLOCK:INVALID_NBBO", "valid bid and ask are required"
        is_combo = request.instrument.asset_type == AssetType.COMBO
        if not is_combo and quote.bid <= 0:
            return "BLOCK:INVALID_NBBO", "valid bid and ask are required"
        mid = quote.mid or ((quote.bid + quote.ask) / Decimal("2"))
        price_scale = abs(mid)
        if price_scale <= 0:
            return "BLOCK:INVALID_NBBO", "quote midpoint magnitude must be positive"
        if request.instrument.asset_type in {AssetType.OPTION, AssetType.COMBO}:
            spread_pct = (quote.ask - quote.bid) / price_scale
            if spread_pct > policy.max_option_spread_pct:
                return "BLOCK:OPTION_SPREAD", "option spread exceeds policy"
        order_price = request.limit_price or request.stop_price
        if order_price is not None:
            deviation = abs(order_price - mid) / price_scale
            if deviation > policy.max_price_deviation_pct:
                return "BLOCK:PRICE_COLLAR", "order price is outside the market-data collar"
        return None


def _reference_price(intent: LiveOrderIntent, quote: MarketQuote) -> Decimal:
    request = intent.request
    if request.limit_price is not None:
        return request.limit_price
    if request.stop_price is not None:
        return request.stop_price
    if request.side == "BUY" and quote.ask is not None:
        return quote.ask
    if request.side == "SELL" and quote.bid is not None:
        return quote.bid
    if quote.mid is not None:
        return quote.mid
    if quote.last is not None:
        return quote.last
    raise ValueError("no usable price is available for notional calculation")


def _contract_multiplier(intent: LiveOrderIntent) -> Decimal:
    instrument = intent.request.instrument
    configured = instrument.metadata.get("multiplier")
    if configured is not None:
        multiplier = Decimal(str(configured))
        if multiplier <= 0:
            raise ValueError("contract multiplier must be positive")
        return multiplier
    return Decimal("100") if instrument.asset_type == AssetType.OPTION else Decimal("1")


def _computed_risk_notional(
    intent: LiveOrderIntent,
    *,
    reference_price: Decimal,
    multiplier: Decimal,
) -> Decimal:
    instrument = intent.request.instrument
    if instrument.asset_type == AssetType.COMBO:
        configured = instrument.metadata.get("max_loss_per_unit")
        if configured is None:
            raise ValueError("COMBO order requires a computed max_loss_per_unit")
        max_loss = Decimal(str(configured))
        if max_loss <= 0:
            raise ValueError("COMBO max_loss_per_unit must be positive")
        return intent.request.quantity * max_loss
    return intent.request.quantity * abs(reference_price) * multiplier


def _quote_age_seconds(quote: MarketQuote, now: datetime) -> float:
    return max(0.0, (now - _as_utc(quote.quote_ts)).total_seconds())


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def instrument_risk_key(instrument: object) -> str:
    conid = getattr(instrument, "conid", None)
    if conid:
        return f"conid:{conid}"
    return ":".join(
        str(value or "")
        for value in (
            getattr(getattr(instrument, "asset_type", None), "value", None),
            getattr(instrument, "symbol", None),
            getattr(instrument, "currency", None),
            getattr(instrument, "venue", None),
            getattr(instrument, "expiry", None),
            getattr(instrument, "option_right", None),
            getattr(instrument, "strike", None),
        )
    )
