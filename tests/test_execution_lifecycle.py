from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from platform_core.db.models import BrokerExecutionRecord, BrokerOrderEventRecord
from platform_core.infra.ibkr import _IBApiClient
from platform_core.schemas import (
    AssetType,
    BrokerExecution,
    BrokerOrderRequest,
    BrokerPosition,
    InstrumentRef,
)
from platform_core.sdk import (
    BracketOrderIntent,
    BrokerEvent,
    BrokerEventType,
    BrokerSessionState,
    ExecutionSDK,
    IBKRReconciliationSDK,
    IdempotencyConflictError,
    LiveOrderIntent,
    LiveRiskGateway,
    OrderCancelCommand,
    OrderLifecycleState,
    OptionLifecycleSDK,
    OrderReplaceCommand,
    ReconciliationBlockedError,
    SessionSupervisorSDK,
    TradingMode,
    TradingSafetyConfig,
    TradingSafetyController,
)
from platform_core.sdk.lifecycle import OrderSupervisorSDK
from tests.support.execution import (
    FakeBroker,
    _execution_sdk,
    _instrument,
    _intent,
    _ledger,
    _policy,
)


def test_ledger_idempotency_replays_same_payload_and_rejects_reuse() -> None:
    ledger, _ = _ledger()
    row, replay = ledger.create_or_get_intent(_intent())
    same, same_replay = ledger.create_or_get_intent(_intent())

    assert replay is False
    assert same_replay is True
    assert same.order_record_id == row.order_record_id
    with pytest.raises(IdempotencyConflictError):
        ledger.create_or_get_intent(_intent(quantity=Decimal(2)))


def test_broker_event_identity_requires_the_expected_client_id() -> None:
    ledger, _ = _ledger()
    row, _ = ledger.create_or_get_intent(_intent(), expected_broker_client_id=11)

    spoofed = ledger.find_for_broker_event(
        account="DU123456",
        broker_order_id=777,
        broker_client_id=22,
        order_ref="client-order-001",
    )
    owned = ledger.find_for_broker_event(
        account="DU123456",
        broker_order_id=777,
        broker_client_id=11,
        order_ref="client-order-001",
    )

    assert spoofed is None
    assert owned.order_record_id == row.order_record_id


def test_broker_order_id_alone_cannot_bind_an_unowned_legacy_order() -> None:
    ledger, _ = _ledger()
    ledger.create_or_get_intent(_intent())
    ledger.transition("client-order-001", OrderLifecycleState.AUTHORIZED)
    ledger.transition("client-order-001", OrderLifecycleState.SUBMITTING)
    status = FakeBroker._status(_intent().request, 777).model_copy(
        update={"client_id": None, "permanent_id": None}
    )
    ledger.apply_reconciled_status(
        "client-order-001",
        state=OrderLifecycleState.ACKNOWLEDGED,
        broker_status=status,
    )

    assert (
        ledger.find_for_broker_event(
            account="DU123456",
            broker_order_id=777,
            broker_client_id=22,
        )
        is None
    )


def test_database_submission_claim_can_only_be_won_once() -> None:
    ledger, _ = _ledger()
    ledger.create_or_get_intent(_intent())
    ledger.transition("client-order-001", OrderLifecycleState.AUTHORIZED)

    first_attempt = ledger.claim_submission("client-order-001")
    second_attempt = ledger.claim_submission("client-order-001")

    assert first_attempt is not None
    assert second_attempt is None
    row = ledger.get("client-order-001")
    assert row.state == OrderLifecycleState.SUBMITTING.value
    assert row.submission_attempt_id == first_attempt


def test_execution_lease_has_single_holder_and_expired_takeover() -> None:
    ledger, _ = _ledger()
    now = datetime.now(UTC)

    assert ledger.acquire_execution_lease(
        account="DU123456", holder_id="instance-a", ttl_seconds=10, now=now
    )
    assert not ledger.acquire_execution_lease(
        account="DU123456", holder_id="instance-b", ttl_seconds=10, now=now
    )
    assert ledger.acquire_execution_lease(
        account="DU123456",
        holder_id="instance-b",
        ttl_seconds=10,
        now=now + timedelta(seconds=11),
    )


def test_execution_sdk_never_resubmits_idempotent_replay() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)

    first = sdk.submit(_intent())
    replay = sdk.submit(_intent())

    assert first.state == OrderLifecycleState.ACKNOWLEDGED
    assert replay.idempotent_replay is True
    assert broker.place_calls == 1


