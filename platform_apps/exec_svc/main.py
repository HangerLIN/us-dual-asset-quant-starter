from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from time import monotonic, sleep
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from platform_apps.common import (
    ServiceIdentity,
    require_service_role,
    require_strategy_access,
    verified_actor,
)
from platform_core.schemas import InstrumentRef
from platform_core.sdk.lifecycle import OptionLifecycleSDK, OrderSupervisorSDK
from platform_core.sdk.models import (
    BracketOrderIntent,
    BrokerSessionState,
    DefinedRiskOptionComboIntent,
    LiveOrderIntent,
    OCAOrderIntentGroup,
    OrderCancelCommand,
    OrderReplaceCommand,
    StrategyOrderEventPage,
)
from platform_core.sdk.runtime import TradingRuntime, get_execution_service_runtime

_runtime_instance: TradingRuntime | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    if _runtime_instance is None:
        return
    _runtime_instance.session_supervisor.stop()
    _runtime_instance.execution.release_execution_lease()
    _runtime_instance.broker.disconnect()


app = FastAPI(title="Execution Service", version="1.0", lifespan=lifespan)


class AccountRequest(BaseModel):
    account: str | None = None


class ArmLiveRequest(BaseModel):
    account: str
    confirmation: str


class AdoptPositionsRequest(BaseModel):
    account: str
    actor: str | None = None
    confirmation: str


class KillRequest(BaseModel):
    account: str
    reason: str = Field(..., min_length=3)
    actor: str | None = None
    include_other_clients: bool = False
    confirmation: str | None = None


class ClearKillRequest(BaseModel):
    account: str
    actor: str | None = None
    confirmation: str


class ExpireRequest(BaseModel):
    account: str
    now: datetime | None = None


class FlattenRequest(BaseModel):
    account: str
    strategy_code: str
    operation_id: str = Field(..., min_length=8)
    confirmation: str
    ttl_seconds: int = Field(default=30, ge=5, le=300)


class OptionActionRequest(BaseModel):
    account: str
    instrument: InstrumentRef
    quantity: Decimal = Field(..., gt=0)
    action: Literal["EXERCISE", "LAPSE"]
    confirmation: str
    override: bool = False


def _runtime() -> TradingRuntime:
    global _runtime_instance
    if _runtime_instance is None:
        _runtime_instance = get_execution_service_runtime()
    return _runtime_instance


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "exec_svc"}


@app.get("/readyz", dependencies=[Depends(require_service_role("read"))])
def readyz() -> dict[str, str | bool | None]:
    runtime = _runtime()
    payload = runtime.execution.readiness()
    if not payload["ready"]:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)
    return payload


@app.get("/metrics", dependencies=[Depends(require_service_role("read"))])
def metrics() -> Response:
    runtime = _runtime()
    runtime.metrics.gauge(
        "trading_broker_ready",
        1.0 if runtime.broker.session_state == BrokerSessionState.READY else 0.0,
    )
    runtime.metrics.gauge(
        "trading_order_efficiency_ratio",
        runtime.execution.pacing.order_efficiency_ratio,
    )
    return Response(runtime.metrics.render(), media_type="text/plain; version=0.0.4")


