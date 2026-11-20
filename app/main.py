"""FastAPI entrypoint for AlphaTrade AI v2.0."""

import uvicorn
from fastapi import FastAPI
from app.api.auth import router as auth_router
from app.api.routes import router
from app.core.execution_guard import assert_safe_execution_mode

assert_safe_execution_mode()

app = FastAPI(
    title="AlphaTrade AI v2.0 — NSE Intraday ORB Research & Paper Trading Engine",
    version="2.0.0",
    description="Deterministic quantitative research and paper-trading engine for NSE India cash equities.",
)
app.include_router(auth_router)
app.include_router(router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8080, reload=False)

