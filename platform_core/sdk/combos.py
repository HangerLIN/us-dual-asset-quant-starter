from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from platform_core.schemas import AssetType, BrokerOrderRequest, InstrumentRef

from .models import (
    ComboLegRef,
    DefinedRiskOptionComboIntent,
    ExecutionResult,
    LiveOrderIntent,
)

if TYPE_CHECKING:
    from .execution import ExecutionSDK


class ComboRuleError(ValueError):
    pass


class VerticalRiskProfile(BaseModel):
    structure: str
    width: Decimal
    premium: Decimal
    multiplier: Decimal
    max_loss_per_combo: Decimal
    max_profit_per_combo: Decimal


class DefinedRiskComboSDK:
    """把限定风险期权垂直价差构建并提交为一个 IBKR BAG 订单。"""

    def __init__(self, execution: ExecutionSDK) -> None:
        self.execution = execution

    def prepare(
        self, intent: DefinedRiskOptionComboIntent
    ) -> tuple[LiveOrderIntent, VerticalRiskProfile]:
        profile = _vertical_risk_profile(intent.legs, intent.limit_price)
        long_leg, _ = _validate_vertical_legs(intent.legs)
        long_contract = long_leg.instrument
        leg_payloads = [self._leg_payload(leg) for leg in intent.legs]
        instrument = InstrumentRef(
            asset_type=AssetType.COMBO,
            symbol=long_contract.symbol,
            currency=intent.currency,
            venue=intent.venue,
            metadata={
                "combo_legs": leg_payloads,
                "combo_kind": "DEFINED_RISK_OPTION_VERTICAL",
                "guaranteed": True,
                "non_guaranteed": False,
                "multiplier": str(profile.multiplier),
                "max_loss_per_unit": str(profile.max_loss_per_combo),
                "max_profit_per_unit": str(profile.max_profit_per_combo),
                "risk_profile": profile.model_dump(mode="json"),
            },
        )
        request = BrokerOrderRequest(
            instrument=instrument,
            side="BUY",
            quantity=intent.quantity,
            order_type="LMT",
            limit_price=intent.limit_price,
            tif=intent.tif,
            account=intent.account,
            order_ref=intent.client_order_id,
            transmit=intent.transmit,
            what_if=False,
        )
        return (
            LiveOrderIntent(
                client_order_id=intent.client_order_id,
                strategy_code=intent.strategy_code,
                request=request,
                created_at=intent.created_at,
                expires_at=intent.expires_at,
                metadata={
                    **intent.metadata,
                    "order_family": "DEFINED_RISK_OPTION_COMBO",
                    "risk_profile": profile.model_dump(mode="json"),
                },
            ),
            profile,
        )

    def submit(self, intent: DefinedRiskOptionComboIntent) -> ExecutionResult:
        prepared, _ = self.prepare(intent)
        return self.execution.submit(prepared)

    @staticmethod
    def _validate_legs(legs: list[ComboLegRef]) -> tuple[ComboLegRef, ComboLegRef]:
        return _validate_vertical_legs(legs)

    @staticmethod
    def _multiplier(legs: list[ComboLegRef]) -> Decimal:
        return _combo_multiplier(legs)

    @staticmethod
    def _leg_payload(leg: ComboLegRef) -> dict[str, object]:
        return {
            "conid": leg.instrument.conid,
            "ratio": leg.ratio,
            "action": leg.action,
            "exchange": leg.exchange,
            "open_close": leg.open_close,
            "instrument": leg.instrument.model_dump(mode="json"),
        }


def validate_prepared_combo_intent(intent: LiveOrderIntent) -> VerticalRiskProfile:
    """重新计算已准备 BAG 的限定风险，不信任客户端元数据。"""

    request = intent.request
    instrument = request.instrument
    if instrument.asset_type != AssetType.COMBO:
        raise ComboRuleError("prepared combo validation requires a COMBO instrument")
    if request.side != "BUY" or request.order_type != "LMT":
        raise ComboRuleError("defined-risk BAG orders must be BUY LMT orders")
    if request.limit_price is None:
        raise ComboRuleError("defined-risk BAG orders require a signed limit price")
    metadata = instrument.metadata
    if (
        metadata.get("combo_kind") != "DEFINED_RISK_OPTION_VERTICAL"
        or metadata.get("guaranteed") is not True
        or metadata.get("non_guaranteed") is not False
    ):
        raise ComboRuleError("only guaranteed defined-risk option verticals are supported")
    raw_legs = metadata.get("combo_legs")
    if not isinstance(raw_legs, list):
        raise ComboRuleError("defined-risk BAG metadata is missing combo legs")
    legs = [_combo_leg_from_payload(payload) for payload in raw_legs]
    profile = _vertical_risk_profile(legs, request.limit_price)
    expected = {
        "multiplier": profile.multiplier,
        "max_loss_per_unit": profile.max_loss_per_combo,
        "max_profit_per_unit": profile.max_profit_per_combo,
    }
    for field, value in expected.items():
        configured = metadata.get(field)
        if configured is None or Decimal(str(configured)) != value:
            raise ComboRuleError(
                f"prepared combo {field} does not match the recomputed vertical risk"
            )
    risk_profile = metadata.get("risk_profile")
    if not isinstance(risk_profile, dict):
        raise ComboRuleError("prepared combo is missing its canonical risk profile")
    try:
        stored_profile = VerticalRiskProfile.model_validate(risk_profile)
    except Exception as exc:  # noqa: BLE001 - 在 SDK 边界统一规范化校验错误。
        raise ComboRuleError("prepared combo risk profile is invalid") from exc
    if stored_profile != profile:
        raise ComboRuleError("prepared combo risk profile was modified after construction")
    return profile


