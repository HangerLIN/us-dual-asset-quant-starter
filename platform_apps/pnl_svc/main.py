from __future__ import annotations

from datetime import date, datetime

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel

from platform_apps.common import (
    ServiceIdentity,
    require_service_role,
    verified_actor,
)
from platform_core.sdk.lifecycle import OptionLifecycleSDK
from platform_core.sdk.models import BrokerSessionState
from platform_core.sdk.runtime import get_read_only_runtime
from platform_core.sdk.statement import BrokerStatementSnapshot

app = FastAPI(title="PnL Service", version="1.0")


class AccountRequest(BaseModel):
    account: str | None = None


class SubscriptionRequest(BaseModel):
    account: str | None = None


class ExpiryQuery(BaseModel):
    account: str
    as_of: date | None = None
    close_days: int = 1
    warning_days: int = 5


class EndOfDayRequest(BaseModel):
    statement: BrokerStatementSnapshot
    actor: str | None = None


class FlexEndOfDayRequest(BaseModel):
    account: str
    period_start: datetime
    period_end: datetime
    actor: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "pnl_svc"}


@app.get("/readyz", dependencies=[Depends(require_service_role("read"))])
def readyz() -> dict[str, str | bool]:
    state = get_read_only_runtime("pnl").broker.session_state
    payload = {
        "ready": state
        in {
            BrokerSessionState.RECOVERING,
            BrokerSessionState.RECONCILING,
            BrokerSessionState.READY,
        },
        "broker_state": state.value,
    }
    if not payload["ready"]:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)
    return payload


@app.post("/v1/session/start", dependencies=[Depends(require_service_role("read"))])
def start_session(request: AccountRequest) -> dict[str, str]:
    runtime = get_read_only_runtime("pnl")
    try:
        runtime.broker.connect()
        account = runtime.broker.resolve_account(request.account)
        runtime.broker.subscribe_pnl(account=account)
        return {"status": "SUBSCRIBED", "account": account}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.get("/v1/pnl", dependencies=[Depends(require_service_role("read"))])
def pnl(account: str | None = None):
    try:
        return get_read_only_runtime("pnl").broker.pnl_snapshot(account=account)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.get("/v1/account-snapshot", dependencies=[Depends(require_service_role("read"))])
def account_snapshot(account: str | None = None):
    runtime = get_read_only_runtime("pnl")
    try:
        selected = runtime.broker.resolve_account(account)
        return runtime.execution.refresh_account_snapshot(selected)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.get("/v1/positions", dependencies=[Depends(require_service_role("read"))])
def positions(account: str | None = None):
    try:
        return get_read_only_runtime("pnl").broker.positions(account=account)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.get("/v1/executions", dependencies=[Depends(require_service_role("read"))])
def executions(account: str | None = None, since: datetime | None = None):
    try:
        return get_read_only_runtime("pnl").broker.executions(
            account=account, since=since, all_clients=True
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.post("/v1/options/expiring", dependencies=[Depends(require_service_role("read"))])
def expiring_options(request: ExpiryQuery):
    runtime = get_read_only_runtime("pnl")
    lifecycle = OptionLifecycleSDK(broker=runtime.broker, ledger=runtime.ledger)
    try:
        return lifecycle.expiring_positions(
            account=request.account,
            as_of=request.as_of,
            close_days=request.close_days,
            warning_days=request.warning_days,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.post("/v1/reconciliation/eod")
def reconcile_end_of_day(
    request: EndOfDayRequest,
    identity: ServiceIdentity = Depends(require_service_role("reconciler")),
):
    actor = verified_actor(identity, request.actor)
    try:
        return get_read_only_runtime("pnl").end_of_day.reconcile(
            request.statement,
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@app.post("/v1/reconciliation/eod/flex")
def reconcile_end_of_day_from_flex(
    request: FlexEndOfDayRequest,
    identity: ServiceIdentity = Depends(require_service_role("reconciler")),
):
    actor = verified_actor(identity, request.actor)
    runtime = get_read_only_runtime("pnl")
    if runtime.statement_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="IB_FLEX_TOKEN and IB_FLEX_QUERY_ID are not configured",
        )
    try:
        return runtime.end_of_day.reconcile_from_provider(
            runtime.statement_provider,
            account=request.account,
            period_start=request.period_start,
            period_end=request.period_end,
            actor=actor,
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
