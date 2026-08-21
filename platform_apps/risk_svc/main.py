from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel

from platform_apps.common import (
    ServiceIdentity,
    require_service_role,
    verified_actor,
)
from platform_core.schemas import MarketQuote
from platform_core.sdk.models import AccountRiskSnapshot, LiveOrderIntent
from platform_core.sdk.risk import LiveRiskPolicy
from platform_core.sdk.runtime import get_risk_control_runtime

app = FastAPI(title="Risk Service", version="1.0")


class AuthorizationRequest(BaseModel):
    intent: LiveOrderIntent
    account: AccountRiskSnapshot
    quote: MarketQuote
    require_live_market_data: bool = False


class ProposePolicyRequest(BaseModel):
    scope: str
    actor: str | None = None
    policy: dict[str, Any]


class PolicyActionRequest(BaseModel):
    actor: str | None = None
    confirmation: str | None = None


class RollbackPolicyRequest(BaseModel):
    scope: str
    version: int
    actor: str | None = None
    confirmation: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "risk_svc"}


@app.get("/readyz", dependencies=[Depends(require_service_role("read"))])
def readyz() -> dict[str, bool]:
    database_ready = get_risk_control_runtime().ledger.healthcheck()
    if not database_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"ready": False, "database_ready": False},
        )
    return {"ready": True}


@app.get("/v1/policy", dependencies=[Depends(require_service_role("read"))])
def policy(account: str | None = None) -> dict[str, Any]:
    try:
        runtime = get_risk_control_runtime()
        value = runtime.risk.policy_for(account) if account else runtime.risk.policy
        scope = f"account:{account}" if account else "global"
        active = runtime.risk_controls.active(scope)
        if active is None and account:
            active = runtime.risk_controls.active("global")
        return {
            "scope": scope,
            "source": "database" if active is not None else "environment-default",
            "effective_scope": active.scope if active is not None else "environment-default",
            "active_version": active.version if active is not None else None,
            "active_policy_id": active.policy_id if active is not None else None,
            "policy": value.to_payload(),
            "policy_fingerprint": value.fingerprint,
        }
    except Exception as exc:  # noqa: BLE001
        raise _api_error(exc) from exc


@app.post("/v1/policies")
def propose_policy(
    request: ProposePolicyRequest,
    identity: ServiceIdentity = Depends(require_service_role("risk_proposer")),
):
    actor = verified_actor(identity, request.actor)
    try:
        runtime = get_risk_control_runtime()
        return runtime.risk_controls.propose(
            scope=request.scope,
            policy=LiveRiskPolicy.from_payload(request.policy),
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        raise _api_error(exc) from exc


@app.post("/v1/policies/{policy_id}/approve")
def approve_policy(
    policy_id: str,
    request: PolicyActionRequest,
    identity: ServiceIdentity = Depends(require_service_role("risk_approver")),
):
    actor = verified_actor(identity, request.actor)
    try:
        return get_risk_control_runtime().risk_controls.approve(
            policy_id=policy_id, actor=actor
        )
    except Exception as exc:  # noqa: BLE001
        raise _api_error(exc) from exc


@app.post("/v1/policies/{policy_id}/activate")
def activate_policy(
    policy_id: str,
    request: PolicyActionRequest,
    identity: ServiceIdentity = Depends(require_service_role("risk_operator")),
):
    actor = verified_actor(identity, request.actor)
    if request.confirmation is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="activation confirmation is required",
        )
    try:
        return get_risk_control_runtime().risk_controls.activate(
            policy_id=policy_id,
            actor=actor,
            confirmation=request.confirmation,
        )
    except Exception as exc:  # noqa: BLE001
        raise _api_error(exc) from exc


@app.post("/v1/policies/rollback")
def rollback_policy(
    request: RollbackPolicyRequest,
    identity: ServiceIdentity = Depends(require_service_role("risk_operator")),
):
    actor = verified_actor(identity, request.actor)
    try:
        return get_risk_control_runtime().risk_controls.rollback(
            scope=request.scope,
            version=request.version,
            actor=actor,
            confirmation=request.confirmation,
        )
    except Exception as exc:  # noqa: BLE001
        raise _api_error(exc) from exc


@app.get("/v1/policies", dependencies=[Depends(require_service_role("read"))])
def policy_versions(scope: str = "global"):
    try:
        return get_risk_control_runtime().risk_controls.versions(scope)
    except Exception as exc:  # noqa: BLE001
        raise _api_error(exc) from exc


@app.post(
    "/v1/authorize", dependencies=[Depends(require_service_role("risk_authorizer"))]
)
def authorize(request: AuthorizationRequest):
    try:
        runtime = get_risk_control_runtime()
        decision = runtime.risk.authorize(
            request.intent,
            account=request.account,
            quote=request.quote,
            require_live_market_data=request.require_live_market_data,
        )
        runtime.ledger.record_risk_decision(decision)
        runtime.metrics.increment(
            "trading_risk_decisions_total",
            result="approved" if decision.approved else "rejected",
        )
        return decision
    except Exception as exc:  # noqa: BLE001
        raise _api_error(exc) from exc


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (PermissionError, ValueError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
