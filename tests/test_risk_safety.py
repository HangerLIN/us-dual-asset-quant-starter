from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from platform_core.schemas import (
    AssetType,
    BrokerExecution,
    BrokerOrderRequest,
    InstrumentRef,
    MarketQuote,
)
from platform_core.sdk import (
    AccountRiskSnapshot,
    BrokerEvent,
    BrokerEventType,
    BrokerSessionState,
    ComboLegRef,
    ComboRuleError,
    ContractRuleError,
    ContractRulesSDK,
    DefinedRiskComboSDK,
    DefinedRiskOptionComboIntent,
    ExecutionSDK,
    LiveOrderIntent,
    LiveRiskGateway,
    LiveRiskPolicy,
    OrderEfficiencyError,
    OrderLifecycleState,
    OrderPacingSDK,
    QualifiedContract,
    RiskLimitControlSDK,
    TradingMode,
    TradingSafetyConfig,
    TradingSafetyController,
    TradingSafetyError,
)
from tests.support.execution import (
    FakeBroker,
    _execution_sdk,
    _instrument,
    _intent,
    _ledger,
    _policy,
)


def test_live_safety_requires_static_enable_allowlist_and_runtime_arm() -> None:
    disabled = TradingSafetyController(
        TradingSafetyConfig(
            mode=TradingMode.LIVE,
            allowed_accounts=frozenset({"U123456"}),
            live_enabled=False,
        )
    )
    with pytest.raises(TradingSafetyError, match="statically enabled"):
        disabled.arm_live(account="U123456", confirmation="ARM-LIVE:U123456")

    enabled = TradingSafetyController(
        TradingSafetyConfig(
            mode=TradingMode.LIVE,
            allowed_accounts=frozenset({"U123456"}),
            live_enabled=True,
        )
    )
    with pytest.raises(TradingSafetyError, match="not armed"):
        enabled.assert_can_transmit(account="U123456")
    enabled.arm_live(account="U123456", confirmation="ARM-LIVE:U123456")
    enabled.assert_can_transmit(account="U123456")


def test_live_readiness_requires_an_active_governed_risk_policy() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    broker.config = SimpleNamespace(client_id=11, account="U123456", market_data_type=1)
    safety = TradingSafetyController(
        TradingSafetyConfig(
            mode=TradingMode.LIVE,
            allowed_accounts=frozenset({"U123456"}),
            live_enabled=True,
        )
    )
    sdk = ExecutionSDK(
        broker=broker,
        ledger=ledger,
        risk=LiveRiskGateway(_policy()),
        safety=safety,
    )
    sdk.start(account="U123456")

    assert sdk.readiness()["risk_policy_ready"] is False
    with pytest.raises(PermissionError, match="approved and active"):
        sdk._require_governed_risk_policy("U123456")

    sdk.risk = LiveRiskGateway(_policy(), policy_resolver=lambda _: _policy())
    assert sdk.readiness()["risk_policy_ready"] is True


def test_live_risk_rejects_delayed_data_and_daily_loss() -> None:
    now = datetime.now(UTC)
    intent = _intent()
    account = AccountRiskSnapshot(
        account="DU123456",
        captured_at=now,
        net_liquidation=Decimal(100000),
        available_funds=Decimal(50000),
        buying_power=Decimal(100000),
        daily_pnl=Decimal(-1000),
    )
    quote = MarketQuote(
        instrument=_instrument(),
        quote_ts=now,
        bid=Decimal(99),
        ask=Decimal(101),
        market_data_type=4,
    )

    decision = LiveRiskGateway(_policy()).authorize(
        intent,
        account=account,
        quote=quote,
        require_live_market_data=True,
        now=now,
    )

    assert decision.approved is False
    assert decision.code == "BLOCK:NON_LIVE_MARKET_DATA"