@app.post("/v1/session/start", dependencies=[Depends(require_service_role("execution_operator"))])
def start_session(request: AccountRequest):
    try:
        runtime = _runtime()
        report = runtime.execution.start(account=request.account)
        runtime.session_supervisor.start(account=report.account)
        return report
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post(
    "/v1/session/reconcile",
    dependencies=[Depends(require_service_role("execution_operator"))],
)
def reconcile(request: AccountRequest):
    runtime = _runtime()
    try:
        account = runtime.broker.resolve_account(request.account)
        return runtime.execution.recover(account=account)
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post(
    "/v1/session/arm-live",
    dependencies=[Depends(require_service_role("execution_operator"))],
)
def arm_live(request: ArmLiveRequest) -> dict[str, str]:
    try:
        runtime = _runtime()
        readiness = runtime.execution.readiness()
        if not readiness["ready"]:
            raise PermissionError(f"execution is not ready to arm: {readiness}")
        armed_until = runtime.safety.arm_live(
            account=request.account, confirmation=request.confirmation
        )
        return {"armed_until": armed_until.isoformat()}
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/session/adopt-positions")
def adopt_positions(
    request: AdoptPositionsRequest,
    identity: ServiceIdentity = Depends(require_service_role("execution_operator")),
):
    actor = verified_actor(identity, request.actor)
    try:
        runtime = _runtime()
        report = runtime.execution.adopt_positions(
            account=request.account,
            actor=actor,
            confirmation=request.confirmation,
        )
        runtime.session_supervisor.start(account=report.account)
        return report
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/orders")
def submit_order(
    intent: LiveOrderIntent,
    identity: ServiceIdentity = Depends(require_service_role("order_submitter")),
    x_strategy_code: str | None = Header(default=None),
):
    try:
        return _runtime().execution.submit(_attributed_intent(intent, identity, x_strategy_code))
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/orders/bracket")
def submit_bracket(
    bracket: BracketOrderIntent,
    identity: ServiceIdentity = Depends(require_service_role("order_submitter")),
    x_strategy_code: str | None = Header(default=None),
):
    try:
        intents = _attributed_intents(
            [bracket.entry, bracket.take_profit, bracket.stop_loss],
            identity,
            x_strategy_code,
        )
        return _runtime().execution.submit_bracket(
            BracketOrderIntent(entry=intents[0], take_profit=intents[1], stop_loss=intents[2])
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/orders/oca")
def submit_oca(
    group: OCAOrderIntentGroup,
    identity: ServiceIdentity = Depends(require_service_role("order_submitter")),
    x_strategy_code: str | None = Header(default=None),
):
    try:
        return _runtime().execution.submit_oca(
            group.model_copy(
                update={"orders": _attributed_intents(group.orders, identity, x_strategy_code)}
            )
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/orders/combo")
def submit_combo(
    intent: DefinedRiskOptionComboIntent,
    identity: ServiceIdentity = Depends(require_service_role("order_submitter")),
    x_strategy_code: str | None = Header(default=None),
):
    try:
        require_strategy_access(
            identity, intent.strategy_code, header_strategy_code=x_strategy_code
        )
        metadata = _attributed_metadata(intent.metadata, identity)
        return _runtime().combos.submit(intent.model_copy(update={"metadata": metadata}))
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/orders/replace")
def replace_order(
    command: OrderReplaceCommand,
    identity: ServiceIdentity = Depends(require_service_role("order_submitter")),
    x_strategy_code: str | None = Header(default=None),
):
    try:
        runtime = _runtime()
        _require_order_strategy_access(runtime, command.client_order_id, identity, x_strategy_code)
        return runtime.execution.replace(command)
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/orders/cancel")
def cancel_order(
    command: OrderCancelCommand,
    identity: ServiceIdentity = Depends(require_service_role("order_submitter")),
    x_strategy_code: str | None = Header(default=None),
):
    try:
        runtime = _runtime()
        _require_order_strategy_access(runtime, command.client_order_id, identity, x_strategy_code)
        return runtime.execution.cancel(command)
    except Exception as exc:
        raise _api_error(exc) from exc


@app.get("/v1/orders/{client_order_id}")
def get_order(
    client_order_id: str,
    identity: ServiceIdentity = Depends(require_service_role("read")),
    x_strategy_code: str | None = Header(default=None),
) -> dict:
    row = _runtime().ledger.get(client_order_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order not found")
    require_strategy_access(identity, row.strategy_code, header_strategy_code=x_strategy_code)
    return {
        "client_order_id": row.client_order_id,
        "state": row.state,
        "account": row.account,
        "broker_order_id": row.broker_order_id,
        "permanent_id": row.permanent_id,
        "filled": str(row.filled),
        "remaining": str(row.remaining),
        "avg_fill_price": str(row.avg_fill_price),
        "revision": row.revision,
        "last_error": row.last_error,
        "updated_at": row.updated_at,
    }


@app.get("/v1/order-events")
def get_order_events(
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    wait_seconds: float = Query(default=0, ge=0, le=30),
    identity: ServiceIdentity = Depends(require_service_role("read")),
    x_strategy_code: str = Header(...),
) -> StrategyOrderEventPage:
    """使用可恢复事件游标长轮询持久化经纪商日志。"""

    strategy_code = require_strategy_access(
        identity,
        x_strategy_code,
        header_strategy_code=x_strategy_code,
    )
    deadline = monotonic() + wait_seconds
    while True:
        events = _runtime().ledger.strategy_order_events(
            strategy_code,
            after_event_id=after_event_id,
            limit=limit,
        )
        if events or monotonic() >= deadline:
            return StrategyOrderEventPage(
                events=events,
                next_event_id=events[-1].event_id if events else after_event_id,
            )
        # 每次查询都重新鉴权后的同一策略视图；短睡眠避免空轮询压垮数据库。
        sleep(min(0.25, max(0, deadline - monotonic())))


@app.post(
    "/v1/orders/expire",
    dependencies=[Depends(require_service_role("execution_operator"))],
)
def expire_orders(request: ExpireRequest):
    runtime = _runtime()
    supervisor = OrderSupervisorSDK(
        execution=runtime.execution, ledger=runtime.ledger, safety=runtime.safety
    )
    try:
        return supervisor.expire_due_orders(account=request.account, now=request.now)
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post(
    "/v1/account/flatten",
    dependencies=[Depends(require_service_role("execution_operator"))],
)
def flatten_account(request: FlattenRequest):
    runtime = _runtime()
    supervisor = OrderSupervisorSDK(
        execution=runtime.execution, ledger=runtime.ledger, safety=runtime.safety
    )
    try:
        return supervisor.flatten_account(
            account=request.account,
            strategy_code=request.strategy_code,
            operation_id=request.operation_id,
            confirmation=request.confirmation,
            ttl_seconds=request.ttl_seconds,
        )
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post(
    "/v1/options/action",
    dependencies=[Depends(require_service_role("execution_operator"))],
)
def option_action(request: OptionActionRequest) -> dict[str, int]:
    runtime = _runtime()
    lifecycle = OptionLifecycleSDK(
        broker=runtime.broker,
        ledger=runtime.ledger,
        execution=runtime.execution,
    )
    try:
        request_id = lifecycle.request_exercise_or_lapse(
            account=request.account,
            instrument=request.instrument,
            quantity=request.quantity,
            action=request.action,
            confirmation=request.confirmation,
            override=request.override,
        )
        return {"request_id": request_id}
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/kill-switch")
def kill_switch(
    request: KillRequest,
    identity: ServiceIdentity = Depends(require_service_role("execution_operator")),
):
    actor = verified_actor(identity, request.actor)
    if request.include_other_clients:
        if "risk_operator" not in identity.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="global cancel also requires the risk_operator role",
            )
        if request.confirmation != f"GLOBAL-CANCEL:{request.account}":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="global cancel confirmation does not match account",
            )
    try:
        cancelled = _runtime().execution.kill(
            account=request.account,
            reason=request.reason,
            actor=actor,
            include_other_clients=request.include_other_clients,
        )
        return {"status": "KILLED", "cancelled": cancelled}
    except Exception as exc:
        raise _api_error(exc) from exc


@app.post("/v1/kill-switch/clear")
def clear_kill_switch(
    request: ClearKillRequest,
    identity: ServiceIdentity = Depends(require_service_role("execution_operator")),
) -> dict[str, str]:
    actor = verified_actor(identity, request.actor)
    try:
        _runtime().execution.clear_kill(
            account=request.account,
            actor=actor,
            confirmation=request.confirmation,
        )
        return {"status": "CLEARED_RECONCILIATION_REQUIRED"}
    except Exception as exc:
        raise _api_error(exc) from exc


def _api_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, (PermissionError, ValueError)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _attributed_intent(
    intent: LiveOrderIntent,
    identity: ServiceIdentity,
    header_strategy_code: str | None,
) -> LiveOrderIntent:
    require_strategy_access(
        identity,
        intent.strategy_code,
        header_strategy_code=header_strategy_code,
    )
    return intent.model_copy(update={"metadata": _attributed_metadata(intent.metadata, identity)})


def _attributed_intents(
    intents: list[LiveOrderIntent],
    identity: ServiceIdentity,
    header_strategy_code: str | None,
) -> list[LiveOrderIntent]:
    strategy_codes = {intent.strategy_code for intent in intents}
    if len(strategy_codes) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="linked order groups must belong to exactly one strategy",
        )
    return [_attributed_intent(intent, identity, header_strategy_code) for intent in intents]


def _attributed_metadata(
    metadata: dict,
    identity: ServiceIdentity,
) -> dict:
    existing_actor = metadata.get("authenticated_actor")
    if existing_actor is not None and existing_actor != identity.actor:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="authenticated_actor metadata cannot be supplied for another identity",
        )
    return {**metadata, "authenticated_actor": identity.actor}


def _require_order_strategy_access(
    runtime: TradingRuntime,
    client_order_id: str,
    identity: ServiceIdentity,
    header_strategy_code: str | None,
) -> None:
    row = runtime.ledger.get(client_order_id)
    if row is None:
        raise LookupError(f"unknown client_order_id {client_order_id!r}")
    require_strategy_access(
        identity,
        row.strategy_code,
        header_strategy_code=header_strategy_code,
    )