def test_submission_timeout_is_unknown_and_retry_does_not_duplicate() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker(fail_submission=True)
    sdk = _execution_sdk(broker, ledger)

    first = sdk.submit(_intent())
    second = sdk.submit(_intent())

    assert first.state == OrderLifecycleState.UNKNOWN
    assert second.state == OrderLifecycleState.UNKNOWN
    assert second.idempotent_replay is True
    assert broker.place_calls == 1


def test_definitive_rejection_callback_wins_over_submission_exception() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    broker.config = SimpleNamespace(client_id=11, account="DU123456")
    sdk = _execution_sdk(broker, ledger)

    def reject(request, *, order_id=None):
        event = BrokerEvent(
            event_type=BrokerEventType.REJECTION,
            account=request.account,
            client_order_id=request.order_ref,
            broker_order_id=101,
            payload={
                "client_id": 11,
                "code": 201,
                "message": "simulated broker rejection",
            },
        )
        for handler in broker.handlers:
            handler(event)
        raise RuntimeError("placeOrder rejected")

    broker.place_order = reject

    result = sdk.submit(_intent())

    assert result.state == OrderLifecycleState.REJECTED
    assert "IBKR 201" in ledger.get("client-order-001").last_error


def test_replace_and_cancel_require_current_revision_and_preserve_identity() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    submitted = sdk.submit(_intent())
    row = ledger.get(submitted.client_order_id)
    replacement = row.request_payload | {"limit_price": "102"}

    replaced = sdk.replace(
        OrderReplaceCommand(
            client_order_id=row.client_order_id,
            expected_revision=row.revision,
            request=BrokerOrderRequest.model_validate(replacement),
        )
    )
    replaced_row = ledger.get(row.client_order_id)
    assert replaced.state == OrderLifecycleState.ACKNOWLEDGED
    assert replaced.broker_status.order_id == submitted.broker_status.order_id
    assert replaced.broker_status.permanent_id == submitted.broker_status.permanent_id
    assert replaced_row.pending_request_payload is None
    assert replaced_row.limit_price == Decimal(102)

    replay = sdk.replace(
        OrderReplaceCommand(
            client_order_id=row.client_order_id,
            expected_revision=row.revision,
            request=BrokerOrderRequest.model_validate(replacement),
        )
    )
    assert replay.idempotent_replay

    with pytest.raises(ValueError, match="revision conflict"):
        sdk.replace(
            OrderReplaceCommand(
                client_order_id=row.client_order_id,
                expected_revision=row.revision,
                request=BrokerOrderRequest.model_validate(replacement | {"limit_price": "103"}),
            )
        )

    cancelled = sdk.cancel(
        OrderCancelCommand(
            client_order_id=row.client_order_id,
            expected_revision=replaced_row.revision,
        )
    )
    assert cancelled.state == OrderLifecycleState.CANCELLED
    assert not broker._open_orders


def test_duplicate_ibkr_status_callback_does_not_invalidate_command_revision() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    submitted = sdk.submit(_intent())
    before = ledger.get(submitted.client_order_id)
    assert before is not None and submitted.broker_status is not None

    ledger.apply_reconciled_status(
        before.client_order_id,
        state=OrderLifecycleState.ACKNOWLEDGED,
        broker_status=submitted.broker_status.model_copy(
            update={
                "status": "Submitted",
                "updated_at": datetime.now(UTC) + timedelta(seconds=1),
            }
        ),
    )

    after = ledger.get(before.client_order_id)
    assert after is not None
    assert after.revision == before.revision
    replaced = sdk.replace(
        OrderReplaceCommand(
            client_order_id=after.client_order_id,
            expected_revision=after.revision,
            request=BrokerOrderRequest.model_validate(
                after.request_payload | {"limit_price": "102"}
            ),
        )
    )
    assert replaced.state == OrderLifecycleState.ACKNOWLEDGED


