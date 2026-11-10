"""FastAPI entrypoint for AlphaTrade AI v2.0."""

from fastapi import FastAPI
from app.api.routes import router
from app.core.execution_guard import assert_paper_mode

assert_paper_mode()

app = FastAPI(
    title="AlphaTrade AI v2.0 — NSE Intraday ORB Research & Paper Trading Engine",
    version="2.0.0",
    description="Deterministic quantitative research and paper-trading engine for NSE India cash equities.",
)
app.include_router(router)
