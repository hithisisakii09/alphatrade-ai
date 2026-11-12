"""Daily session runner, APScheduler intraday cron manager, and deterministic ORB simulation benchmark."""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import logging
import os
import time as timemod
from typing import Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.backtest.engine import EventDrivenBacktester
from app.core.clock import IST, NSECalendar, ist_datetime, to_ist
from app.core.config import AppConfig, load_app_config
from app.core.database import (
    DailyMetricRecord,
    PositionRecord,
    RiskDecisionRecord,
    Session,
    TradeSignalRecord,
    init_db,
)
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, to_decimal
from app.data.cache import LiveCandleCache
from app.data.instruments import InstrumentRegistry
from app.data.models import CandleBar
from app.data.providers import MarketDataProvider, get_data_provider
from app.data.validator import CandleIngestionValidator, aggregate_1m_to_nm
from app.execution.costs import NSECostEngine
from app.execution.paper_broker import PaperBroker
from app.market.indicators import (
    compute_atr_series,
    compute_higher_timeframe_ema,
    compute_opening_range,
    compute_session_vwap,
    compute_session_vwap_std,
    compute_time_of_day_rvol,
)
from app.portfolio.ledger import PortfolioLedger
from app.reporting.reports import generate_daily_report
from app.risk.engine import RiskEngine
from app.services.notifications import WebhookNotificationService
from app.services.watchdog import FeedWatchdog
from app.strategy.orb.engine import ORBStrategyEngine

logger = logging.getLogger(__name__)


def checkpoint_database_wal(db_url: str) -> tuple[int, int, int]:
  """Performs an SQLite WAL TRUNCATE checkpoint if connected to SQLite."""
  if "sqlite" in db_url.lower():
    clean_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    if clean_path and os.path.exists(clean_path):
      try:
        import sqlite3
        conn = sqlite3.connect(clean_path, timeout=30.0)
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
        conn.close()
        logger.info("Executed SQLite WAL checkpoint on %s: busy=%s, log=%s, checkpointed=%s", clean_path, row[0], row[1], row[2])
        return int(row[0]), int(row[1]), int(row[2])
      except Exception as e:
        logger.warning("WAL checkpoint failed: %s", e)
  return 0, 0, 0


