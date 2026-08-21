from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from platform_apps.common import (
    authenticate_service_identity,
    require_service_api_key,
    require_service_role,
    require_strategy_access,
    verified_actor,
)
from platform_apps.exec_svc import main as exec_app
from platform_core.schemas import AssetType, BrokerOrderRequest, InstrumentRef
from platform_core.sdk import (
    BracketOrderIntent,
    BrokerEvent,
    BrokerEventType,
    ComboLegRef,
    DefinedRiskOptionComboIntent,
    LiveOrderIntent,
    OCAOrderIntentGroup,
    OrderCancelCommand,
    OrderReplaceCommand,
    StrategyExecutionClient,
    StrategyExecutionClientConfig,
    StrategyExecutionClientError,
    TradingMode,
)
from platform_core.sdk import runtime as runtime_module
from platform_core.sdk.client import _RejectRedirects
from tests.support.execution import FakeBroker, _execution_sdk, _intent, _ledger

ALPHA_KEY = "alpha-secret-key-1234"
BETA_KEY = "beta-secret-key-12345"
OPERATOR_KEY = "operator-secret-key-1"


@dataclass
class FakeResponse:
    status_code: int
    payload: Any

    @property
    def text(self) -> str:
        return str(self.payload)

    def json(self) -> Any:
        return self.payload


class FakeHTTPClient:
    def __init__(self, response: FakeResponse | None = None, *, error: OSError | None = None):
        self.response = response or FakeResponse(200, {})
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        return None


def _request() -> BrokerOrderRequest:
    return BrokerOrderRequest(
        instrument=InstrumentRef(asset_type=AssetType.ETF, symbol="SPY"),
        side="BUY",
        quantity=Decimal(1),
        order_type="LMT",
        limit_price=Decimal(100),
    )