def test_reduce_only_remains_available_after_expansion_limits_are_hit() -> None:
    intent = _intent("reduce-only-close").model_copy(
        update={
            "request": _intent().request.model_copy(
                update={
                    "side": "SELL",
                    "reduce_only": True,
                    "order_ref": "reduce-only-close",
                }
            )
        }
    )
    account = (
        FakeBroker()
        .account_risk_snapshot(account="DU123456")
        .model_copy(
            update={
                "daily_pnl": Decimal(-5000),
                "daily_order_count": 100,
                "daily_traded_notional": Decimal(250000),
                "gross_position_notional": Decimal(1000000),
                "instrument_position_notional": {"conid:756733": Decimal(100)},
                "instrument_position_quantity": {"conid:756733": Decimal(1)},
            }
        )
    )

    decision = LiveRiskGateway(_policy()).authorize(
        intent,
        account=account,
        quote=FakeBroker().snapshot_quote(_instrument()),
        require_live_market_data=True,
    )

    assert decision.approved


def test_contract_rules_enforce_tick_and_market_session() -> None:
    class ContractBroker:
        def qualify_contract(self, instrument):
            return QualifiedContract(
                instrument=instrument.model_copy(update={"conid": 756733}),
                supported_order_types=["LMT", "STP LMT"],
                min_tick=Decimal("0.01"),
                min_size=Decimal(1),
                size_increment=Decimal(1),
                time_zone_id="US/Eastern",
                liquid_hours="20260821:0930-20260821:1600",
            )

        def market_rule(self, rule_id):
            return []

    rules = ContractRulesSDK(ContractBroker())
    request = _intent().request
    valid = rules.qualify_and_validate(
        request,
        require_complete=True,
        require_open_session=True,
        now=datetime(2026, 8, 21, 14, 0, tzinfo=UTC),
    )
    assert valid.instrument.conid == 756733

    with pytest.raises(ContractRuleError, match="tick increment"):
        rules.qualify_and_validate(
            request.model_copy(update={"limit_price": Decimal("100.005")}),
            require_complete=True,
        )
    with pytest.raises(ContractRuleError, match="outside"):
        rules.qualify_and_validate(
            request,
            require_complete=True,
            require_open_session=True,
            now=datetime(2026, 8, 21, 2, 0, tzinfo=UTC),
        )


def test_event_persistence_failure_activates_fail_closed_kill() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)

    def fail(_event):
        raise OSError("database unavailable")

    ledger.record_event = fail
    sdk.on_broker_event(
        BrokerEvent(
            event_type=BrokerEventType.CONNECTION,
            account="DU123456",
            payload={"state": "LOST", "code": 1100},
        )
    )

    assert "event persistence failed" in sdk.safety.killed_reason
    assert broker.session_state == BrokerSessionState.DEGRADED


def test_unmanaged_open_order_event_activates_a_persistent_kill() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)

    sdk.on_broker_event(
        BrokerEvent(
            event_type=BrokerEventType.OPEN_ORDER,
            account="DU123456",
            broker_order_id=777,
            client_order_id="external-order-ref",
            payload={"client_id": 22},
        )
    )

    assert sdk.safety.killed_reason == "unmanaged open broker order 777"
    assert ledger.kill_switch_reason("account:DU123456") == sdk.safety.killed_reason
    assert broker.session_state == BrokerSessionState.DEGRADED


def test_unmanaged_execution_event_activates_a_persistent_kill() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    execution = BrokerExecution(
        execution_id="external-execution-01",
        order_id=888,
        client_id=22,
        account="DU123456",
        instrument=_instrument(),
        side="BUY",
        quantity=Decimal(1),
        price=Decimal(100),
        executed_at=datetime.now(UTC),
        order_ref="external-order-ref",
    )

    sdk.on_broker_event(
        BrokerEvent(
            event_type=BrokerEventType.EXECUTION,
            account="DU123456",
            broker_order_id=888,
            execution_id=execution.execution_id,
            payload=execution.model_dump(mode="json"),
        )
    )

    assert "unmanaged broker execution" in sdk.safety.killed_reason
    assert ledger.kill_switch_reason("account:DU123456") == sdk.safety.killed_reason