def _combo_leg_from_payload(payload: Any) -> ComboLegRef:
    if not isinstance(payload, dict) or not isinstance(payload.get("instrument"), dict):
        raise ComboRuleError("each defined-risk BAG leg requires full contract metadata")
    instrument = InstrumentRef.model_validate(payload["instrument"])
    if instrument.conid != int(payload.get("conid") or 0):
        raise ComboRuleError("combo leg conid differs from its embedded contract")
    try:
        return ComboLegRef(
            instrument=instrument,
            ratio=payload.get("ratio"),
            action=str(payload.get("action", "")).upper(),
            exchange=payload.get("exchange") or "SMART",
            open_close=payload.get("open_close", 0),
        )
    except Exception as exc:  # noqa: BLE001 - 对外仅暴露稳定的 SDK 错误类型。
        raise ComboRuleError("invalid defined-risk BAG leg") from exc


def _vertical_risk_profile(
    legs: list[ComboLegRef], limit_price: Decimal
) -> VerticalRiskProfile:
    long_leg, short_leg = _validate_vertical_legs(legs)
    long_contract = long_leg.instrument
    short_contract = short_leg.instrument
    assert long_contract.strike is not None
    assert short_contract.strike is not None
    assert long_contract.option_right is not None
    width = abs(long_contract.strike - short_contract.strike)
    if width <= 0:
        raise ComboRuleError("vertical spread strikes must differ")
    debit = (
        long_contract.strike < short_contract.strike
        if long_contract.option_right == "CALL"
        else long_contract.strike > short_contract.strike
    )
    if debit and limit_price <= 0:
        raise ComboRuleError("debit vertical requires a positive BAG limit price")
    if not debit and limit_price >= 0:
        raise ComboRuleError("credit vertical requires a negative BAG limit price")
    premium = abs(limit_price)
    if premium >= width:
        raise ComboRuleError("vertical premium must be smaller than strike width")
    multiplier = _combo_multiplier(legs)
    max_loss_points = premium if debit else width - premium
    max_profit_points = width - premium if debit else premium
    return VerticalRiskProfile(
        structure=(
            f"{long_contract.option_right}_{'DEBIT' if debit else 'CREDIT'}_VERTICAL"
        ),
        width=width,
        premium=premium,
        multiplier=multiplier,
        max_loss_per_combo=max_loss_points * multiplier,
        max_profit_per_combo=max_profit_points * multiplier,
    )


def _validate_vertical_legs(
    legs: list[ComboLegRef],
) -> tuple[ComboLegRef, ComboLegRef]:
    if len(legs) != 2:
        raise ComboRuleError("defined-risk vertical requires exactly two legs")
    if any(leg.ratio != 1 for leg in legs):
        raise ComboRuleError("defined-risk vertical currently requires 1:1 legs")
    if {leg.action for leg in legs} != {"BUY", "SELL"}:
        raise ComboRuleError("defined-risk vertical requires one BUY and one SELL leg")
    instruments = [leg.instrument for leg in legs]
    if any(instrument.asset_type != AssetType.OPTION for instrument in instruments):
        raise ComboRuleError("defined-risk vertical legs must both be options")
    first, second = instruments
    first_identity = (
        first.symbol,
        first.currency,
        first.expiry,
        first.option_right,
    )
    second_identity = (
        second.symbol,
        second.currency,
        second.expiry,
        second.option_right,
    )
    if first_identity != second_identity:
        raise ComboRuleError(
            "vertical legs must share underlying, currency, expiry, and option right"
        )
    long_leg = next(leg for leg in legs if leg.action == "BUY")
    short_leg = next(leg for leg in legs if leg.action == "SELL")
    return long_leg, short_leg


def _combo_multiplier(legs: list[ComboLegRef]) -> Decimal:
    multipliers = {
        Decimal(str(leg.instrument.metadata.get("multiplier", "100")))
        for leg in legs
    }
    if len(multipliers) != 1:
        raise ComboRuleError("combo leg multipliers must match")
    multiplier = multipliers.pop()
    if multiplier <= 0:
        raise ComboRuleError("combo multiplier must be positive")
    return multiplier