def test_rejected_replacement_preserves_confirmed_terms_until_reconciliation() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    broker.config = SimpleNamespace(client_id=11, account="DU123456")
    sdk = _execution_sdk(broker, ledger)
    submitted = sdk.submit(_intent())
    row = ledger.get(submitted.client_order_id)
    replacement = row.request_payload | {"limit_price": "102"}

    def reject_replace(order_id, request, *, expected_permanent_id=None):
        event = BrokerEvent(
            event_type=BrokerEventType.REJECTION,
            account=request.account,
            client_order_id=request.order_ref,
            broker_order_id=order_id,
            permanent_id=expected_permanent_id,
            payload={
                "client_id": 11,
                "code": 201,
                "message": "replacement rejected",
            },
        )
        for handler in broker.handlers:
            handler(event)
        raise RuntimeError("replacement rejected")

    broker.replace_order = reject_replace
    result = sdk.replace(
        OrderReplaceCommand(
            client_order_id=row.client_order_id,
            expected_revision=row.revision,
            request=BrokerOrderRequest.model_validate(replacement),
        )
    )
    uncertain = ledger.get(row.client_order_id)

    assert result.state == OrderLifecycleState.UNKNOWN
    assert uncertain.limit_price == Decimal(100)
    assert uncertain.pending_request_payload["limit_price"] == "102"

    report = sdk.reconciliation.run(account="DU123456", trigger="TEST")
    settled = ledger.get(row.client_order_id)

    assert report.ok
    assert settled.state == OrderLifecycleState.ACKNOWLEDGED.value
    assert settled.limit_price == Decimal(100)
    assert settled.pending_request_payload is None


def test_reconciliation_blocks_broker_order_term_mismatch() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    submitted = sdk.submit(_intent())
    broker._open_orders = [submitted.broker_status.model_copy(update={"limit_price": Decimal(103)})]

    report = sdk.reconciliation.run(account="DU123456", trigger="TEST")

    assert not report.ok
    assert any(issue.code == "ORDER_REQUEST_MISMATCH" for issue in report.issues)


def test_unsolicited_execution_is_kept_and_emitted() -> None:
    events = []
    client = object.__new__(_IBApiClient)
    client._executions_done = {}
    client._executions = {}
    client._live_executions = {}
    client._commissions = {}
    client._event_handler = events.append
    execution = SimpleNamespace(
        execId="exec-1",
        orderId=50,
        permId=9001,
        clientId=11,
        acctNumber="DU123456",
        side="BOT",
        shares=Decimal(1),
        price=Decimal(100),
        time="20260821 09:30:01 US/Eastern",
        exchange="ARCA",
        orderRef="client-order-001",
    )
    contract = SimpleNamespace(
        symbol="SPY",
        localSymbol="SPY",
        secType="STK",
        currency="USD",
        exchange="SMART",
        conId=756733,
    )

    client.execDetails(-1, contract, execution)

    assert "exec-1" in client._live_executions
    assert events[0].event_type == BrokerEventType.EXECUTION
    assert events[0].execution_id == "exec-1"


def test_execution_and_late_commission_are_persisted_once() -> None:
    ledger, factory = _ledger()
    ledger.create_or_get_intent(_intent())
    status = FakeBroker._status(_intent().request, 101)
    ledger.transition("client-order-001", OrderLifecycleState.AUTHORIZED)
    ledger.transition("client-order-001", OrderLifecycleState.SUBMITTING)
    ledger.apply_reconciled_status(
        "client-order-001", state=OrderLifecycleState.ACKNOWLEDGED, broker_status=status
    )
    execution = BrokerExecution(
        execution_id="exec-1",
        order_id=101,
        permanent_id=status.permanent_id,
        client_id=11,
        account="DU123456",
        instrument=_instrument(),
        side="BUY",
        quantity=Decimal(1),
        price=Decimal(100),
        executed_at=datetime.now(UTC),
        order_ref="client-order-001",
    )

    ledger.upsert_execution(execution)
    ledger.upsert_execution(execution)
    assert ledger.attach_commission(
        "exec-1",
        commission=Decimal("0.35"),
        currency="USD",
        realized_pnl=Decimal(0),
    )
    with factory() as session:
        rows = list(session.scalars(select(BrokerExecutionRecord)))
    assert len(rows) == 1
    assert rows[0].commission == Decimal("0.350000")
    assert ledger.get("client-order-001").state == OrderLifecycleState.FILLED.value