def test_order_pacing_blocks_new_orders_on_bad_efficiency_but_not_cancels() -> None:
    pacing = OrderPacingSDK(
        max_messages_per_second=5,
        max_order_efficiency_ratio=1,
        minimum_messages_for_oer=2,
    )
    pacing.acquire(messages=2)

    with pytest.raises(OrderEfficiencyError):
        pacing.check_new_orders_allowed()

    # 撤单直接占用消息配额；即使订单效率限制阻止开仓，也必须保留撤单能力。
    pacing.record_execution()
    pacing.record_execution()
    pacing.check_new_orders_allowed()
    assert pacing.order_efficiency_ratio == 1


def test_order_efficiency_guard_does_not_block_reduce_only_exit() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    sdk.pacing = OrderPacingSDK(
        max_messages_per_second=5,
        max_order_efficiency_ratio=1,
        minimum_messages_for_oer=2,
    )
    sdk.pacing.acquire(messages=2)
    original_snapshot = broker.account_risk_snapshot

    def positioned_snapshot(*, account=None):
        return original_snapshot(account=account).model_copy(
            update={
                "instrument_position_notional": {"conid:756733": Decimal(100)},
                "instrument_position_quantity": {"conid:756733": Decimal(1)},
            }
        )

    broker.account_risk_snapshot = positioned_snapshot
    close_intent = _intent("reduce-only-oer").model_copy(
        update={
            "request": _intent().request.model_copy(
                update={
                    "side": "SELL",
                    "reduce_only": True,
                    "order_ref": "reduce-only-oer",
                }
            )
        }
    )

    result = sdk.submit(close_intent)

    assert result.state == OrderLifecycleState.ACKNOWLEDGED


def test_defined_risk_combo_sdk_computes_vertical_max_loss() -> None:
    expiry = datetime(2026, 9, 18, tzinfo=UTC).date()
    long_call = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=1001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=expiry,
    )
    short_call = long_call.model_copy(update={"conid": 1002, "strike": Decimal(510)})
    combo = DefinedRiskOptionComboIntent(
        client_order_id="combo-debit-001",
        strategy_code="test-strategy",
        legs=[
            ComboLegRef(instrument=long_call, action="BUY"),
            ComboLegRef(instrument=short_call, action="SELL"),
        ],
        quantity=Decimal(1),
        limit_price=Decimal("2.00"),
        account="DU123456",
    )

    prepared, profile = DefinedRiskComboSDK(SimpleNamespace()).prepare(combo)

    assert prepared.request.instrument.asset_type == AssetType.COMBO
    assert prepared.request.side == "BUY"
    assert profile.max_loss_per_combo == Decimal(200)
    assert profile.max_profit_per_combo == Decimal(800)
    quote = MarketQuote(
        instrument=prepared.request.instrument,
        quote_ts=datetime.now(UTC),
        bid=Decimal("1.90"),
        ask=Decimal("2.10"),
        market_data_type=1,
        halted_status=0,
    )
    decision = LiveRiskGateway(_policy()).authorize(
        prepared,
        account=FakeBroker().account_risk_snapshot(account="DU123456"),
        quote=quote,
        require_live_market_data=True,
    )
    assert decision.approved
    assert decision.computed_notional == Decimal(200)
    assert decision.reasons["policy_fingerprint"] == _policy().fingerprint


def test_execution_recomputes_combo_risk_and_rejects_tampered_metadata() -> None:
    expiry = datetime(2026, 9, 18, tzinfo=UTC).date()
    long_call = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=1001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=expiry,
    )
    short_call = long_call.model_copy(update={"conid": 1002, "strike": Decimal(510)})
    prepared, _ = DefinedRiskComboSDK(SimpleNamespace()).prepare(
        DefinedRiskOptionComboIntent(
            client_order_id="combo-tamper-001",
            strategy_code="test-strategy",
            legs=[
                ComboLegRef(instrument=long_call, action="BUY"),
                ComboLegRef(instrument=short_call, action="SELL"),
            ],
            quantity=Decimal(1),
            limit_price=Decimal("2.00"),
            account="DU123456",
        )
    )
    metadata = dict(prepared.request.instrument.metadata)
    metadata["max_loss_per_unit"] = "1"
    tampered = prepared.model_copy(
        update={
            "request": prepared.request.model_copy(
                update={
                    "instrument": prepared.request.instrument.model_copy(
                        update={"metadata": metadata}
                    )
                }
            )
        }
    )
    ledger, _ = _ledger()
    sdk = _execution_sdk(FakeBroker(), ledger)

    with pytest.raises(ComboRuleError, match="recomputed"):
        sdk.submit(tampered)