def _strategy_intent(
    strategy_code: str,
    client_order_id: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> LiveOrderIntent:
    return _intent(client_order_id).model_copy(
        update={"strategy_code": strategy_code, "metadata": metadata or {}}
    )


def _headers(strategy_code: str, key: str) -> dict[str, str]:
    return {"X-API-Key": key, "X-Strategy-Code": strategy_code}


@pytest.fixture
def execution_api(monkeypatch: pytest.MonkeyPatch):
    identities = {
        "strategy-alpha": {
            "key": ALPHA_KEY,
            "roles": ["read", "order_submitter"],
            "strategies": ["alpha"],
        },
        "strategy-beta": {
            "key": BETA_KEY,
            "roles": ["read", "order_submitter"],
            "strategies": ["beta"],
        },
        "execution-operator": {
            "key": OPERATOR_KEY,
            "roles": ["read", "execution_operator", "risk_operator"],
        },
    }
    monkeypatch.setenv("TRADING_MODE", "PAPER")
    monkeypatch.setenv("SERVICE_API_IDENTITIES", json.dumps(identities))
    ledger, _ = _ledger()
    broker = FakeBroker()
    sdk = _execution_sdk(broker, ledger)
    captured_intents: list[LiveOrderIntent] = []
    original_submit = sdk.submit

    def capture_submit(intent: LiveOrderIntent):
        captured_intents.append(intent)
        return original_submit(intent)

    sdk.submit = capture_submit
    runtime = SimpleNamespace(execution=sdk, ledger=ledger, broker=broker)
    monkeypatch.setattr(exec_app, "_runtime", lambda: runtime)

    # TestClient 通过 httpx 的进程内 ASGI transport 验证完整路由，不开放本地端口。
    client = TestClient(exec_app.app)
    yield client, ledger, broker, captured_intents
    client.close()


def test_execution_service_health_is_public_but_sensitive_routes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert exec_app.healthz()["status"] == "ok"

    monkeypatch.setenv("SERVICE_API_KEYS", "")
    with pytest.raises(HTTPException) as missing_config:
        require_service_api_key(None)
    assert missing_config.value.status_code == 503

    monkeypatch.setenv("SERVICE_API_KEYS", "test-secret")
    with pytest.raises(HTTPException) as invalid_key:
        require_service_api_key("wrong")
    assert invalid_key.value.status_code == 401
    assert require_service_api_key("test-secret") == "test-secret"


def test_live_mode_requires_role_bound_service_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("SERVICE_API_KEYS", "legacy-secret")
    monkeypatch.setenv("SERVICE_API_IDENTITIES", "")

    with pytest.raises(HTTPException) as missing_identity:
        authenticate_service_identity("legacy-secret")
    assert missing_identity.value.status_code == 503


def test_role_bound_identity_enforces_rbac_and_audit_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv(
        "SERVICE_API_IDENTITIES",
        '{"risk-approver@example.com":{"key":"0123456789abcdef","roles":["read","risk_approver"]}}',
    )
    identity = authenticate_service_identity("0123456789abcdef")

    assert identity.actor == "risk-approver@example.com"
    assert require_service_role("risk_approver")(x_api_key="0123456789abcdef") == identity
    with pytest.raises(HTTPException) as forbidden:
        require_service_role("order_submitter")(x_api_key="0123456789abcdef")
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as spoofed:
        verified_actor(identity, "someone-else@example.com")
    assert spoofed.value.status_code == 403
    assert verified_actor(identity, None) == "risk-approver@example.com"


def test_live_order_submitter_identity_is_bound_to_explicit_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv(
        "SERVICE_API_IDENTITIES",
        '{"strategy-alpha":{"key":"alpha-secret-key-1234","roles":["read","order_submitter"]}}',
    )
    with pytest.raises(HTTPException) as unbound:
        authenticate_service_identity(ALPHA_KEY)
    assert unbound.value.status_code == 503

    monkeypatch.setenv(
        "SERVICE_API_IDENTITIES",
        '{"strategy-alpha":{"key":"alpha-secret-key-1234",'
        '"roles":["read","order_submitter"],"strategies":["alpha"]}}',
    )
    identity = authenticate_service_identity(ALPHA_KEY)
    assert identity.strategy_codes == frozenset({"alpha"})
    assert require_strategy_access(identity, "alpha", header_strategy_code="alpha") == "alpha"

    with pytest.raises(HTTPException) as impersonation:
        require_strategy_access(identity, "beta", header_strategy_code="beta")
    assert impersonation.value.status_code == 403
    with pytest.raises(HTTPException) as header_spoof:
        require_strategy_access(identity, "alpha", header_strategy_code="beta")
    assert header_spoof.value.status_code == 403
    assert exec_app._api_error(header_spoof.value).status_code == 403


def test_strategy_client_submits_typed_intent_with_bound_identity_headers() -> None:
    http = FakeHTTPClient(
        FakeResponse(
            200,
            {
                "client_order_id": "alpha-order-001",
                "state": "ACKNOWLEDGED",
                "idempotent_replay": False,
            },
        )
    )
    client = StrategyExecutionClient(
        strategy_code="alpha",
        config=StrategyExecutionClientConfig(
            base_url="http://127.0.0.1:8002",
            api_key=ALPHA_KEY,
        ),
        http_client=http,
    )
    intent = LiveOrderIntent(
        client_order_id="alpha-order-001",
        strategy_code="alpha",
        request=_request(),
        created_at=datetime.now(UTC),
    )

    result = client.submit(intent)

    assert result.client_order_id == "alpha-order-001"
    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:8002/v1/orders"
    assert kwargs["headers"]["X-Strategy-Code"] == "alpha"
    assert kwargs["headers"]["X-API-Key"] == ALPHA_KEY
    assert kwargs["json"]["strategy_code"] == "alpha"


def test_strategy_client_refuses_cross_strategy_intent_before_network() -> None:
    http = FakeHTTPClient()
    client = StrategyExecutionClient(
        strategy_code="alpha",
        config=StrategyExecutionClientConfig(
            base_url="http://localhost:8002",
            api_key=ALPHA_KEY,
        ),
        http_client=http,
    )

    with pytest.raises(PermissionError, match="cannot submit"):
        client.submit(
            LiveOrderIntent(
                client_order_id="beta-order-001",
                strategy_code="beta",
                request=_request(),
            )
        )
    assert http.calls == []


@pytest.mark.parametrize(
    ("base_url", "message"),
    [
        ("http://execution.internal:8002", "must use HTTPS"),
        ("https://user:password@execution.internal", "must not contain credentials"),
        ("https://execution.internal/path?secret=value", "query or fragment"),
    ],
)
def test_strategy_client_rejects_unsafe_service_urls(base_url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        StrategyExecutionClientConfig(base_url=base_url, api_key=ALPHA_KEY)


def test_strategy_client_does_not_follow_redirects_or_disclose_credential() -> None:
    config = StrategyExecutionClientConfig(
        base_url="https://execution.internal",
        api_key=ALPHA_KEY,
    )
    assert ALPHA_KEY not in repr(config)
    assert _RejectRedirects().redirect_request() is None

    client = StrategyExecutionClient(
        strategy_code="alpha",
        config=config,
        http_client=FakeHTTPClient(FakeResponse(302, {"detail": "redirect blocked"})),
    )
    with pytest.raises(StrategyExecutionClientError) as denied:
        client.place(_request())
    assert denied.value.status_code == 302
    assert ALPHA_KEY not in str(denied.value)


def test_strategy_client_escapes_order_ids_and_normalizes_transport_errors() -> None:
    http = FakeHTTPClient(FakeResponse(200, {"state": "ACKNOWLEDGED"}))
    config = StrategyExecutionClientConfig(
        base_url="https://execution.internal",
        api_key=ALPHA_KEY,
    )
    client = StrategyExecutionClient(strategy_code="alpha", config=config, http_client=http)

    assert client.order("alpha/order ?") == {"state": "ACKNOWLEDGED"}
    assert http.calls[0][1].endswith("/v1/orders/alpha%2Forder%20%3F")

    unavailable = StrategyExecutionClient(
        strategy_code="alpha",
        config=config,
        http_client=FakeHTTPClient(error=OSError("transport down")),
    )
    with pytest.raises(StrategyExecutionClientError) as failed:
        unavailable.readiness()
    assert failed.value.status_code == 0
    assert "unavailable" in failed.value.detail


def test_non_execution_services_cannot_construct_an_order_capable_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runtime.db'}")
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("IB_MARKET_DATA_TYPE", "1")
    runtime_module._get_trading_runtime.cache_clear()
    runtime_module.get_risk_control_runtime.cache_clear()
    try:
        market_data = runtime_module.get_read_only_runtime("md")
        pnl = runtime_module.get_read_only_runtime("pnl")
        risk = runtime_module.get_risk_control_runtime()

        assert market_data.safety.config.mode == TradingMode.READ_ONLY
        assert pnl.safety.config.mode == TradingMode.READ_ONLY
        assert not hasattr(risk, "broker")
        with pytest.raises(ValueError, match="must be 'md' or 'pnl'"):
            runtime_module.get_read_only_runtime("exec")
    finally:
        runtime_module._get_trading_runtime.cache_clear()
        runtime_module.get_risk_control_runtime.cache_clear()


def test_asgi_submit_binds_authenticated_actor_and_serializes_result(execution_api) -> None:
    client, _, broker, captured_intents = execution_api
    intent = _strategy_intent("alpha", "alpha-order-001")

    response = client.post(
        "/v1/orders",
        headers=_headers("alpha", ALPHA_KEY),
        json=intent.model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["state"] == "ACKNOWLEDGED"
    assert response.json()["client_order_id"] == "alpha-order-001"
    assert captured_intents[0].metadata["authenticated_actor"] == "strategy-alpha"
    assert broker.place_calls == 1


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"X-Strategy-Code": "alpha"}, 401),
        (_headers("alpha", BETA_KEY), 403),
        (_headers("beta", ALPHA_KEY), 403),
        (_headers("alpha", OPERATOR_KEY), 403),
    ],
)
def test_asgi_submit_rejects_missing_or_mismatched_identity(
    execution_api,
    headers: dict[str, str],
    expected_status: int,
) -> None:
    client, _, broker, _ = execution_api
    response = client.post(
        "/v1/orders",
        headers=headers,
        json=_strategy_intent("alpha", "alpha-denied-001").model_dump(mode="json"),
    )
    assert response.status_code == expected_status
    assert broker.place_calls == 0


