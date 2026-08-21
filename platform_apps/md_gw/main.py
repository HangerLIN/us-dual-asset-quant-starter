from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel

from platform_apps.common import require_service_role
from platform_core.schemas import InstrumentRef
from platform_core.sdk.models import BrokerSessionState
from platform_core.sdk.runtime import get_read_only_runtime

app = FastAPI(title="Market Data Gateway", version="1.0")


class MarketRuleRequest(BaseModel):
    market_rule_id: int


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "md_gw"}


@app.get("/readyz", dependencies=[Depends(require_service_role("read"))])
def readyz() -> dict[str, str | int | bool | None]:
    broker = get_read_only_runtime("md").broker
    connected = broker.session_state in {
        BrokerSessionState.RECOVERING,
        BrokerSessionState.RECONCILING,
        BrokerSessionState.READY,
    }
    payload = {
        "ready": (
            connected
            and broker.market_data_type is not None
            and broker.market_data_farm_healthy is not False
        ),
        "broker_state": broker.session_state.value,
        "market_data_type": broker.market_data_type,
        "market_data_farm_healthy": broker.market_data_farm_healthy,
    }
    if not payload["ready"]:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=payload)
    return payload


@app.post("/v1/session/start", dependencies=[Depends(require_service_role("read"))])
def start_session() -> dict[str, str]:
    broker = get_read_only_runtime("md").broker
    try:
        broker.connect()
        return {"status": broker.session_state.value}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.post("/v1/quotes/snapshot", dependencies=[Depends(require_service_role("read"))])
def snapshot(instrument: InstrumentRef):
    try:
        return get_read_only_runtime("md").broker.snapshot_quote(instrument)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.post("/v1/contracts/qualify", dependencies=[Depends(require_service_role("read"))])
def qualify(instrument: InstrumentRef):
    try:
        return get_read_only_runtime("md").broker.qualify_contract(instrument)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@app.post("/v1/contracts/market-rule", dependencies=[Depends(require_service_role("read"))])
def market_rule(request: MarketRuleRequest):
    try:
        return get_read_only_runtime("md").broker.market_rule(request.market_rule_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.get("/v1/capabilities", dependencies=[Depends(require_service_role("read"))])
def capabilities():
    try:
        return get_read_only_runtime("md").broker.capabilities()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
