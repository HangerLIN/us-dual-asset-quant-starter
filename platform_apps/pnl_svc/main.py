from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="PnL Service")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "pnl_svc"}