def test_asgi_rejects_actor_metadata_spoofing_before_broker(execution_api) -> None:
    client, _, broker, _ = execution_api
    intent = _strategy_intent(
        "alpha",
        "alpha-spoof-001",
        metadata={"authenticated_actor": "strategy-beta"},
    )

    response = client.post(
        "/v1/orders",
        headers=_headers("alpha", ALPHA_KEY),
        json=intent.model_dump(mode="json"),
    )

    assert response.status_code == 403
    assert broker.place_calls == 0


def test_asgi_prevents_cross_strategy_read_replace_and_cancel(execution_api) -> None:
    client, ledger, broker, _ = execution_api
    intent = _strategy_intent("alpha", "alpha-owned-001")
    submitted = client.post(
        "/v1/orders",
        headers=_headers("alpha", ALPHA_KEY),
        json=intent.model_dump(mode="json"),
    )
    assert submitted.status_code == 200
    row = ledger.get("alpha-owned-001")
    replacement = intent.request.model_copy(update={"limit_price": Decimal(101)})
    attempts = [
        ("GET", "/v1/orders/alpha-owned-001", None),
        (
            "POST",
            "/v1/orders/replace",
            OrderReplaceCommand(
                client_order_id=row.client_order_id,
                expected_revision=row.revision,
                request=replacement,
            ).model_dump(mode="json"),
        ),
        (
            "POST",
            "/v1/orders/cancel",
            OrderCancelCommand(
                client_order_id=row.client_order_id,
                expected_revision=row.revision,
            ).model_dump(mode="json"),
        ),
    ]

    for method, path, payload in attempts:
        response = client.request(
            method,
            path,
            headers=_headers("beta", BETA_KEY),
            json=payload,
        )
        assert response.status_code == 403
    assert broker.place_calls == 1
    assert ledger.get("alpha-owned-001").revision == row.revision