def test_defined_risk_combo_sdk_requires_signed_credit_price() -> None:
    expiry = datetime(2026, 9, 18, tzinfo=UTC).date()
    short_call = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=1001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=expiry,
    )
    long_call = short_call.model_copy(update={"conid": 1002, "strike": Decimal(510)})
    invalid = DefinedRiskOptionComboIntent(
        client_order_id="combo-credit-01",
        strategy_code="test-strategy",
        legs=[
            ComboLegRef(instrument=short_call, action="SELL"),
            ComboLegRef(instrument=long_call, action="BUY"),
        ],
        quantity=Decimal(1),
        limit_price=Decimal("2.00"),
        account="DU123456",
    )

    with pytest.raises(ComboRuleError, match="negative"):
        DefinedRiskComboSDK(SimpleNamespace()).prepare(invalid)


def test_group_authorization_reserves_all_daily_order_slots() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    account = broker.account_risk_snapshot(account="DU123456").model_copy(
        update={"daily_order_count": 98}
    )
    decision = sdk.risk.authorize(
        _intent("bracket-capacity"),
        account=account,
        quote=broker.snapshot_quote(_instrument()),
        require_live_market_data=False,
        submission_order_count=3,
        worst_case_fill_count=2,
    )

    assert decision.code == "BLOCK:DAILY_ORDER_COUNT"


def test_risk_policy_control_requires_independent_approval_and_supports_rollback() -> None:
    _, factory = _ledger()
    controls = RiskLimitControlSDK(factory)
    first = controls.propose(scope="global", policy=_policy(), actor="proposer")
    with pytest.raises(PermissionError, match="cannot approve"):
        controls.approve(policy_id=first.policy_id, actor="proposer")
    controls.approve(policy_id=first.policy_id, actor="approver")
    active = controls.activate(
        policy_id=first.policy_id,
        actor="operator",
        confirmation="ACTIVATE-RISK-POLICY:global:1",
    )
    assert active.status == "ACTIVE"
    assert controls.resolve("DU123456") == _policy()

    tighter = LiveRiskPolicy.from_payload(_policy().to_payload() | {"max_order_notional": "5000"})
    second = controls.propose(scope="global", policy=tighter, actor="proposer")
    controls.approve(policy_id=second.policy_id, actor="approver")
    controls.activate(
        policy_id=second.policy_id,
        actor="operator",
        confirmation="ACTIVATE-RISK-POLICY:global:2",
    )
    rolled_back = controls.rollback(
        scope="global",
        version=1,
        actor="operator",
        confirmation="ROLLBACK-RISK-POLICY:global:1",
    )
    assert rolled_back.status == "ACTIVE"
    assert controls.resolve("DU123456").max_order_notional == Decimal(10000)


def test_risk_blocks_naked_option_sell_and_daily_capacity() -> None:
    now = datetime.now(UTC)
    option = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=2001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=datetime(2026, 9, 18, tzinfo=UTC).date(),
    )
    intent = LiveOrderIntent(
        client_order_id="naked-option-01",
        strategy_code="test-strategy",
        request=BrokerOrderRequest(
            instrument=option,
            side="SELL",
            quantity=Decimal(1),
            order_type="LMT",
            limit_price=Decimal(2),
            account="DU123456",
        ),
    )
    account = FakeBroker().account_risk_snapshot(account="DU123456")
    quote = MarketQuote(
        instrument=option,
        quote_ts=now,
        bid=Decimal("1.95"),
        ask=Decimal("2.05"),
        market_data_type=1,
        halted_status=0,
    )
    decision = LiveRiskGateway(_policy()).authorize(
        intent,
        account=account,
        quote=quote,
        require_live_market_data=True,
        now=now,
    )
    assert decision.code == "BLOCK:NAKED_OPTION"

    capacity = account.model_copy(update={"daily_order_count": 100})
    equity_decision = LiveRiskGateway(_policy()).authorize(
        _intent(),
        account=capacity,
        quote=FakeBroker().snapshot_quote(_instrument()),
        require_live_market_data=True,
    )
    assert equity_decision.code == "BLOCK:DAILY_ORDER_COUNT"


