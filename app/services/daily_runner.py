"""Daily session runner and deterministic multi-day ORB simulation benchmark."""

from datetime import timedelta
from decimal import Decimal
from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.core.database import init_db
from app.data.models import CandleBar
from app.backtest.engine import EventDrivenBacktester
from app.reporting.reports import generate_daily_report


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


if __name__ == "__main__":
  res = run_benchmark_simulation()
  print("Benchmark completed:", res["summary"])
