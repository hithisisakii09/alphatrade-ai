"""Read-only FastAPI endpoints implementing all 11 specification routes."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
import os
from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse
from app.core.clock import NSECalendar, to_ist
from app.core.config import load_app_config
from app.core.money import to_decimal, round_paise
from app.core.database import (
    BacktestTradeRecord,
    DataQualityEventRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
    PositionRecord,
    TradeSignalRecord,
    init_db,
)
from app.execution.costs import NSECostEngine
from app.execution.gateway import (
    MockAngelSmartAPIGateway,
    PaperBrokerGateway,
    SmartOrderRouter,
)
from app.execution.impact import ExecutionAnalyticsEngine
from app.execution.shadow_reconciler import RealTimeShadowReconciler
from app.execution.algo_slicer import (
    AlgoExecutionCoordinator,
    ParentOrderRequest,
    TWAPExecutionEngine,
    DynamicVWAPExecutionEngine,
)
from app.execution.tca import (
    ExecutionSliceFill,
    compute_implementation_shortfall,
)
from app.market.microstructure import (
    MicrostructureLiquidityGate,
    OrderBookDepth,
    OrderBookLevel,
    analyze_order_book,
)
from app.market.regime import VolatilityRegimeClassifier
from app.portfolio.ledger import PortfolioLedger
from app.reporting.analytics import compute_trade_analytics, generate_trade_journal_csv
from app.reporting.reports import generate_daily_report, list_backtest_summaries
from app.research.ablation import run_augmented_factor_ablation_from_db
from app.research.monte_carlo import run_monte_carlo_resampling
from app.research.optimizer import run_hyperparameter_optimization
from app.surveillance.drift import BaselineDistribution, StatisticalDriftEngine
from app.risk.allocation import RiskParityAllocator
from app.risk.margin_stress import IntradayMarginStressEngine
from app.risk.engine import RiskEngine
from app.risk.var import PortfolioVaREngine
from app.services.daily_runner import ScheduledDailyRunner

router = APIRouter()

_cfg = load_app_config()
_engine, _SessionLocal = init_db(_cfg.database_url)
_calendar = NSECalendar()
_daily_runner = ScheduledDailyRunner(config=_cfg, auto_start=True)

_sor = SmartOrderRouter(default_gateway="PAPER_INTERNAL")
_paper_gw = PaperBrokerGateway(name="PAPER_INTERNAL")
_mock_angel_gw = MockAngelSmartAPIGateway(name="ANGEL_SMARTAPI_MOCK")
_sor.register_gateway(_paper_gw)
_sor.register_gateway(_mock_angel_gw)
_shadow_reconciler = RealTimeShadowReconciler(gateway=_paper_gw)
_algo_coordinator = AlgoExecutionCoordinator()

# Pre-populate sample institutional TWAP parent order for live status monitoring
_now_utc = datetime.now(timezone.utc)
_sample_twap_parent = ParentOrderRequest(
    parent_order_id="ALGO_TWAP_RELIANCE_001",
    symbol="RELIANCE",
    side="BUY",
    total_quantity=500,
    start_time_utc=_now_utc,
    end_time_utc=_now_utc + timedelta(minutes=15),
    algo_type="TWAP",
    num_slices=5,
    arrival_price=Decimal("2850.00"),
    decision_price=Decimal("2848.50"),
)
_sample_twap_engine = TWAPExecutionEngine(_sample_twap_parent)
# Simulate 2 executed slices
_sample_twap_engine.record_slice_fill(
    slice_id="ALGO_TWAP_RELIANCE_001_S01",
    filled_quantity=100,
    average_price=Decimal("2851.20"),
    broker_order_id="PB_ORD_SAMPLE_01",
    timestamp_utc=_now_utc,
    fee_inr=Decimal("4.00"),
)
_sample_twap_engine.record_slice_fill(
    slice_id="ALGO_TWAP_RELIANCE_001_S02",
    filled_quantity=100,
    average_price=Decimal("2851.80"),
    broker_order_id="PB_ORD_SAMPLE_02",
    timestamp_utc=_now_utc + timedelta(minutes=3),
    fee_inr=Decimal("4.00"),
)
_algo_coordinator.register_algorithm(_sample_twap_engine)


@router.get("/health")
def get_health():
  watchdog_health = _daily_runner.watchdog.check_health()
  return {
      "status": "OK" if watchdog_health["is_healthy"] else "DEGRADED",
      "version": "2.0.0",
      "execution_mode": _cfg.execution_mode,
      "active_risk_profile": _cfg.active_risk_profile,
      "watchdog": watchdog_health,
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
    cash_bal = ledger.get_cash_balance()
    deployed = ledger.get_deployed_exposure()
    unrealized = ledger.get_daily_unrealized_pnl()
    net_equity = cash_bal + deployed + unrealized
    return {
        "starting_capital_inr": str(ledger.starting_capital_inr),
        "cash_balance_inr": str(cash_bal),
        "deployed_exposure_inr": str(deployed),
        "unrealized_pnl_inr": str(unrealized),
        "net_equity_inr": str(net_equity),
        "reconciled": reconciled,
        "reconciliation_message": msg,
        "candle_cache": _daily_runner.candle_cache.get_cache_stats(),
    }


@router.get("/portfolio/snapshots")
def get_portfolio_snapshots(limit: int = 150):
  with _SessionLocal() as db:
    snaps = (
        db.query(PortfolioSnapshotRecord)
        .order_by(PortfolioSnapshotRecord.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "snapshots": [
            {
                "id": s.id,
                "timestamp_ist": to_ist(s.timestamp_utc).strftime("%H:%M:%S") if s.timestamp_utc else "",
                "timestamp_utc": s.timestamp_utc.isoformat() if s.timestamp_utc else None,
                "net_equity_inr": str(s.net_equity_inr),
                "cash_balance_inr": str(s.cash_balance_inr),
                "deployed_exposure_inr": str(s.deployed_exposure_inr),
                "realized_pnl_inr": str(s.realized_pnl_inr),
                "unrealized_pnl_inr": str(s.unrealized_pnl_inr),
                "total_fees_inr": str(s.total_fees_inr),
            }
            for s in reversed(snaps)
        ]
    }


@router.get("/reports/analytics")
def get_trade_analytics_endpoint(session_date: str | None = None):
  with _SessionLocal() as db:
    return compute_trade_analytics(db, session_date=session_date)


@router.get("/reports/journal")
def get_trade_journal_endpoint(session_date: str | None = None):
  with _SessionLocal() as db:
    analytics = compute_trade_analytics(db, session_date=session_date)
    return {
        "summary": {
            "total_trades": analytics["total_trades"],
            "win_rate_pct": analytics["win_rate_pct"],
            "profit_factor": analytics["profit_factor"],
            "payoff_ratio": analytics["payoff_ratio"],
            "avg_mfe_r": analytics["avg_mfe_r"],
            "avg_mae_r": analytics["avg_mae_r"],
            "capture_efficiency_pct": analytics["capture_efficiency_pct"],
        },
        "journal": analytics["trades"],
    }


@router.get("/reports/journal.csv")
def get_trade_journal_csv_endpoint(session_date: str | None = None):
  with _SessionLocal() as db:
    csv_data = generate_trade_journal_csv(db, session_date=session_date)
    fn = f"alphatrade_journal_{session_date}.csv" if session_date else "alphatrade_journal_all.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fn}"},
    )


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


@router.post("/backtest/run")
def trigger_backtest_run(symbols: str | None = None, dataset_id: str = "WEB_DASHBOARD_RUN"):
  from scripts.run_live_backtest import run_backtest_from_db
  sym_list = (
      [s.strip().upper() for s in symbols.split(",") if s.strip()]
      if symbols
      else ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
  )
  summary = run_backtest_from_db(symbols=sym_list, dataset_id=dataset_id, db_url=_cfg.database_url)
  if not summary:
    return {"status": "NO_DATA", "message": "No historical candle bars found in database."}
  return {
      "status": "SUCCESS",
      "run_id": summary.run_id,
      "total_trades": summary.total_trades,
      "win_rate_pct": str(summary.win_rate_pct),
      "profit_factor": str(summary.profit_factor),
      "net_expectancy_inr": str(summary.net_expectancy_inr),
      "net_expectancy_r": str(summary.net_expectancy_r),
      "max_drawdown_pct": str(summary.max_drawdown_pct),
      "evidence_classification": summary.evidence_classification,
  }


@router.get("/market/candles-summary")
def get_market_candles_summary():
  with _SessionLocal() as db:
    cur = db.conn.cursor()
    total_candles = cur.execute("SELECT count(*) FROM market_candles").fetchone()[0]
    by_symbol_rows = cur.execute(
        "SELECT symbol, count(*) FROM market_candles GROUP BY symbol ORDER BY count(*) DESC"
    ).fetchall()
    return {
        "total_candles": total_candles,
        "symbols_count": len(by_symbol_rows),
        "by_symbol": [{"symbol": r[0], "bars_count": r[1]} for r in by_symbol_rows],
    }


@router.get("/runner/status")
def get_runner_status():
  has_poller = any(j["id"] == "nse_intraday_tick" for j in _daily_runner.get_jobs_summary())
  return {
      "is_running": _daily_runner.is_running,
      "poller_enabled": has_poller,
      "scheduler_timezone": "Asia/Kolkata",
      "jobs_count": len(_daily_runner.get_jobs_summary()),
      "jobs": _daily_runner.get_jobs_summary(),
      "telemetry": _daily_runner.get_telemetry_summary(),
      "watchdog": _daily_runner.watchdog.check_health(),
  }


@router.post("/runner/start")
def start_runner():
  _daily_runner.start()
  return {"status": "STARTED", "is_running": _daily_runner.is_running}


@router.post("/runner/stop")
def stop_runner():
  _daily_runner.stop()
  return {"status": "STOPPED", "is_running": _daily_runner.is_running}


@router.post("/runner/poller/enable")
def enable_runner_poller():
  _daily_runner.enable_intraday_tick_job()
  return {
      "status": "POLLER_ENABLED",
      "jobs_count": len(_daily_runner.get_jobs_summary()),
      "is_running": _daily_runner.is_running,
  }


@router.post("/runner/poller/disable")
def disable_runner_poller():
  _daily_runner.disable_intraday_tick_job()
  return {
      "status": "POLLER_DISABLED",
      "jobs_count": len(_daily_runner.get_jobs_summary()),
      "is_running": _daily_runner.is_running,
  }


@router.post("/runner/tick")
def trigger_runner_tick(symbols: str | None = None):
  sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else None
  summary = _daily_runner.job_intraday_tick(symbols=sym_list)
  return {"status": "SUCCESS", "summary": summary}


@router.get("/research/monte-carlo")
def get_monte_carlo_endpoint(
    source: str = "ALL",
    iterations: int = 2000,
    ruin_drawdown_pct: float = 0.10,
    seed: int = 42,
):
  from app.core.money import to_decimal, round_paise
  with _SessionLocal() as db:
    pnls: list[Decimal] = []
    if source.upper() in ("LIVE", "ALL"):
      positions = db.query(PositionRecord).filter(PositionRecord.status == "CLOSED").all()
      pnls.extend(to_decimal(p.realized_pnl_inr) for p in positions)
    if source.upper() in ("BACKTEST", "ALL"):
      bt_trades = db.query(BacktestTradeRecord).all()
      pnls.extend(to_decimal(t.net_pnl_inr) for t in bt_trades)

    prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
    res = run_monte_carlo_resampling(
        trade_pnls=pnls,
        starting_capital_inr=prof.reference_capital,
        iterations=iterations,
        ruin_drawdown_pct=Decimal(str(ruin_drawdown_pct)),
        seed=seed,
    )
    return res.to_dict()


@router.get("/risk/var")
def get_portfolio_var_endpoint(
    symbols: str | None = None,
    lookback_bars: int = 1500,
):
  from app.core.money import to_decimal, round_paise
  prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
  with _SessionLocal() as db:
    ledger = PortfolioLedger(db, prof.reference_capital)
    cash_bal = ledger.get_cash_balance()
    deployed = ledger.get_deployed_exposure()
    unrealized = ledger.get_daily_unrealized_pnl()
    total_eq = cash_bal + deployed + unrealized
    holdings: dict[str, Decimal] = {}

    if symbols:
      sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
      equal_weight = total_eq / Decimal(len(sym_list)) if sym_list else Decimal("0")
      for s in sym_list:
        holdings[s] = equal_weight
    else:
      open_positions = db.query(PositionRecord).filter(PositionRecord.status == "OPEN").all()
      for p in open_positions:
        notional = to_decimal(p.quantity) * to_decimal(p.entry_price)
        holdings[p.symbol] = holdings.get(p.symbol, Decimal("0")) + notional

    db_file = _cfg.database_url.replace("sqlite:///", "")
    engine = PortfolioVaREngine(db_file)
    report = engine.compute_portfolio_risk(
        holdings=holdings,
        total_equity_inr=total_eq,
        lookback_bars=lookback_bars,
    )

    return {
        "timestamp_utc": report.timestamp_utc.isoformat(),
        "total_equity_inr": str(report.total_equity_inr),
        "total_exposure_inr": str(report.total_exposure_inr),
        "holdings_count": report.metrics.holdings_count,
        "metrics": {
            "portfolio_volatility_daily_pct": str(report.metrics.portfolio_volatility_daily_pct),
            "var_95_pct": str(report.metrics.var_95_pct),
            "var_95_inr": str(report.metrics.var_95_inr),
            "var_99_pct": str(report.metrics.var_99_pct),
            "var_99_inr": str(report.metrics.var_99_inr),
            "cvar_95_pct": str(report.metrics.cvar_95_pct),
            "cvar_95_inr": str(report.metrics.cvar_95_inr),
            "cvar_99_pct": str(report.metrics.cvar_99_pct),
            "cvar_99_inr": str(report.metrics.cvar_99_inr),
            "cf_var_95_pct": str(report.metrics.cf_var_95_pct),
            "cf_var_95_inr": str(report.metrics.cf_var_95_inr),
            "cf_var_99_pct": str(report.metrics.cf_var_99_pct),
            "cf_var_99_inr": str(report.metrics.cf_var_99_inr),
            "undiversified_var_95_inr": str(report.metrics.undiversified_var_95_inr),
            "diversification_benefit_pct": str(report.metrics.diversification_benefit_pct),
            "portfolio_skewness": str(report.metrics.portfolio_skewness),
            "portfolio_excess_kurtosis": str(report.metrics.portfolio_excess_kurtosis),
        },
        "weights": {s: str(w) for s, w in report.weights.items()},
        "individual_volatilities_daily_pct": {
            s: str(v) for s, v in report.individual_volatilities_daily_pct.items()
        },
        "correlation_matrix": {
            s1: {s2: str(c) for s2, c in inner.items()}
            for s1, inner in report.correlation_matrix.items()
        },
    }


@router.get("/execution/impact")
def get_execution_impact_endpoint(
    symbol: str | None = None,
    side: str = "BUY",
    quantity: int = 100,
    price: float | None = None,
):
  db_file = _cfg.database_url.replace("sqlite:///", "")
  engine = ExecutionAnalyticsEngine(db_file)
  summary = engine.analyze_historical_executions()

  sim_breakdown = None
  if symbol:
    ref_p = Decimal(str(price)) if price is not None else Decimal("1000.00")
    b = engine.compute_order_market_impact(
        symbol=symbol.upper(),
        side=side,
        quantity=quantity,
        raw_price=ref_p,
    )
    sim_breakdown = {
        "symbol": b.symbol,
        "side": b.side,
        "quantity": b.quantity,
        "raw_arrival_price": str(b.raw_arrival_price),
        "reference_volume": b.reference_volume,
        "participation_rate_pct": str(b.participation_rate_pct),
        "daily_volatility_pct": str(b.daily_volatility_pct),
        "temporary_impact_pct": str(b.temporary_impact_pct),
        "temporary_impact_inr": str(b.temporary_impact_inr),
        "permanent_impact_pct": str(b.permanent_impact_pct),
        "permanent_impact_inr": str(b.permanent_impact_inr),
        "total_impact_bps": str(b.total_impact_bps),
        "effective_execution_price": str(b.effective_execution_price),
        "implementation_shortfall_inr": str(b.implementation_shortfall_inr),
        "is_participation_compliant": b.is_participation_compliant,
        "max_allowed_participation_pct": str(b.max_allowed_participation_pct),
    }

  return {
      "summary": {
          "total_orders_analyzed": summary.total_orders_analyzed,
          "avg_slippage_bps": str(summary.avg_slippage_bps),
          "total_implementation_shortfall_inr": str(summary.total_implementation_shortfall_inr),
          "avg_participation_rate_pct": str(summary.avg_participation_rate_pct),
          "total_fees_and_impact_inr": str(summary.total_fees_and_impact_inr),
      },
      "by_symbol": summary.by_symbol,
      "simulation": sim_breakdown,
  }


@router.get("/execution/gateway/status")
def get_execution_gateway_status_endpoint():
  """
  M24 — Multi-Broker Execution Gateway & Smart Order Router (SOR):
  Returns registered gateways, connection status, protocols, and latency diagnostics.
  """
  statuses = [gw.get_status() for gw in _sor.gateways.values()]
  return {
      "default_gateway": _sor.default_gateway,
      "gateways_count": len(_sor.gateways),
      "gateways": [
          {
              "name": s.gateway_name,
              "protocol": s.protocol,
              "connected": s.connected,
              "latency_ms": s.latency_ms,
              "last_heartbeat_utc": s.last_heartbeat_utc.isoformat(),
          }
          for s in statuses
      ],
  }


@router.get("/execution/reconciliation/status")
def get_execution_reconciliation_status_endpoint():
  """
  M24 — Real-Time Shadow Reconciler:
  Performs real-time state reconciliation between internal ledger and external broker books.
  """
  with _SessionLocal() as session:
    open_positions = session.query(PositionRecord).filter(PositionRecord.status == "OPEN").all()
    internal_pos_map: dict[str, tuple[int, Decimal]] = {
        p.symbol: (p.quantity, to_decimal(p.entry_price)) for p in open_positions
    }
    prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
    ledger = PortfolioLedger(session, prof.reference_capital)
    internal_margin = round_paise(ledger.get_deployed_exposure() * Decimal("0.20"))

  report = _shadow_reconciler.reconcile(
      explicit_internal_positions=internal_pos_map,
      explicit_internal_margin=internal_margin,
  )
  return report.to_dict()


@router.get("/execution/algo/status")
def get_execution_algo_status_endpoint():
  """
  M25 — Algorithmic Execution Slicer (TWAP / Dynamic VWAP):
  Returns registered algorithmic parent orders, slice execution schedules, and completion states.
  """
  algos = [algo.to_dict() for algo in _algo_coordinator.active_algorithms.values()]
  total_active = sum(1 for a in algos if a["status"] == "ACTIVE")
  total_completed = sum(1 for a in algos if a["status"] == "COMPLETED")
  return {
      "active_algorithms_count": total_active,
      "completed_algorithms_count": total_completed,
      "algorithms": algos,
      "timestamp_utc": datetime.now(timezone.utc).isoformat(),
  }


@router.get("/execution/tca/summary")
def get_execution_tca_summary_endpoint():
  """
  M25 — Post-Trade TCA Implementation Shortfall (IS) Attribution:
  Returns Perold (1988) implementation shortfall breakdowns, market impact, delay, and benchmark shortfalls.
  """
  tca_reports = []
  for algo in _algo_coordinator.active_algorithms.values():
    if algo.fills:
      rep = algo.compute_tca(
          final_market_price=algo.parent.arrival_price,
          interval_vwap=algo.parent.arrival_price,
          interval_twap=algo.parent.arrival_price,
      )
      tca_reports.append(rep.to_dict())

  return {
      "total_tca_reports": len(tca_reports),
      "reports": tca_reports,
      "timestamp_utc": datetime.now(timezone.utc).isoformat(),
  }


@router.get("/market/regime")
def get_market_regime(symbol: str = "RELIANCE", lookback_bars: int = 60):
  """
  M19 — HMM Volatility Regime: classify current market regime from recent 1-min candle bars.
  Returns BULL / NEUTRAL / BEAR with posterior probabilities and confidence.
  """
  with _SessionLocal() as session:
    cur = session.conn.cursor()
    cur.execute(
        "SELECT open, high, low, close, volume FROM market_candles "
        "WHERE symbol = ? AND interval_minutes = 1 "
        "ORDER BY timestamp_utc DESC LIMIT ?",
        (symbol, lookback_bars),
    )
    rows = cur.fetchall()

  if not rows:
    return {
        "symbol": symbol,
        "regime": "NEUTRAL",
        "confidence": 0.333,
        "bull_prob": 0.333,
        "neutral_prob": 0.334,
        "bear_prob": 0.333,
        "n_bars_used": 0,
        "log_likelihood": None,
        "regime_distribution": {"BULL": 0.0, "NEUTRAL": 1.0, "BEAR": 0.0},
        "message": "Insufficient data — defaulting to NEUTRAL.",
    }

  # Rows are newest-first → reverse to chronological
  rows = list(reversed(rows))
  closes = [float(r[3]) for r in rows]
  highs  = [float(r[1]) for r in rows]
  lows   = [float(r[2]) for r in rows]

  clf = VolatilityRegimeClassifier(min_bars=20)
  clf.fit(closes, highs, lows)
  state = clf.predict_current()
  dist  = clf.regime_distribution()

  return {
      "symbol": symbol,
      "lookback_bars": len(rows),
      "regime": state.regime.value,
      "confidence": round(state.confidence, 4),
      "bull_prob": round(state.bull_prob, 4),
      "neutral_prob": round(state.neutral_prob, 4),
      "bear_prob": round(state.bear_prob, 4),
      "n_bars_used": state.n_bars_used,
      "log_likelihood": round(state.log_likelihood, 4) if state.log_likelihood != float("-inf") else None,
      "regime_distribution": {k: round(v, 4) for k, v in dist.items()},
      "hmm_trained": clf.is_trained,
  }


@router.get("/research/augmented-study")
def get_augmented_study_endpoint(
    symbols: str = "RELIANCE,SBIN,TCS",
    bars_limit: int | None = 500,
):
  """
  M20 — Augmented Multi-Month Walk-Forward & Factor Ablation Study:
  Evaluates 5-tier factor hierarchy across authentic historical candle data.
  """
  target_syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
  res = run_augmented_factor_ablation_from_db(
      db_url=_cfg.database_url,
      symbols=target_syms,
      max_bars_per_symbol=bars_limit,
  )
  return res.to_dict()


@router.get("/research/hyperparameter-optimization")
def get_hyperparameter_optimization_endpoint(
    symbols: str = "RELIANCE,SBIN,TCS",
    bars_limit: int | None = 300,
    coarse_evals: int = 20,
    bayes_iters: int = 6,
):
  """
  M21 — Adaptive Signal Calibration & Bayesian Hyperparameter Optimizer:
  Runs a 2-stage coarse grid + GP-UCB search over the ORB strategy parameter space.
  Returns ranked top-10 configurations with composite scores, expectancy CIs,
  and improvement vs the M9/M20 baselines.
  """
  target_syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
  res = run_hyperparameter_optimization(
      db_url=_cfg.database_url,
      symbols=target_syms,
      bars_limit=bars_limit,
      max_coarse_evals=coarse_evals,
      bayesian_iterations=bayes_iters,
  )
  return res.to_dict()


@router.get("/surveillance/drift")
def get_strategy_drift_endpoint(
    lookback_trades: int = 100,
    baseline_profile: str = "M21",
):
  """
  M22 — Statistical Drift Surveillance & Concept Shift:
  Evaluates recent closed trades against the baseline distribution via
  Two-Sample Kolmogorov-Smirnov (KS) test and CUSUM change-point detection.
  """
  baseline = (
      BaselineDistribution.calibrated_m21_baseline()
      if baseline_profile.upper() == "M21"
      else BaselineDistribution.default_m20_baseline()
  )
  engine = StatisticalDriftEngine(baseline=baseline)

  with _SessionLocal() as session:
    cur = session.conn.cursor()
    cur.execute(
        "SELECT r_multiple_net FROM backtest_trades ORDER BY id DESC LIMIT ?",
        (lookback_trades,),
    )
    rows = cur.fetchall()

  r_multiples = [to_decimal(str(r[0])) for r in reversed(rows)] if rows else []
  report = engine.analyze_trades(r_multiples)
  return report.to_dict()


@router.get("/risk/allocation")
def get_risk_allocation_endpoint(
    symbols: str = "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK,SBIN",
    method: str = "EQUAL_RISK_CONTRIBUTION",
    lookback_bars: int = 1500,
):
  """
  M23 — Dynamic Capital Allocation & Risk Parity:
  Computes Equal Risk Contribution (ERC) weights, asset volatilities,
  marginal risk contributions, and portfolio diversification ratio.
  """
  sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
  db_file = _cfg.database_url.replace("sqlite:///", "")
  allocator = RiskParityAllocator(db_path=db_file)

  with _SessionLocal() as session:
    cur = session.conn.cursor()
    prices: dict[str, Decimal] = {}
    for s in sym_list:
      cur.execute(
          "SELECT close FROM market_candles WHERE symbol = ? ORDER BY timestamp_utc DESC LIMIT 1",
          (s,),
      )
      row = cur.fetchone()
      prices[s] = to_decimal(str(row[0])) if row else Decimal("1000.00")

    prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
    ledger = PortfolioLedger(session, prof.reference_capital)
    cash_bal = ledger.get_cash_balance()
    deployed = ledger.get_deployed_exposure()
    unrealized = ledger.get_daily_unrealized_pnl()
    total_eq = cash_bal + deployed + unrealized

  rep = allocator.allocate(
      symbols=sym_list,
      prices=prices,
      total_capital_inr=total_eq if total_eq > Decimal("0") else prof.reference_capital,
      deployable_pct=prof.max_capital_deployed_pct,
      method=method.upper(),
      lookback_bars=lookback_bars,
  )
  return rep.to_dict()


@router.get("/risk/margin-stress")
def get_margin_stress_endpoint(
    symbols: str | None = None,
    candidate_symbol: str | None = None,
    candidate_qty: int = 0,
    candidate_price: float | None = None,
):
  """
  M23 — Pre-Trade Intraday Margin Stress & Headroom:
  Evaluates portfolio margin utilization under joint instantaneous shocks:
  -2.5% market gap drop, +50% clearing house margin spike, and 20 bps liquidity haircut.
  """
  engine = IntradayMarginStressEngine()
  with _SessionLocal() as session:
    prof = _cfg.strategy.risk_profiles[_cfg.active_risk_profile]
    ledger = PortfolioLedger(session, prof.reference_capital)
    cash_bal = ledger.get_cash_balance()
    deployed = ledger.get_deployed_exposure()
    unrealized = ledger.get_daily_unrealized_pnl()
    total_eq = cash_bal + deployed + unrealized

    open_positions: dict[str, tuple[int, Decimal]] = {}
    db_positions = session.query(PositionRecord).filter(PositionRecord.status == "OPEN").all()
    for p in db_positions:
      open_positions[p.symbol] = (p.quantity, to_decimal(p.entry_price))

    if not open_positions and symbols:
      sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
      cur = session.conn.cursor()
      per_sym_notional = (total_eq * Decimal("0.80")) / Decimal(len(sym_list)) if sym_list else Decimal("0")
      for s in sym_list:
        cur.execute(
            "SELECT close FROM market_candles WHERE symbol = ? ORDER BY timestamp_utc DESC LIMIT 1",
            (s,),
        )
        row = cur.fetchone()
        p_val = to_decimal(str(row[0])) if row else Decimal("1000.00")
        qty = int((per_sym_notional / p_val).to_integral_value(rounding=ROUND_DOWN)) if p_val > Decimal("0") else 1
        open_positions[s] = (max(1, qty), p_val)

  report = engine.evaluate_stress(
      open_positions=open_positions,
      candidate_symbol=candidate_symbol.upper() if candidate_symbol else None,
      candidate_qty=candidate_qty,
      candidate_price=Decimal(str(candidate_price)) if candidate_price is not None else Decimal("0.00"),
      total_equity_inr=total_eq if total_eq > Decimal("0") else prof.reference_capital,
      available_cash_inr=cash_bal if cash_bal > Decimal("0") else prof.reference_capital,
  )
  return report.to_dict()


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