@pytest.mark.parametrize(
    ("mode", "account", "live_enabled", "arm", "what_if", "allowed", "message"),
    [
        (TradingMode.READ_ONLY, "DU123456", False, False, False, False, "READ_ONLY"),
        (TradingMode.PAPER, "DU123456", False, False, False, True, None),
        (TradingMode.PAPER, "U123456", False, False, False, True, "DU-prefixed"),
        (TradingMode.LIVE, "DU123456", True, True, False, True, "paper accounts"),
        (TradingMode.LIVE, "U123456", False, False, False, True, "disabled"),
        (TradingMode.LIVE, "U123456", True, False, False, True, "not armed"),
        (TradingMode.LIVE, "U123456", True, True, False, True, None),
        (TradingMode.LIVE, "U123456", True, False, True, True, None),
        (TradingMode.LIVE, "U123456", True, True, False, False, "allowlist"),
    ],
)
def test_trading_mode_account_arm_and_what_if_matrix(
    mode: TradingMode,
    account: str,
    live_enabled: bool,
    arm: bool,
    what_if: bool,
    allowed: bool,
    message: str | None,
) -> None:
    allowlist = frozenset({account}) if allowed else frozenset()
    safety = TradingSafetyController(
        TradingSafetyConfig(
            mode=mode,
            allowed_accounts=allowlist,
            live_enabled=live_enabled,
        )
    )
    if arm and mode == TradingMode.LIVE and live_enabled and allowed:
        safety.arm_live(account=account, confirmation=f"ARM-LIVE:{account}")

    if message is None:
        safety.assert_can_transmit(account=account, what_if=what_if)
    else:
        with pytest.raises(TradingSafetyError, match=message):
            safety.assert_can_transmit(account=account, what_if=what_if)


def test_arm_expires_disarms_on_connection_loss_and_requires_rearm() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    safety = TradingSafetyController(
        TradingSafetyConfig(
            mode=TradingMode.LIVE,
            allowed_accounts=frozenset({"U123456"}),
            live_enabled=True,
        )
    )
    sdk = ExecutionSDK(
        broker=broker,
        ledger=ledger,
        risk=LiveRiskGateway(_policy()),
        safety=safety,
        require_active_risk_policy_for_live=False,
    )
    sdk.start(account="U123456")
    safety.arm_live(account="U123456", confirmation="ARM-LIVE:U123456")
    safety.assert_can_transmit(account="U123456")

    safety._armed_until = datetime.now(UTC) - timedelta(microseconds=1)
    with pytest.raises(TradingSafetyError, match="expired"):
        safety.assert_can_transmit(account="U123456")

    safety.arm_live(account="U123456", confirmation="ARM-LIVE:U123456")
    sdk.on_broker_event(
        BrokerEvent(event_type=BrokerEventType.CONNECTION, payload={"state": "LOST"})
    )
    with pytest.raises(TradingSafetyError, match="not armed"):
        safety.assert_can_transmit(account="U123456")