def test_asgi_rejects_mixed_strategy_bracket_and_oca_atomically(execution_api) -> None:
    client, _, broker, _ = execution_api
    entry = _strategy_intent("alpha", "alpha-entry-001")
    take_profit = _strategy_intent("alpha", "alpha-profit-001").model_copy(
        update={
            "request": entry.request.model_copy(
                update={
                    "side": "SELL",
                    "limit_price": Decimal(110),
                    "order_ref": "alpha-profit-001",
                }
            )
        }
    )
    stop_loss = _strategy_intent("beta", "beta-stop-0001").model_copy(
        update={
            "request": entry.request.model_copy(
                update={
                    "side": "SELL",
                    "order_type": "STP",
                    "limit_price": None,
                    "stop_price": Decimal(90),
                    "order_ref": "beta-stop-0001",
                }
            )
        }
    )
    bracket = BracketOrderIntent(
        entry=entry,
        take_profit=take_profit,
        stop_loss=stop_loss,
    )
    oca = OCAOrderIntentGroup(
        group_id="mixed-oca-001",
        orders=[entry, _strategy_intent("beta", "beta-oca-0001")],
    )

    bracket_response = client.post(
        "/v1/orders/bracket",
        headers=_headers("alpha", ALPHA_KEY),
        json=bracket.model_dump(mode="json"),
    )
    oca_response = client.post(
        "/v1/orders/oca",
        headers=_headers("alpha", ALPHA_KEY),
        json=oca.model_dump(mode="json"),
    )

    assert bracket_response.status_code == 422
    assert oca_response.status_code == 422
    assert broker.bracket_calls == 0
    assert broker.place_calls == 0


def test_asgi_rejects_cross_strategy_combo_before_runtime_dispatch(execution_api) -> None:
    client, _, broker, _ = execution_api
    expiry = datetime(2026, 9, 18, tzinfo=UTC).date()
    long_call = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=6001,
        option_right="CALL",
        strike=Decimal(500),
        expiry=expiry,
    )
    short_call = long_call.model_copy(update={"conid": 6002, "strike": Decimal(510)})
    combo = DefinedRiskOptionComboIntent(
        client_order_id="beta-combo-0001",
        strategy_code="beta",
        legs=[
            ComboLegRef(instrument=long_call, action="BUY"),
            ComboLegRef(instrument=short_call, action="SELL"),
        ],
        quantity=Decimal(1),
        limit_price=Decimal(2),
        account="DU123456",
    )

    response = client.post(
        "/v1/orders/combo",
        headers=_headers("alpha", ALPHA_KEY),
        json=combo.model_dump(mode="json"),
    )

    assert response.status_code == 403
    assert broker.place_calls == 0


