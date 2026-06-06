from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Signal Service")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "signal_svc"}