def _authorize_equity_boundary(
    *,
    policy: LiveRiskPolicy,
    account_updates: dict | None = None,
    intent_updates: dict | None = None,
    quote_updates: dict | None = None,
    now: datetime | None = None,
):
    decided_at = now or datetime.now(UTC)
    intent = _intent("risk-boundary-001")
    if intent_updates:
        intent = intent.model_copy(
            update={"request": intent.request.model_copy(update=intent_updates)}
        )
    account = FakeBroker().account_risk_snapshot(account="DU123456")
    if account_updates:
        account = account.model_copy(update=account_updates)
    quote = MarketQuote(
        instrument=intent.request.instrument,
        quote_ts=decided_at,
        bid=Decimal(99),
        ask=Decimal(101),
        market_data_type=1,
        halted_status=0,
    )
    if quote_updates:
        quote = quote.model_copy(update=quote_updates)
    return LiveRiskGateway(policy).authorize(
        intent,
        account=account,
        quote=quote,
        require_live_market_data=True,
        now=decided_at,
    )


@pytest.mark.parametrize(
    ("policy", "allowed_updates", "blocked_updates", "blocked_code"),
    [
        (
            LiveRiskPolicy(
                max_order_notional=Decimal(100),
                max_symbol_notional=Decimal(1000),
                max_gross_notional=Decimal(2000),
                daily_loss_limit=Decimal(1000),
                max_price_deviation_pct=Decimal("0.20"),
            ),
            {},
            {"intent": {"limit_price": Decimal("100.01")}},
            "BLOCK:ORDER_NOTIONAL",
        ),
        (
            _policy(),
            {"account": {"available_funds": Decimal(100)}},
            {"account": {"available_funds": Decimal("99.99")}},
            "BLOCK:AVAILABLE_FUNDS",
        ),
        (
            LiveRiskPolicy(
                max_order_notional=Decimal(100),
                max_symbol_notional=Decimal(200),
                max_gross_notional=Decimal(1000),
                daily_loss_limit=Decimal(1000),
                max_price_deviation_pct=Decimal("0.20"),
            ),
            {
                "account": {
                    "gross_position_notional": Decimal(0),
                    "symbol_position_notional": {"SPY": Decimal(100)},
                }
            },
            {
                "account": {
                    "gross_position_notional": Decimal(0),
                    "symbol_position_notional": {"SPY": Decimal("100.01")},
                }
            },
            "BLOCK:SYMBOL_NOTIONAL",
        ),
        (
            LiveRiskPolicy(
                max_order_notional=Decimal(100),
                max_symbol_notional=Decimal(500),
                max_gross_notional=Decimal(500),
                daily_loss_limit=Decimal(1000),
                max_price_deviation_pct=Decimal("0.20"),
            ),
            {"account": {"gross_position_notional": Decimal(400)}},
            {"account": {"gross_position_notional": Decimal("400.01")}},
            "BLOCK:GROSS_NOTIONAL",
        ),
        (
            LiveRiskPolicy.from_payload(_policy().to_payload() | {"max_daily_order_count": 10}),
            {"account": {"daily_order_count": 9}},
            {"account": {"daily_order_count": 10}},
            "BLOCK:DAILY_ORDER_COUNT",
        ),
        (
            LiveRiskPolicy.from_payload(
                _policy().to_payload() | {"max_daily_traded_notional": "1000"}
            ),
            {"account": {"daily_traded_notional": Decimal(900)}},
            {"account": {"daily_traded_notional": Decimal("900.01")}},
            "BLOCK:DAILY_TRADED_NOTIONAL",
        ),
    ],
)
def test_risk_caps_allow_the_limit_and_block_the_first_excess(
    policy: LiveRiskPolicy,
    allowed_updates: dict,
    blocked_updates: dict,
    blocked_code: str,
) -> None:
    allowed = _authorize_equity_boundary(
        policy=policy,
        account_updates=allowed_updates.get("account"),
        intent_updates=allowed_updates.get("intent"),
    )
    blocked = _authorize_equity_boundary(
        policy=policy,
        account_updates=blocked_updates.get("account"),
        intent_updates=blocked_updates.get("intent"),
    )

    assert allowed.approved
    assert blocked.code == blocked_code


def test_daily_loss_blocks_at_the_limit_without_weakening_the_stop() -> None:
    below_limit = _authorize_equity_boundary(
        policy=_policy(),
        account_updates={"daily_pnl": Decimal("-999.99")},
    )
    at_limit = _authorize_equity_boundary(
        policy=_policy(),
        account_updates={"daily_pnl": Decimal(-1000)},
    )

    assert below_limit.approved
    assert at_limit.code == "BLOCK:DAILY_LOSS"