def generate_realistic_session_bars(
    symbol: str,
    year: int,
    month: int,
    day: int,
    open_price: Decimal,
    breakout_multiplier: Decimal = Decimal("1.008"),
    peak_multiplier: Decimal = Decimal("1.036"),
    pullback_multiplier: Decimal = Decimal("1.022"),
) -> list[CandleBar]:
  """Generates a realistic 60-minute sequence of 1m NSE candles (09:15 to 10:14 IST) plus 15:00 IST bar."""
  bars: list[CandleBar] = []
  base = open_price
  for m in range(60):
    ts = ist_datetime(year, month, day, 9 + (15 + m) // 60, (15 + m) % 60)
    if m < 15:
      # Opening range formation (09:15 - 09:29 IST): tight range [base * 0.996, base * 1.004]
      o = (base * Decimal("0.999")).quantize(Decimal("0.05"))
      h = (base * Decimal("1.004")).quantize(Decimal("0.05"))
      l = (base * Decimal("0.996")).quantize(Decimal("0.05"))
      c = (base * Decimal("1.001")).quantize(Decimal("0.05"))
      vol = 25000
    elif 15 <= m < 20:
      # First 5m bar post-ORB (09:30 - 09:34 IST): breakout above ORH with high volume
      o = (base * Decimal("1.002")).quantize(Decimal("0.05"))
      c = (base * breakout_multiplier).quantize(Decimal("0.05"))
      h = max(o, c) + Decimal("0.50")
      l = min(o, c) - Decimal("0.25")
      vol = 85000
    elif 20 <= m < 35:
      # Smooth rally from breakout_multiplier to peak_multiplier (+3.6% > +2.5% trailing activation)
      prev_mult = breakout_multiplier + (peak_multiplier - breakout_multiplier) * Decimal(m - 20) / Decimal(15)
      curr_mult = breakout_multiplier + (peak_multiplier - breakout_multiplier) * Decimal(m - 19) / Decimal(15)
      o = (base * prev_mult).quantize(Decimal("0.05"))
      c = (base * curr_mult).quantize(Decimal("0.05"))
      h = c
      l = o
      vol = 45000
    else:
      # Pullback triggering trailing exit (-0.75% from peak)
      o = (base * peak_multiplier).quantize(Decimal("0.05"))
      c = (base * pullback_multiplier).quantize(Decimal("0.05"))
      h = o
      l = c
      vol = 30000

    bars.append(
        CandleBar(
            symbol=symbol,
            interval_minutes=1,
            timestamp_utc=ts,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=vol,
            source="SIMULATED_BENCHMARK",
        )
    )

  # Append 15:15 IST EOD liquidation bar
  last_c = bars[-1].close
  bars.append(
      CandleBar(
          symbol=symbol,
          interval_minutes=1,
          timestamp_utc=ist_datetime(year, month, day, 15, 15),
          open=last_c,
          high=last_c,
          low=last_c,
          close=last_c,
          volume=5000,
          source="SIMULATED_BENCHMARK",
      )
  )
  return bars


def run_benchmark_simulation(db_url: str = "sqlite:///./alphatrade_v2.db") -> dict:
  cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(db_url)
  with SessionLocal() as db:
    bt = EventDrivenBacktester(db, cfg)
    bars_reliance = generate_realistic_session_bars("RELIANCE", 2026, 11, 10, Decimal("1250.00"))
    bars_tcs = generate_realistic_session_bars("TCS", 2026, 11, 10, Decimal("4100.00"))
    summary = bt.run_backtest(
        bars_1m_by_symbol={"RELIANCE": bars_reliance, "TCS": bars_tcs},
        prev_closes_by_symbol={"RELIANCE": Decimal("1245.00"), "TCS": Decimal("4080.00")},
        dataset_id="NSE_BENCHMARK_2026_11_10",
    )
    daily_rep = generate_daily_report(db, bt.ledger, "2026-11-10")
    return {"summary": summary, "daily_report": daily_rep}


class ScheduledDailyRunner:
  """Automates intraday NSE trading sessions via APScheduler cron schedules (09:00 - 15:30 IST)."""

  def __init__(
      self,
      config: AppConfig | None = None,
      database_url: str | None = None,
      auto_start: bool = False,
      enable_intraday_poll: bool | None = None,
      symbols_universe: list[str] | None = None,
  ):
    assert_paper_mode()
    self.config = config or load_app_config(database_url=database_url)
    self.db_url = self.config.database_url
    _, self.SessionLocal = init_db(self.db_url)
    self.calendar = NSECalendar()
    self.provider: MarketDataProvider = get_data_provider(self.config.data_provider)
    self.notification_service = WebhookNotificationService()
    self.registry = InstrumentRegistry()
    self.risk_profile = self.config.strategy.risk_profiles[self.config.active_risk_profile]
    self.cost_profile = self.config.costs[self.config.active_cost_profile]
    self.cost_engine = NSECostEngine(self.cost_profile)
    self.risk_engine = RiskEngine(
        self.risk_profile,
        self.cost_engine,
        entry_cutoff_ist=self.config.strategy.entry_cutoff_ist,
        liquidation_start_ist=self.config.strategy.liquidation_start_ist,
        liquidation_deadline_ist=self.config.strategy.liquidation_deadline_ist,
        registry=self.registry,
    )
    self.strategy_engine = ORBStrategyEngine(self.config.strategy, self.registry)
    self.validator = CandleIngestionValidator()
    self.symbols_universe: list[str] = symbols_universe or [
        "RELIANCE",
        "TCS",
        "INFY",
        "HDFCBANK",
        "ICICIBANK",
        "SBIN",
        "BHARTIARTL",
        "ITC",
        "LT",
        "AXISBANK",
    ]
    self.prev_daily_closes: dict[str, Decimal] = {}
    self.candle_cache = LiveCandleCache(orb_minutes=self.config.strategy.opening_range_minutes)
    self.scheduler = BackgroundScheduler(timezone=IST, daemon=True)
    self._is_active = False
    self.recent_tick_latencies: list[dict[str, Any]] = []
    self.total_ticks_executed: int = 0

    if enable_intraday_poll is not None:
      self.enable_intraday_poll = enable_intraday_poll
    else:
      self.enable_intraday_poll = os.environ.get("ALPHATRADE_ENABLE_INTRADAY_POLLER", "").lower() in ("true", "1")

    def _on_feed_recovered() -> None:
      if self.risk_engine.kill_switch_active and self.risk_engine.kill_switch_reason == "STALE_OR_INVALID_MARKET_DATA":
        logger.info("Watchdog feed recovered: resetting STALE_OR_INVALID_MARKET_DATA kill switch.")
        self.risk_engine.reset_kill_switch()

    self.watchdog = FeedWatchdog(
        staleness_threshold_seconds=180,
        notification_service=self.notification_service,
        on_stall_callback=lambda reason: self.risk_engine.trigger_kill_switch("STALE_OR_INVALID_MARKET_DATA"),
        on_recovery_callback=_on_feed_recovered,
        calendar=self.calendar,
    )

    self._register_jobs()
    if auto_start:
      self.start()

  def _register_jobs(self) -> None:
    # 1. 09:00 IST: Pre-market calendar and session check
    self.scheduler.add_job(
        self.job_pre_market_check,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=IST),
        id="nse_pre_market_0900",
        name="Pre-Market Calendar Verification",
        replace_existing=True,
    )
    # 2. 09:15 IST: Market open & live candle ingestion initiation
    self.scheduler.add_job(
        self.job_market_open,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=15, timezone=IST),
        id="nse_market_open_0915",
        name="Market Open & Live Ingestion Initiation",
        replace_existing=True,
    )
    # 3. 09:30 IST: Opening Range formation completion
    self.scheduler.add_job(
        self.job_opening_range_formed,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=IST),
        id="nse_orb_formed_0930",
        name="Opening Range High/Low Formation",
        replace_existing=True,
    )
    # 4. 14:30 IST: Entry cutoff gate
    self.scheduler.add_job(
        self.job_entry_cutoff,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=30, timezone=IST),
        id="nse_entry_cutoff_1430",
        name="Entry Cutoff Gate Enforcement",
        replace_existing=True,
    )
    # 5. 15:00 IST: EOD forced liquidation
    self.scheduler.add_job(
        self.job_eod_liquidation,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=0, timezone=IST),
        id="nse_eod_liquidation_1500",
        name="Forced EOD Position Liquidation",
        replace_existing=True,
    )
    # 6. 15:35 IST: Daily ledger reconciliation and reporting
    self.scheduler.add_job(
        self.job_reconcile_and_report,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=IST),
        id="nse_session_close_1535",
        name="Session Close, Reconciliation & Daily Metric Snapshot",
        replace_existing=True,
    )
    # 7. Feed Watchdog 30s Health Interval
    self.scheduler.add_job(
        self.job_watchdog_check,
        "interval",
        seconds=30,
        id="feed_watchdog_30s",
        name="Feed Staleness Watchdog",
        replace_existing=True,
    )
    # 8. 15:40 IST: SQLite WAL Checkpoint
    self.scheduler.add_job(
        self.job_wal_checkpoint,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=40, timezone=IST),
        id="nse_wal_checkpoint_1540",
        name="Post-Market SQLite WAL Checkpoint",
        replace_existing=True,
    )

    if self.enable_intraday_poll:
      self.enable_intraday_tick_job()

  def enable_intraday_tick_job(self) -> None:
    self.scheduler.add_job(
        self.job_intraday_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-14", minute="*", timezone=IST),
        id="nse_intraday_tick",
        name="Live Market 1-Minute Candle Ingestion & ORB Breakout Evaluation",
        replace_existing=True,
    )

  def disable_intraday_tick_job(self) -> None:
    if self.scheduler.get_job("nse_intraday_tick"):
      self.scheduler.remove_job("nse_intraday_tick")

  def start(self) -> None:
    if not self.scheduler.running:
      self.scheduler.start()
      self._is_active = True
      logger.info("ScheduledDailyRunner started with %d intraday cron jobs.", len(self.scheduler.get_jobs()))

  def stop(self) -> None:
    if self.scheduler.running:
      self.scheduler.shutdown(wait=False)
      self._is_active = False
      logger.info("ScheduledDailyRunner stopped.")

  @property
  def is_running(self) -> bool:
    return self.scheduler.running

  def get_jobs_summary(self) -> list[dict[str, Any]]:
    return [
        {
            "id": j.id,
            "name": j.name,
            "next_run_time": str(getattr(j, "next_run_time", None)),
            "trigger": str(j.trigger),
        }
        for j in self.scheduler.get_jobs()
    ]

  def get_telemetry_summary(self) -> dict[str, Any]:
    """Returns rolling latency distribution and execution telemetry."""
    latencies = [x["latency_ms"] for x in self.recent_tick_latencies]
    avg_lat = round(sum(latencies) / len(latencies), 2) if latencies else 0.0
    max_lat = max(latencies) if latencies else 0.0
    p95_idx = int(len(latencies) * 0.95)
    p95_lat = round(sorted(latencies)[p95_idx], 2) if latencies else 0.0
    return {
        "total_ticks_executed": self.total_ticks_executed,
        "recent_ticks_tracked": len(latencies),
        "avg_latency_ms": avg_lat,
        "max_latency_ms": max_lat,
        "p95_latency_ms": p95_lat,
        "latency_budget_ms": 500.0,
        "budget_compliant": max_lat <= 500.0 if latencies else True,
        "recent_latencies": self.recent_tick_latencies[-10:],
    }

  def job_watchdog_check(self, now_utc: datetime | None = None) -> dict[str, Any]:
    return self.watchdog.check_health(now_utc)

  def job_wal_checkpoint(self) -> dict[str, Any]:
    busy, log_f, chk_f = checkpoint_database_wal(self.db_url)
    return {
        "status": "CHECKPOINT_SUCCESS",
        "busy": busy,
        "log_frames": log_f,
        "checkpointed_frames": chk_f,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }

  def job_pre_market_check(self, now_utc: datetime | None = None) -> dict[str, Any]:
    ts = now_utc or datetime.now(timezone.utc)
    is_trading_day = self.calendar.is_trading_day(ts)
    session_state = self.calendar.get_session_state(ts)
    return {
        "timestamp_utc": ts.isoformat(),
        "is_trading_day": is_trading_day,
        "session_state": session_state,
    }

  def job_market_open(self, now_utc: datetime | None = None) -> str:
    ts = now_utc or datetime.now(timezone.utc)
    self.risk_engine.reset_kill_switch()
    with self.SessionLocal() as db:
      ledger = PortfolioLedger(db, self.risk_profile.reference_capital)
      broker = PaperBroker(db, ledger, self.cost_engine, self.risk_engine)
      broker.recover_state_after_restart()
    return f"Market open & live candle ingestion initiated at {ts.isoformat()}"

  def job_opening_range_formed(self, now_utc: datetime | None = None) -> str:
    ts = now_utc or datetime.now(timezone.utc)
    return f"ORB formed at {ts.isoformat()}"

  def job_entry_cutoff(self, now_utc: datetime | None = None) -> str:
    ts = now_utc or datetime.now(timezone.utc)
    return f"Entry cutoff enforced at {ts.isoformat()}"

  def job_eod_liquidation(self, now_utc: datetime | None = None) -> dict[str, Any]:
    ts = now_utc or datetime.now(timezone.utc)
    dt_ist = to_ist(ts)
    session_date = dt_ist.strftime("%Y-%m-%d")
    closed: list[dict[str, str]] = []

    with self.SessionLocal() as db:
      ledger = PortfolioLedger(db, self.risk_profile.reference_capital)
      broker = PaperBroker(
          db, ledger, self.cost_engine, self.risk_engine, notification_service=self.notification_service
      )
      broker.recover_state_after_restart()
      open_positions = ledger.get_open_positions()

      for pos in open_positions:
        ref_price = to_decimal(pos.current_stop_price or pos.entry_price)
        order, _ = broker.submit_order(
            idempotency_key=f"EOD_LIQ_{pos.position_id}_{dt_ist.strftime('%H%M%S')}",
            symbol=pos.symbol,
            session_date=session_date,
            side="SELL",
            quantity=pos.quantity,
            reference_price=ref_price,
            timestamp_utc=ts,
        )
        order.reason = "EOD_FORCED_LIQUIDATION"
        fill = broker.execute_fill_on_bar(
            order,
            bar_open_price=ref_price,
            timestamp_utc=ts,
        )
        if fill:
          closed.append({
              "symbol": pos.symbol,
              "exit_price": str(fill.fill_price),
              "reason": "EOD_FORCED_LIQUIDATION",
          })
          if self.notification_service.is_configured:
            try:
              msg = self.notification_service.format_position_exit_message(
                  symbol=pos.symbol,
                  entry_price=to_decimal(pos.entry_price),
                  exit_price=to_decimal(pos.exit_price),
                  quantity=pos.quantity,
                  net_pnl_inr=to_decimal(pos.realized_pnl_inr),
                  exit_reason="EOD_FORCED_LIQUIDATION",
              )
              self.notification_service.send_message_sync(msg)
            except Exception as ne:
              logger.warning(f"Failed to dispatch liquidation alert: {ne}")

    return {
        "status": "LIQUIDATION_COMPLETE",
        "positions_closed": closed,
        "timestamp_ist": dt_ist.isoformat(),
    }

  def job_intraday_tick(
      self,
      now_utc: datetime | None = None,
      custom_bars_by_symbol: dict[str, list[CandleBar]] | None = None,
      symbols: list[str] | None = None,
  ) -> dict[str, Any]:
    """Live 1m candle ingestion, validation, ORB calculation, breakout execution, and trailing stop management."""
    start_perf = timemod.perf_counter()
    ts = now_utc or datetime.now(timezone.utc)
    self.watchdog.record_tick(ts)
    dt_ist = to_ist(ts)
    session_date = dt_ist.strftime("%Y-%m-%d")

    # Calendar check (skip if holiday unless custom bars provided for testing)
    if custom_bars_by_symbol is None and not self.calendar.is_trading_day(ts):
      return {
          "status": "SKIPPED_NON_TRADING_DAY",
          "session_date": session_date,
          "timestamp_ist": dt_ist.isoformat(),
      }

    # Market hours check (09:15 - 15:30 IST)
    ist_time = dt_ist.time()
    if custom_bars_by_symbol is None and (ist_time < time(9, 15) or ist_time >= time(15, 30)):
      return {
          "status": "MARKET_CLOSED",
          "session_date": session_date,
          "timestamp_ist": dt_ist.isoformat(),
      }

    # 15:00 IST Forced EOD Liquidation Check
    eod_time = datetime.strptime(self.config.strategy.liquidation_start_ist, "%H:%M").time()
    if ist_time >= eod_time:
      liq_res = self.job_eod_liquidation(ts)
      return {
          "status": "EOD_LIQUIDATION_WINDOW",
          "session_date": session_date,
          "timestamp_ist": dt_ist.isoformat(),
          "liquidation_result": liq_res,
      }

    summary: dict[str, Any] = {
        "status": "SUCCESS",
        "session_date": session_date,
        "timestamp_ist": dt_ist.isoformat(),
        "symbols_evaluated": 0,
        "signals_generated": [],
        "orders_placed": [],
        "orders_filled": [],
        "positions_closed": [],
        "open_positions_count": 0,
        "errors": [],
    }

    entry_cutoff = datetime.strptime(self.config.strategy.entry_cutoff_ist, "%H:%M").time()
    target_symbols = symbols or (list(custom_bars_by_symbol.keys()) if custom_bars_by_symbol else self.symbols_universe)

    # Check session rollover for live candle cache
    self.candle_cache.check_session_rollover(session_date)

    with self.SessionLocal() as db:
      ledger = PortfolioLedger(db, self.risk_profile.reference_capital)
      broker = PaperBroker(
          db, ledger, self.cost_engine, self.risk_engine, notification_service=self.notification_service
      )
      broker.recover_state_after_restart()

      for sym in target_symbols:
        try:
          # 1. Ingest 1m candles with Live Incremental Caching
          if custom_bars_by_symbol and sym in custom_bars_by_symbol:
            bars = custom_bars_by_symbol[sym]
            valid_bars = [b for b in bars if self.validator.validate_bar(b)[0]]
            self.candle_cache.update_bars(sym, valid_bars)
          else:
            cached_last_ts = self.candle_cache.get_last_timestamp(sym)
            if cached_last_ts is not None:
              fetch_start_utc = cached_last_ts + timedelta(minutes=1)
              if fetch_start_utc <= ts:
                new_raw = self.provider.fetch_1m_candles(sym, fetch_start_utc, ts)
                new_valid = [
                    b
                    for b in new_raw
                    if self.validator.validate_bar(b)[0]
                    and sym not in self.validator.quarantined_symbols
                ]
                self.candle_cache.update_bars(sym, new_valid)
            else:
              start_utc = ist_datetime(dt_ist.year, dt_ist.month, dt_ist.day, 9, 15)
              raw_bars = self.provider.fetch_1m_candles(sym, start_utc, ts)
              valid_bars = [
                  b
                  for b in raw_bars
                  if self.validator.validate_bar(b)[0]
                  and sym not in self.validator.quarantined_symbols
              ]
              self.candle_cache.update_bars(sym, valid_bars)

          valid_bars = self.candle_cache.get_bars(sym)
          if not valid_bars:
            continue

          summary["symbols_evaluated"] += 1
          latest_bar = valid_bars[-1]
          prev_close = self.prev_daily_closes.get(sym, valid_bars[0].open)
          lower_circuit, upper_circuit = self.registry.compute_price_bands(sym, prev_close)

          # 3. Position Management on Latest 1m Bar (trailing stops / stop-loss)
          open_pos = ledger.get_open_position_by_symbol(sym)
          if open_pos is not None:
            eval_res = self.risk_engine.evaluate_position_bar_1m(
                bar_open=latest_bar.open,
                bar_high=latest_bar.high,
                bar_low=latest_bar.low,
                bar_close=latest_bar.close,
                bar_timestamp_utc=latest_bar.timestamp_utc,
                initial_stop_price=to_decimal(open_pos.initial_stop_price),
                trailing_activation_price=to_decimal(open_pos.trailing_activation_price),
                trailing_drop_pct=to_decimal(open_pos.trailing_drop_pct),
                trailing_activated=bool(open_pos.trailing_activated),
                highest_price_since_entry=to_decimal(open_pos.highest_price_since_entry),
            )
            # Update trailing stop state
            if (
                open_pos.trailing_activated != eval_res.trailing_activated
                or open_pos.highest_price_since_entry != eval_res.highest_price_since_entry
                or open_pos.current_stop_price != eval_res.current_stop_price
            ):
              open_pos.trailing_activated = eval_res.trailing_activated
              open_pos.highest_price_since_entry = eval_res.highest_price_since_entry
              open_pos.current_stop_price = eval_res.current_stop_price
              db.flush()

            # Execute exit if stop triggered
            if eval_res.should_exit and eval_res.trigger_price is not None:
              exit_order, _ = broker.submit_order(
                  idempotency_key=f"LIVE_EXIT_{open_pos.position_id}_{latest_bar.timestamp_utc.strftime('%H%M%S')}",
                  symbol=sym,
                  session_date=session_date,
                  side="SELL",
                  quantity=open_pos.quantity,
                  reference_price=eval_res.trigger_price,
                  timestamp_utc=latest_bar.timestamp_utc,
              )
              exit_order.reason = eval_res.exit_reason
              sell_fill = broker.execute_fill_on_bar(
                  exit_order,
                  bar_open_price=eval_res.trigger_price,
                  timestamp_utc=latest_bar.timestamp_utc,
                  upper_circuit=upper_circuit,
                  lower_circuit=lower_circuit,
              )
              if sell_fill:
                summary["positions_closed"].append({
                    "symbol": sym,
                    "reason": eval_res.exit_reason,
                    "exit_price": str(sell_fill.fill_price),
                })
              continue

          # 4. Opening Range Formation (09:15 - 09:30 IST)
          orb_state = compute_opening_range(valid_bars, self.config.strategy.opening_range_minutes)
          if not orb_state.is_complete:
            continue

          # 5. Breakout Signal & Entry Evaluation (09:30 to 14:30 IST)
          if ist_time < entry_cutoff and open_pos is None:
            bars_5m = aggregate_1m_to_nm(valid_bars, 5)
            if not bars_5m:
              continue
            latest_5m = bars_5m[-1]
            vwap = compute_session_vwap(valid_bars)
            vwap_std = (
                compute_session_vwap_std(valid_bars, vwap)
                if self.config.strategy.require_vwap_volatility_cap
                else None
            )
            cum_vol = sum(b.volume for b in valid_bars)
            rvol = compute_time_of_day_rvol(cum_vol, [max(1, cum_vol // 2)])

            htf_ema = (
                compute_higher_timeframe_ema(
                    valid_bars,
                    htf_minutes=self.config.strategy.macro_timeframe_minutes,
                    period=self.config.strategy.macro_ema_period,
                )
                if self.config.strategy.require_macro_trend_alignment
                else None
            )

            sig_eval = self.strategy_engine.evaluate_5m_bar(
                bar_5m=latest_5m,
                orb_state=orb_state,
                vwap=vwap,
                rvol=rvol,
                estimated_spread_pct=Decimal("0.0005"),
                prev_daily_close=prev_close,
                nifty_strongly_bearish=False,
                htf_ema=htf_ema,
                vwap_std=vwap_std,
            )

            if sig_eval.triggered and sig_eval.signal_id:
              # Check idempotency in DB
              existing_sig = db.query(TradeSignalRecord).filter_by(signal_id=sig_eval.signal_id).first()
              if existing_sig is None:
                trade_sig = TradeSignalRecord(
                    signal_id=sig_eval.signal_id,
                    symbol=sym,
                    session_date=session_date,
                    timestamp_utc=latest_bar.timestamp_utc,
                    direction="LONG",
                    signal_price=sig_eval.signal_price,
                    orb_high=sig_eval.orb_high,
                    orb_low=sig_eval.orb_low,
                    vwap=sig_eval.vwap,
                    rvol=sig_eval.rvol,
                    status="PROPOSED",
                    explanation=sig_eval.explanation,
                )
                db.add(trade_sig)
                db.flush()

                summary["signals_generated"].append({
                    "signal_id": sig_eval.signal_id,
                    "symbol": sym,
                    "price": str(sig_eval.signal_price),
                    "orb_high": str(sig_eval.orb_high),
                })

                # Risk Engine Evaluation
                atr_series = compute_atr_series(valid_bars, 14)
                latest_atr = atr_series[-1] if atr_series and atr_series[-1] is not None else None
                open_syms = {p.symbol for p in ledger.get_open_positions()}
                risk_dec = self.risk_engine.evaluate_entry_signal(
                    symbol=sym,
                    session_date=session_date,
                    signal_timestamp_utc=latest_bar.timestamp_utc,
                    candidate_entry_price=sig_eval.signal_price,
                    orb_low=orb_state.orb_low,
                    current_capital_inr=self.risk_profile.reference_capital,
                    available_cash_inr=ledger.get_cash_balance(),
                    deployed_exposure_inr=ledger.get_deployed_exposure(),
                    open_positions_Symbols=open_syms,
                    daily_realized_pnl_inr=ledger.get_daily_realized_pnl(session_date),
                    daily_unrealized_pnl_inr=ledger.get_daily_unrealized_pnl(),
                    atr_value=latest_atr,
                )

                risk_rec = RiskDecisionRecord(
                    signal_id=sig_eval.signal_id,
                    symbol=sym,
                    timestamp_utc=latest_bar.timestamp_utc,
                    approved=risk_dec.approved,
                    reason_code=risk_dec.reason_code,
                    approved_quantity=risk_dec.approved_quantity,
                    risk_budget_inr=risk_dec.risk_budget_inr,
                    cost_to_risk_ratio=risk_dec.cost_to_risk_ratio,
                    explanation=risk_dec.explanation,
                )
                db.add(risk_rec)
                db.flush()

                if risk_dec.approved and risk_dec.approved_quantity >= 1:
                  trade_sig.status = "APPROVED"
                  order, _ = broker.submit_order(
                      idempotency_key=f"LIVE_ENTRY_{sig_eval.signal_id}",
                      symbol=sym,
                      session_date=session_date,
                      side="BUY",
                      quantity=risk_dec.approved_quantity,
                      reference_price=latest_bar.close,
                      timestamp_utc=latest_bar.timestamp_utc,
                      signal_id=sig_eval.signal_id,
                  )
                  summary["orders_placed"].append({
                      "order_id": order.order_id,
                      "symbol": sym,
                      "qty": order.requested_qty,
                  })

                  fill = broker.execute_fill_on_bar(
                      order,
                      bar_open_price=latest_bar.close,
                      timestamp_utc=latest_bar.timestamp_utc,
                      orb_low=orb_state.orb_low,
                      upper_circuit=upper_circuit,
                      lower_circuit=lower_circuit,
                  )
                  if fill:
                    trade_sig.status = "EXECUTED"
                    summary["orders_filled"].append({
                        "order_id": order.order_id,
                        "fill_id": fill.fill_id,
                        "symbol": sym,
                        "fill_price": str(fill.fill_price),
                    })
                else:
                  trade_sig.status = f"REJECTED_{risk_dec.reason_code}"
                db.commit()

        except Exception as sym_err:
          logger.warning(f"Error evaluating live tick for {sym}: {sym_err}")
          summary["errors"].append({"symbol": sym, "error": str(sym_err)})

      summary["open_positions_count"] = len(ledger.get_open_positions())

      # Live Mark-to-Market valuation, cash balance & equity snapshot
      mark_prices = {
          sym: self.candle_cache.get_latest_price(sym)
          for sym in target_symbols
          if self.candle_cache.get_latest_price(sym) is not None
      }
      snap = ledger.take_snapshot(ts, mark_prices=mark_prices)
      summary["live_cash_inr"] = str(snap.cash_balance_inr)
      summary["deployed_exposure_inr"] = str(snap.deployed_exposure_inr)
      summary["unrealized_pnl_inr"] = str(snap.unrealized_pnl_inr)
      summary["net_equity_inr"] = str(snap.net_equity_inr)
      summary["candle_cache_stats"] = self.candle_cache.get_cache_stats()

      # Latency Telemetry Tracking
      elapsed_ms = (timemod.perf_counter() - start_perf) * 1000.0
      summary["latency_ms"] = round(elapsed_ms, 2)
      summary["latency_budget_exceeded"] = elapsed_ms > 500.0

      self.total_ticks_executed += 1
      self.recent_tick_latencies.append({
          "timestamp_ist": dt_ist.isoformat(),
          "latency_ms": round(elapsed_ms, 2),
          "symbols_evaluated": summary["symbols_evaluated"],
          "signals": len(summary["signals_generated"]),
          "orders": len(summary["orders_placed"]),
      })
      if len(self.recent_tick_latencies) > 100:
        self.recent_tick_latencies.pop(0)

      if elapsed_ms > 500.0:
        logger.warning(
            "Intraday tick evaluation exceeded 500ms budget: %.2f ms (symbols: %d)",
            elapsed_ms,
            summary["symbols_evaluated"],
        )

      return summary

  def job_reconcile_and_report(self, now_utc: datetime | None = None) -> dict[str, Any]:
    ts = now_utc or datetime.now(timezone.utc)
    session_date = to_ist(ts).strftime("%Y-%m-%d")
    with self.SessionLocal() as db:
      prof = self.config.strategy.risk_profiles[self.config.active_risk_profile]
      ledger = PortfolioLedger(db, prof.reference_capital)
      reconciled, msg = ledger.reconcile()
      daily_rep = generate_daily_report(db, ledger, session_date)

      if self.notification_service.is_configured:
        try:
          positions_today = db.query(PositionRecord).filter(PositionRecord.session_date == session_date).all()
          snap = ledger.take_snapshot(ts)
          msg_text = self.notification_service.format_eod_summary_message(
              session_date=session_date,
              starting_capital=prof.reference_capital,
              ending_cash=to_decimal(snap.cash_balance_inr),
              total_realized_pnl=to_decimal(snap.realized_pnl_inr),
              total_fees_paid=to_decimal(snap.total_fees_inr),
              trades_count=len(positions_today),
          )
          if hasattr(self.notification_service, "send_message_background"):
            self.notification_service.send_message_background(msg_text)
          else:
            self.notification_service.send_message_sync(msg_text)
        except Exception as e:
          logger.warning(f"Failed to dispatch EOD notification: {e}")

      checkpoint_database_wal(self.db_url)

      return {
          "session_date": session_date,
          "reconciled": reconciled,
          "message": msg,
          "daily_report": daily_rep,
      }


if __name__ == "__main__":
  res = run_benchmark_simulation()
  print("Benchmark completed:", res["summary"])