def test_commission_event_inherits_strategy_from_execution() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    submitted = sdk.submit(_intent())
    assert submitted.broker_status is not None
    execution = BrokerExecution(
        execution_id="strategy-commission.01",
        order_id=submitted.broker_status.order_id,
        permanent_id=submitted.broker_status.permanent_id,
        client_id=submitted.broker_status.client_id,
        account="DU123456",
        instrument=_instrument(),
        side="BUY",
        quantity=Decimal(1),
        price=Decimal(100),
        executed_at=datetime.now(UTC),
        order_ref="client-order-001",
    )
    ledger.upsert_execution(execution)

    ledger.record_event(
        BrokerEvent(
            event_type=BrokerEventType.COMMISSION,
            execution_id=execution.execution_id,
            payload={"commission": "0.25", "currency": "USD"},
        )
    )

    events = ledger.strategy_order_events("test-strategy")
    assert len(events) == 1
    assert events[0].event_type == BrokerEventType.COMMISSION
    assert events[0].client_order_id == "client-order-001"
    assert events[0].execution_id == execution.execution_id


def test_reconciliation_blocks_unmanaged_open_order() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    broker._open_orders = [FakeBroker._status(_intent().request, 777)]

    report = IBKRReconciliationSDK(broker=broker, ledger=ledger).run(account="DU123456")

    assert report.ok is False
    assert report.issues[0].code == "UNMANAGED_OPEN_ORDER"
    assert broker.session_state == BrokerSessionState.DEGRADED


def test_bracket_is_authorized_and_sent_as_one_batch() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    entry = _intent("bracket-entry-001", limit_price=Decimal(100))
    profit = LiveOrderIntent(
        client_order_id="bracket-profit-01",
        strategy_code="test-strategy",
        request=BrokerOrderRequest(
            instrument=_instrument(),
            side="SELL",
            quantity=Decimal(1),
            order_type="LMT",
            limit_price=Decimal(110),
            account="DU123456",
        ),
    )
    stop = LiveOrderIntent(
        client_order_id="bracket-stop-001",
        strategy_code="test-strategy",
        request=BrokerOrderRequest(
            instrument=_instrument(),
            side="SELL",
            quantity=Decimal(1),
            order_type="STP",
            stop_price=Decimal(90),
            account="DU123456",
        ),
    )

    results = sdk.submit_bracket(
        BracketOrderIntent(entry=entry, take_profit=profit, stop_loss=stop)
    )

    assert broker.bracket_calls == 1
    assert {result.state for result in results} == {OrderLifecycleState.ACKNOWLEDGED}


def test_ttl_expires_intent_that_never_reached_broker() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    expired_intent = _intent(
        "expired-order-01", expires_at=datetime.now(UTC) + timedelta(seconds=1)
    )
    ledger.create_or_get_intent(expired_intent)
    supervisor = OrderSupervisorSDK(execution=sdk, ledger=ledger, safety=sdk.safety)

    result = supervisor.expire_due_orders(
        account="DU123456", now=datetime.now(UTC) + timedelta(seconds=2)
    )

    assert result[0].state == OrderLifecycleState.EXPIRED


def test_duplicate_broker_event_is_deduplicated() -> None:
    ledger, factory = _ledger()
    normalized = BrokerEvent(
        event_type=BrokerEventType.CONNECTION,
        payload={"state": "LOST", "code": 1100},
    )
    assert ledger.record_event(normalized)
    assert not ledger.record_event(normalized)
    with factory() as session:
        assert len(list(session.scalars(select(BrokerOrderEventRecord)))) == 1


def test_snapshot_request_uses_no_illegal_generic_ticks() -> None:
    client = object.__new__(_IBApiClient)
    client.timeout_seconds = 1
    client._next_req_id = 1000
    from threading import Lock

    client._lock = Lock()
    client._snapshots = {}
    client._snapshot_done = {}
    client._request_errors = {}
    client.current_market_data_type = None
    client.errors = []
    captured = {}

    def request(req_id, contract, generic_ticks, snapshot, regulatory, options):
        captured["generic_ticks"] = generic_ticks
        client.marketDataType(req_id, 1)
        client.tickPrice(req_id, 1, 99.0, None)
        client.tickPrice(req_id, 2, 101.0, None)
        client.tickSnapshotEnd(req_id)

    client.reqMktData = request
    client.cancelMktData = lambda req_id: None

    result = client.request_snapshot(SimpleNamespace())

    assert captured["generic_ticks"] == ""
    assert result["market_data_type"] == 1


