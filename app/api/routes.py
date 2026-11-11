"""Read-only FastAPI endpoints implementing all 11 specification routes."""

from datetime import datetime, timezone
import os
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from app.core.clock import NSECalendar
from app.core.config import load_app_config
from app.core.database import (
    DataQualityEventRecord,
    OrderRecord,
    PositionRecord,
    TradeSignalRecord,
    init_db,
)
from app.execution.costs import NSECostEngine
from app.portfolio.ledger import PortfolioLedger
from app.reporting.reports import generate_daily_report, list_backtest_summaries
from app.risk.engine import RiskEngine
from app.services.daily_runner import ScheduledDailyRunner

router = APIRouter()

_cfg = load_app_config()
_engine, _SessionLocal = init_db(_cfg.database_url)
_calendar = NSECalendar()
_daily_runner = ScheduledDailyRunner(config=_cfg, auto_start=True)


@router.get("/health")
def get_health():
  return {
      "status": "OK",
      "version": "2.0.0",
      "execution_mode": _cfg.execution_mode,
      "active_risk_profile": _cfg.active_risk_profile,
  }


@router.get("/market/status")
def get_market_status():
  now_utc = datetime.now(timezone.utc)
  return {
      "timestamp_utc": now_utc.isoformat(),
      "exchange": "NSE",
      "session_state": _calendar.get_session_state(now_utc),
      "calendar_version": _calendar.version,
  }


@router.get("/market/data-quality")
def get_market_data_quality():
  with _SessionLocal() as db:
    events = db.query(DataQualityEventRecord).order_by(DataQualityEventRecord.id.desc()).limit(50).all()
    return {
        "provider": _cfg.data_provider,
        "quality_events_count": len(events),
        "events": [
            {
                "symbol": e.symbol,
                "timestamp_utc": e.timestamp_utc.isoformat(),
                "event_type": e.event_type,
                "severity": e.severity,
                "details": e.details,
            }
            for e in events
        ],
    }


@router.get("/strategy/status")
def get_strategy_status():
  return {
      "strategy_id": _cfg.strategy.strategy_id,
      "version": _cfg.strategy.version,
      "direction": _cfg.strategy.direction,
      "opening_range_minutes": _cfg.strategy.opening_range_minutes,
      "signal_interval_minutes": _cfg.strategy.signal_interval_minutes,
      "min_rvol_threshold": str(_cfg.strategy.min_rvol_threshold),
      "entry_cutoff_ist": _cfg.strategy.entry_cutoff_ist,
  }


@router.get("/signals")
def get_signals():
  with _SessionLocal() as db:
    sigs = db.query(TradeSignalRecord).order_by(TradeSignalRecord.id.desc()).limit(100).all()
    return {
        "signals": [
            {
                "signal_id": s.signal_id,
                "symbol": s.symbol,
                "session_date": s.session_date,
                "signal_price": str(s.signal_price),
                "orb_high": str(s.orb_high),
                "orb_low": str(s.orb_low),
                "vwap": str(s.vwap),
                "rvol": str(s.rvol),
                "status": s.status,
            }
            for s in sigs
        ]
    }


@router.get("/risk/status")
def get_risk_status():
  prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
  with _SessionLocal() as db:
    ledger = PortfolioLedger(db, prof.reference_capital)
    reconciled, msg = ledger.reconcile()
    return {
        "active_risk_profile": _cfg.active_risk_profile,
        "exit_mode": prof.exit_mode,
        "initial_stop_pct": str(prof.initial_stop_pct),
        "trailing_activation_pct": str(prof.trailing_activation_pct),
        "trailing_drop_pct": str(prof.trailing_drop_pct),
        "max_cost_to_risk_ratio": str(prof.max_cost_to_risk_ratio),
        "max_open_positions": prof.max_open_positions,
        "open_positions_count": len(ledger.get_open_positions()),
        "deployed_exposure_inr": str(ledger.get_deployed_exposure()),
        "ledger_reconciled": reconciled,
        "reconciliation_status": msg,
        "kill_switch_active": not reconciled,
    }


@router.get("/orders")
def get_orders():
  with _SessionLocal() as db:
    orders = db.query(OrderRecord).order_by(OrderRecord.id.desc()).limit(100).all()
    return {
        "orders": [
            {
                "order_id": o.order_id,
                "idempotency_key": o.idempotency_key,
                "symbol": o.symbol,
                "side": o.side,
                "requested_qty": o.requested_qty,
                "filled_qty": o.filled_qty,
                "reference_price": str(o.reference_price),
                "status": o.status,
                "reason": o.reason,
            }
            for o in orders
        ]
    }


@router.get("/positions")
def get_positions():
  with _SessionLocal() as db:
    positions = db.query(PositionRecord).order_by(PositionRecord.id.desc()).limit(100).all()
    return {
        "positions": [
            {
                "position_id": p.position_id,
                "symbol": p.symbol,
                "session_date": p.session_date,
                "status": p.status,
                "quantity": p.quantity,
                "entry_price": str(p.entry_price),
                "exit_price": str(p.exit_price) if p.exit_price is not None else None,
                "initial_stop_price": str(p.initial_stop_price),
                "current_stop_price": str(p.current_stop_price),
                "trailing_activation_price": str(p.trailing_activation_price),
                "trailing_activated": p.trailing_activated,
                "highest_price_since_entry": str(p.highest_price_since_entry),
                "realized_pnl_inr": str(p.realized_pnl_inr),
                "exit_reason": p.exit_reason,
            }
            for p in positions
        ]
    }


@router.get("/portfolio")
def get_portfolio():
  prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
  with _SessionLocal() as db:
    ledger = PortfolioLedger(db, prof.reference_capital)
    reconciled, msg = ledger.reconcile()
    return {
        "starting_capital_inr": str(ledger.starting_capital_inr),
        "cash_balance_inr": str(ledger.get_cash_balance()),
        "deployed_exposure_inr": str(ledger.get_deployed_exposure()),
        "reconciled": reconciled,
        "reconciliation_message": msg,
    }


@router.get("/reports/daily")
def get_daily_report_endpoint(session_date: str = "2026-11-10"):
  prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
  with _SessionLocal() as db:
    ledger = PortfolioLedger(db, prof.reference_capital)
    return generate_daily_report(db, ledger, session_date)


@router.get("/backtests")
def get_backtests():
  with _SessionLocal() as db:
    return {"backtest_runs": list_backtest_summaries(db)}


@router.get("/runner/status")
def get_runner_status():
  return {
      "is_running": _daily_runner.is_running,
      "scheduler_timezone": "Asia/Kolkata",
      "jobs_count": len(_daily_runner.get_jobs_summary()),
      "jobs": _daily_runner.get_jobs_summary(),
  }


@router.post("/runner/start")
def start_runner():
  _daily_runner.start()
  return {"status": "STARTED", "is_running": _daily_runner.is_running}


@router.post("/runner/stop")
def stop_runner():
  _daily_runner.stop()
  return {"status": "STOPPED", "is_running": _daily_runner.is_running}


@router.post("/runner/tick")
def trigger_runner_tick(symbols: str | None = None):
  sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None
  summary = _daily_runner.job_intraday_tick(symbols=sym_list)
  return {"status": "SUCCESS", "summary": summary}


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
@router.get("/dashboard/", response_class=HTMLResponse)
def get_dashboard_html():
  base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
  dash_path = os.path.join(base, "dashboard/index.html")
  if os.path.exists(dash_path):
    with open(dash_path, "r") as f:
      return f.read()
  return "<h1>AlphaTrade AI v2.0 Read-Only Dashboard</h1>"
