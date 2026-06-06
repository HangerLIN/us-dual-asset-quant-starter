from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Market Data Gateway")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "md_gw"}