def test_tradeable_quote_requires_halt_state_and_captures_shortability() -> None:
    client = object.__new__(_IBApiClient)
    client.timeout_seconds = 1
    client._next_req_id = 1000
    from threading import Lock

    client._lock = Lock()
    client._snapshots = {}
    client._snapshot_done = {}
    client._tradeable_quotes = {}
    client._tradeable_quote_done = {}
    client._request_errors = {}
    client.current_market_data_type = None
    client.errors = []
    captured = {}

    def request(req_id, contract, generic_ticks, snapshot, regulatory, options):
        captured["generic_ticks"] = generic_ticks
        client.marketDataType(req_id, 1)
        client.tickPrice(req_id, 1, 99.0, None)
        client.tickPrice(req_id, 2, 101.0, None)
        client.tickGeneric(req_id, 46, 3.0)
        client.tickGeneric(req_id, 49, 0.0)

    client.reqMktData = request
    client.cancelMktData = lambda req_id: None

    result = client.request_tradeable_quote(SimpleNamespace())

    assert captured["generic_ticks"] == "236"
    assert result["halted_status"] == 0
    assert result["shortable"] == 3.0


def test_execution_correction_supersedes_prior_fill_version() -> None:
    ledger, factory = _ledger()
    ledger.create_or_get_intent(_intent())
    status = FakeBroker._status(_intent().request, 101)
    ledger.transition("client-order-001", OrderLifecycleState.AUTHORIZED)
    ledger.transition("client-order-001", OrderLifecycleState.SUBMITTING)
    ledger.apply_reconciled_status(
        "client-order-001", state=OrderLifecycleState.ACKNOWLEDGED, broker_status=status
    )

    def execution(execution_id: str, price: Decimal) -> BrokerExecution:
        return BrokerExecution(
            execution_id=execution_id,
            order_id=101,
            permanent_id=status.permanent_id,
            client_id=11,
            account="DU123456",
            instrument=_instrument(),
            side="BUY",
            quantity=Decimal(1),
            price=price,
            executed_at=datetime.now(UTC),
            order_ref="client-order-001",
        )

    ledger.upsert_execution(execution("trade.root.01", Decimal(100)))
    ledger.upsert_execution(execution("trade.root.02", Decimal(101)))

    with factory() as session:
        rows = list(
            session.scalars(
                select(BrokerExecutionRecord).order_by(BrokerExecutionRecord.execution_id)
            )
        )
    assert [row.superseded for row in rows] == [True, False]
    assert rows[1].is_correction is True
    assert ledger.get("client-order-001").avg_fill_price == Decimal("101.000000")


def test_commission_event_can_arrive_before_execution() -> None:
    ledger, factory = _ledger()
    ledger.record_event(
        BrokerEvent(
            event_type=BrokerEventType.COMMISSION,
            execution_id="commission-first.01",
            payload={
                "commission": "0.42",
                "currency": "USD",
                "realized_pnl": "1.25",
            },
        )
    )
    execution = BrokerExecution(
        execution_id="commission-first.01",
        order_id=1,
        account="DU123456",
        instrument=_instrument(),
        side="BUY",
        quantity=Decimal(1),
        price=Decimal(100),
        executed_at=datetime.now(UTC),
    )

    ledger.upsert_execution(execution)

    with factory() as session:
        row = session.scalar(select(BrokerExecutionRecord))
    assert row.commission == Decimal("0.420000")
    assert row.realized_pnl == Decimal("1.250000")


def test_session_supervisor_heartbeats_and_reconciles_recovery() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    supervisor = SessionSupervisorSDK(execution=sdk, heartbeat_interval_seconds=1)

    assert supervisor.check_once(account="DU123456") is None
    assert supervisor.last_heartbeat_at is not None

    broker.session_state = BrokerSessionState.RECOVERING
    report = supervisor.check_once(account="DU123456")
    assert report is not None and report.ok
    assert broker.session_state == BrokerSessionState.READY