@pytest.mark.parametrize(
    ("kind", "extra_age", "blocked_code"),
    [
        ("account", timedelta(microseconds=1), "BLOCK:STALE_ACCOUNT"),
        ("quote", timedelta(microseconds=1), "BLOCK:STALE_QUOTE"),
    ],
)
def test_snapshot_and_quote_age_allow_exact_limit_then_fail_closed(
    kind: str,
    extra_age: timedelta,
    blocked_code: str,
) -> None:
    now = datetime.now(UTC)
    policy = _policy()
    account_limit = now - timedelta(seconds=policy.max_account_snapshot_age_seconds)
    quote_limit = now - timedelta(seconds=policy.max_quote_age_seconds)
    allowed = _authorize_equity_boundary(
        policy=policy,
        account_updates={"captured_at": account_limit},
        quote_updates={"quote_ts": quote_limit},
        now=now,
    )
    blocked = _authorize_equity_boundary(
        policy=policy,
        account_updates={
            "captured_at": account_limit - extra_age if kind == "account" else account_limit
        },
        quote_updates={"quote_ts": quote_limit - extra_age if kind == "quote" else quote_limit},
        now=now,
    )

    assert allowed.approved
    assert blocked.code == blocked_code


def test_price_collar_and_option_spread_are_inclusive_at_the_limit() -> None:
    price_at_limit = _authorize_equity_boundary(
        policy=_policy(),
        intent_updates={"limit_price": Decimal(120)},
        quote_updates={"bid": Decimal(100), "ask": Decimal(100), "mid": Decimal(100)},
    )
    price_over = _authorize_equity_boundary(
        policy=_policy(),
        intent_updates={"limit_price": Decimal("120.01")},
        quote_updates={"bid": Decimal(100), "ask": Decimal(100), "mid": Decimal(100)},
    )
    assert price_at_limit.approved
    assert price_over.code == "BLOCK:PRICE_COLLAR"

    now = datetime.now(UTC)
    option = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=2002,
        option_right="CALL",
        strike=Decimal(500),
        expiry=datetime(2026, 9, 18, tzinfo=UTC).date(),
    )
    intent = LiveOrderIntent(
        client_order_id="option-spread-001",
        strategy_code="test-strategy",
        request=BrokerOrderRequest(
            instrument=option,
            side="BUY",
            quantity=Decimal(1),
            order_type="LMT",
            limit_price=Decimal(2),
            account="DU123456",
        ),
    )
    account = FakeBroker().account_risk_snapshot(account="DU123456")

    def authorize_spread(ask: Decimal):
        return LiveRiskGateway(_policy()).authorize(
            intent,
            account=account,
            quote=MarketQuote(
                instrument=option,
                quote_ts=now,
                bid=Decimal("1.90"),
                ask=ask,
                market_data_type=1,
                halted_status=0,
            ),
            require_live_market_data=True,
            now=now,
        )

    assert authorize_spread(Decimal("2.10")).approved
    assert authorize_spread(Decimal("2.1001")).code == "BLOCK:OPTION_SPREAD"


@pytest.mark.parametrize(
    ("halted_status", "blocked_code"),
    [
        (None, "BLOCK:HALT_STATUS_UNKNOWN"),
        (-1, "BLOCK:HALT_STATUS_UNKNOWN"),
        (1, "BLOCK:TRADING_HALTED"),
    ],
)
def test_live_halt_state_fails_closed(
    halted_status: int | None,
    blocked_code: str,
) -> None:
    decision = _authorize_equity_boundary(
        policy=_policy(),
        quote_updates={"halted_status": halted_status},
    )
    assert decision.code == blocked_code


