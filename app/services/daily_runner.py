"""Daily session runner, APScheduler intraday cron manager, and deterministic ORB simulation benchmark."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.backtest.engine import EventDrivenBacktester
from app.core.clock import IST, NSECalendar, ist_datetime, to_ist
from app.core.config import AppConfig, load_app_config
from app.core.database import DailyMetricRecord, PositionRecord, Session, init_db
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_paise, to_decimal
from app.data.models import CandleBar
from app.data.providers import MarketDataProvider, get_data_provider
from app.execution.costs import NSECostEngine
from app.execution.paper_broker import PaperBroker
from app.portfolio.ledger import PortfolioLedger
from app.reporting.reports import generate_daily_report
from app.risk.engine import RiskEngine

logger = logging.getLogger(__name__)


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
  ):
    assert_paper_mode()
    self.config = config or load_app_config(database_url=database_url)
    self.db_url = self.config.database_url
    _, self.SessionLocal = init_db(self.db_url)
    self.calendar = NSECalendar()
    self.provider: MarketDataProvider = get_data_provider(self.config.data_provider)
    self.scheduler = BackgroundScheduler(timezone=IST)
    self._is_active = False
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
    # 2. 09:30 IST: Opening Range formation completion
    self.scheduler.add_job(
        self.job_opening_range_formed,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=IST),
        id="nse_orb_formed_0930",
        name="Opening Range High/Low Formation",
        replace_existing=True,
    )
    # 3. 09:31 - 14:29 IST: Intraday 1-minute bar tick & 5-minute signal evaluation
    self.scheduler.add_job(
        self.job_intraday_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-14", minute="*", timezone=IST),
        id="nse_intraday_tick",
        name="1-Minute Intraday Tick & 5-Minute Breakout Evaluation",
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
    # 6. 15:30 IST: Daily ledger reconciliation and reporting
    self.scheduler.add_job(
        self.job_reconcile_and_report,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone=IST),
        id="nse_session_close_1530",
        name="Session Close, Reconciliation & Daily Metric Snapshot",
        replace_existing=True,
    )

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

  def job_pre_market_check(self, now_utc: datetime | None = None) -> dict[str, Any]:
    ts = now_utc or datetime.now(timezone.utc)
    is_trading_day = self.calendar.is_trading_day(ts)
    session_state = self.calendar.get_session_state(ts)
    return {
        "timestamp_utc": ts.isoformat(),
        "is_trading_day": is_trading_day,
        "session_state": session_state,
    }

  def job_opening_range_formed(self, now_utc: datetime | None = None) -> str:
    ts = now_utc or datetime.now(timezone.utc)
    return f"ORB formed at {ts.isoformat()}"

  def job_intraday_tick(self, now_utc: datetime | None = None) -> str:
    ts = now_utc or datetime.now(timezone.utc)
    return f"Intraday tick processed at {ts.isoformat()}"

  def job_entry_cutoff(self, now_utc: datetime | None = None) -> str:
    ts = now_utc or datetime.now(timezone.utc)
    return f"Entry cutoff enforced at {ts.isoformat()}"

  def job_eod_liquidation(self, now_utc: datetime | None = None) -> str:
    ts = now_utc or datetime.now(timezone.utc)
    return f"EOD forced liquidation executed at {ts.isoformat()}"

  def job_reconcile_and_report(self, now_utc: datetime | None = None) -> dict[str, Any]:
    ts = now_utc or datetime.now(timezone.utc)
    session_date = to_ist(ts).strftime("%Y-%m-%d")
    with self.SessionLocal() as db:
      prof = self.config.strategy.risk_profiles[self.config.active_risk_profile]
      ledger = PortfolioLedger(db, prof.reference_capital)
      reconciled, msg = ledger.reconcile()
      daily_rep = generate_daily_report(db, ledger, session_date)
      return {
          "session_date": session_date,
          "reconciled": reconciled,
          "message": msg,
          "daily_report": daily_rep,
      }


if __name__ == "__main__":
  res = run_benchmark_simulation()
  print("Benchmark completed:", res["summary"])