def test_periodic_reconciliation_applies_managed_fills_to_expected_positions() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    submitted = sdk.submit(_intent())
    execution = BrokerExecution(
        execution_id="managed-execution-01",
        order_id=submitted.broker_status.order_id,
        permanent_id=submitted.broker_status.permanent_id,
        client_id=submitted.broker_status.client_id,
        account="DU123456",
        instrument=_instrument(),
        side="BUY",
        quantity=Decimal(1),
        price=Decimal(100),
        executed_at=datetime.now(UTC),
        order_ref="client-order-001",
    )
    broker._executions = [execution]
    broker._positions = [
        BrokerPosition(
            account="DU123456",
            instrument=_instrument(),
            quantity=Decimal(1),
            avg_cost=Decimal(100),
        )
    ]
    filled = submitted.broker_status.model_copy(
        update={
            "status": "Filled",
            "filled": Decimal(1),
            "remaining": Decimal(0),
            "updated_at": datetime.now(UTC),
        }
    )
    broker._open_orders = []
    broker._completed_orders = [filled]

    report = sdk.audit_broker_state(account="DU123456")

    assert report.ok
    assert ledger.current_positions("DU123456")[0].quantity == Decimal(1)


def test_periodic_reconciliation_kills_on_unexplained_position_change() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    broker._positions = [
        BrokerPosition(
            account="DU123456",
            instrument=_instrument(),
            quantity=Decimal(1),
            avg_cost=Decimal(100),
        )
    ]

    with pytest.raises(ReconciliationBlockedError) as blocked:
        sdk.audit_broker_state(account="DU123456")

    assert blocked.value.report.issues[0].code == "POSITION_MISMATCH"
    assert "periodic broker reconciliation blocked" in ledger.kill_switch_reason("account:DU123456")


def test_session_supervisor_runs_periodic_reconciliation_when_due() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    supervisor = SessionSupervisorSDK(
        execution=sdk,
        heartbeat_interval_seconds=1,
        reconciliation_interval_seconds=1,
    )
    supervisor.last_reconciliation_at = datetime.now(UTC) - timedelta(seconds=2)

    report = supervisor.check_once(account="DU123456")

    assert report is not None and report.ok
    assert supervisor.last_reconciliation_at == report.completed_at


def test_reconciliation_requires_explicit_initial_position_adoption() -> None:
    ledger, _ = _ledger()
    broker = FakeBroker()
    broker._positions = [
        SimpleNamespace(
            account="DU123456",
            instrument=_instrument(),
            quantity=Decimal(2),
            avg_cost=Decimal(100),
            model_dump=lambda **_: {
                "account": "DU123456",
                "instrument": _instrument().model_dump(mode="json"),
                "quantity": "2",
                "avg_cost": "100",
            },
        )
    ]
    sdk = ExecutionSDK(
        broker=broker,
        ledger=ledger,
        risk=LiveRiskGateway(_policy()),
        safety=TradingSafetyController(
            TradingSafetyConfig(
                mode=TradingMode.PAPER,
                allowed_accounts=frozenset({"DU123456"}),
            )
        ),
    )

    with pytest.raises(ReconciliationBlockedError) as blocked:
        sdk.start(account="DU123456")
    assert blocked.value.report.issues[0].code == "POSITION_BASELINE_REQUIRED"

    adopted = sdk.adopt_positions(
        account="DU123456",
        actor="operator@example.com",
        confirmation="ADOPT-POSITIONS:DU123456",
    )
    assert adopted.ok


def test_option_lifecycle_requires_and_forwards_execution_capability() -> None:
    class CapabilityBroker(FakeBroker):
        def configure_execution_boundary(self, token: object) -> None:
            self.execution_boundary = token

        def exercise_option(self, *, execution_token=None, **kwargs):
            assert execution_token is self.execution_boundary
            self.exercise_calls.append(kwargs)
            return 8123

    ledger, _ = _ledger()
    broker = CapabilityBroker()
    sdk = _execution_sdk(broker, ledger)
    option = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=76543,
        option_right="CALL",
        strike=Decimal(500),
        expiry=datetime(2026, 9, 18, tzinfo=UTC).date(),
    )
    request = {
        "account": "DU123456",
        "instrument": option,
        "quantity": Decimal(1),
        "action": "EXERCISE",
        "confirmation": "EXERCISE:DU123456:76543:1",
    }

    without_execution = OptionLifecycleSDK(broker=broker, ledger=ledger)
    with pytest.raises(PermissionError, match="ExecutionSDK capability"):
        without_execution.request_exercise_or_lapse(**request)

    lifecycle = OptionLifecycleSDK(broker=broker, ledger=ledger, execution=sdk)
    assert lifecycle.request_exercise_or_lapse(**request) == 8123
    assert broker.exercise_calls[0]["account"] == "DU123456"