def test_asgi_operator_cannot_spoof_kill_switch_actor(execution_api) -> None:
    client, _, _, _ = execution_api
    response = client.post(
        "/v1/kill-switch",
        headers={"X-API-Key": OPERATOR_KEY},
        json={
            "account": "DU123456",
            "reason": "operator request",
            "actor": "strategy-alpha",
        },
    )
    assert response.status_code == 403


def test_asgi_option_action_uses_execution_capability_boundary(execution_api) -> None:
    client, _, broker, _ = execution_api
    option = InstrumentRef(
        asset_type=AssetType.OPTION,
        symbol="SPY",
        conid=54321,
        option_right="CALL",
        strike=Decimal(500),
        expiry=datetime(2026, 9, 18, tzinfo=UTC).date(),
    )
    response = client.post(
        "/v1/options/action",
        headers={"X-API-Key": OPERATOR_KEY},
        json={
            "account": "DU123456",
            "instrument": option.model_dump(mode="json"),
            "quantity": "1",
            "action": "EXERCISE",
            "confirmation": "EXERCISE:DU123456:54321:1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"request_id": 7001}
    assert broker.exercise_calls[0]["account"] == "DU123456"


def test_strategy_client_reads_resumable_order_event_pages() -> None:
    http = FakeHTTPClient(
        FakeResponse(
            200,
            {
                "events": [
                    {
                        "event_id": 41,
                        "event_type": "ORDER_STATUS",
                        "event_time": datetime.now(UTC).isoformat(),
                        "received_at": datetime.now(UTC).isoformat(),
                        "client_order_id": "alpha-order-001",
                        "broker_order_id": 101,
                        "payload": {"status": "PartiallyFilled"},
                    }
                ],
                "next_event_id": 41,
            },
        )
    )
    client = StrategyExecutionClient(
        strategy_code="alpha",
        config=StrategyExecutionClientConfig(
            base_url="http://127.0.0.1:8002",
            api_key=ALPHA_KEY,
        ),
        http_client=http,
    )

    page = client.order_events(after_event_id=40, limit=25, wait_seconds=3)

    assert page.next_event_id == 41
    assert page.events[0].client_order_id == "alpha-order-001"
    method, url, kwargs = http.calls[0]
    assert method == "GET"
    assert "after_event_id=40" in url
    assert "wait_seconds=3" in url
    assert kwargs["headers"]["X-Strategy-Code"] == "alpha"


def test_order_event_feed_is_durable_and_strategy_scoped(execution_api) -> None:
    client, ledger, _, _ = execution_api
    alpha_id = "alpha-event-order-001"
    beta_id = "beta-event-order-0001"
    for strategy, key, client_order_id in (
        ("alpha", ALPHA_KEY, alpha_id),
        ("beta", BETA_KEY, beta_id),
    ):
        response = client.post(
            "/v1/orders",
            headers=_headers(strategy, key),
            json=_strategy_intent(strategy, client_order_id).model_dump(mode="json"),
        )
        assert response.status_code == 200
        row = ledger.get(client_order_id)
        assert row is not None
        ledger.record_event(
            BrokerEvent(
                event_type=BrokerEventType.ORDER_STATUS,
                account=row.account,
                client_order_id=client_order_id,
                broker_order_id=row.broker_order_id,
                permanent_id=row.permanent_id,
                payload={"status": "PartiallyFilled", "client_id": row.broker_client_id},
            )
        )

    alpha_page = client.get(
        "/v1/order-events?after_event_id=0&wait_seconds=0",
        headers=_headers("alpha", ALPHA_KEY),
    )
    assert alpha_page.status_code == 200
    payload = alpha_page.json()
    assert {event["client_order_id"] for event in payload["events"]} == {alpha_id}
    cursor = payload["next_event_id"]

    empty_resume = client.get(
        f"/v1/order-events?after_event_id={cursor}&wait_seconds=0",
        headers=_headers("alpha", ALPHA_KEY),
    )
    assert empty_resume.status_code == 200
    assert empty_resume.json() == {"events": [], "next_event_id": cursor}

    forged = client.get(
        "/v1/order-events",
        headers=_headers("beta", ALPHA_KEY),
    )
    assert forged.status_code == 403