def test_defined_risk_combo_max_loss_obeys_order_notional_boundary() -> None:
    expiry = datetime(2026, 9, 18, tzinfo=UTC).date()
    long_call = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=3001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=expiry,
    )
    short_call = long_call.model_copy(update={"conid": 3002, "strike": Decimal(510)})
    prepared, _ = DefinedRiskComboSDK(SimpleNamespace()).prepare(
        DefinedRiskOptionComboIntent(
            client_order_id="combo-boundary-01",
            strategy_code="test-strategy",
            legs=[
                ComboLegRef(instrument=long_call, action="BUY"),
                ComboLegRef(instrument=short_call, action="SELL"),
            ],
            quantity=Decimal(1),
            limit_price=Decimal(2),
            account="DU123456",
        )
    )
    quote = MarketQuote(
        instrument=prepared.request.instrument,
        quote_ts=datetime.now(UTC),
        bid=Decimal("1.90"),
        ask=Decimal("2.10"),
        market_data_type=1,
        halted_status=0,
    )
    account = FakeBroker().account_risk_snapshot(account="DU123456")
    exact_policy = LiveRiskPolicy.from_payload(
        _policy().to_payload()
        | {
            "max_order_notional": "200",
            "max_symbol_notional": "1000",
            "max_gross_notional": "2000",
        }
    )
    over_policy = LiveRiskPolicy.from_payload(
        exact_policy.to_payload() | {"max_order_notional": "199.99"}
    )

    exact = LiveRiskGateway(exact_policy).authorize(
        prepared,
        account=account,
        quote=quote,
        require_live_market_data=True,
    )
    over = LiveRiskGateway(over_policy).authorize(
        prepared,
        account=account,
        quote=quote,
        require_live_market_data=True,
    )
    assert exact.approved
    assert over.code == "BLOCK:ORDER_NOTIONAL"


def test_persistent_kill_survives_a_new_execution_sdk_instance() -> None:
    ledger, _ = _ledger()
    first_broker = FakeBroker()
    first = _execution_sdk(first_broker, ledger)
    first.kill(
        account="DU123456",
        reason="persistent safety stop",
        actor="risk-operator",
    )
    first.release_execution_lease()

    second_broker = FakeBroker()
    second = ExecutionSDK(
        broker=second_broker,
        ledger=ledger,
        risk=LiveRiskGateway(_policy()),
        safety=TradingSafetyController(
            TradingSafetyConfig(
                mode=TradingMode.PAPER,
                allowed_accounts=frozenset({"DU123456"}),
            )
        ),
    )
    with pytest.raises(PermissionError, match="persistent kill switch is active"):
        second.start(account="DU123456")
    assert second_broker.session_state == BrokerSessionState.KILLED
    assert ledger.kill_switch_reason("account:DU123456") == "persistent safety stop"


def test_clear_kill_requires_confirmation_reconciliation_and_fresh_live_arm() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    safety = TradingSafetyController(
        TradingSafetyConfig(
            mode=TradingMode.LIVE,
            allowed_accounts=frozenset({"U123456"}),
            live_enabled=True,
        )
    )
    sdk = ExecutionSDK(
        broker=broker,
        ledger=ledger,
        risk=LiveRiskGateway(_policy()),
        safety=safety,
        require_active_risk_policy_for_live=False,
    )
    sdk.start(account="U123456")
    safety.arm_live(account="U123456", confirmation="ARM-LIVE:U123456")
    sdk.kill(account="U123456", reason="manual stop", actor="risk-operator")

    with pytest.raises(TradingSafetyError, match="invalid"):
        sdk.clear_kill(
            account="U123456",
            actor="risk-operator",
            confirmation="CLEAR-KILL-SWITCH:WRONG",
        )
    sdk.clear_kill(
        account="U123456",
        actor="risk-operator",
        confirmation="CLEAR-KILL-SWITCH:U123456",
    )
    assert broker.session_state == BrokerSessionState.DEGRADED
    with pytest.raises(PermissionError, match="READY"):
        sdk._assert_ready()

    sdk.recover(account="U123456")
    with pytest.raises(TradingSafetyError, match="not armed"):
        safety.assert_can_transmit(account="U123456")
    safety.arm_live(account="U123456", confirmation="ARM-LIVE:U123456")
    safety.assert_can_transmit(account="U123456")
