"""CLI script to safely download, validate, and persist historical 1-minute candles from Angel One into SQLite."""

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
import time

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.clock import NSECalendar, ist_datetime, to_ist
from app.core.config import _load_env_file, load_app_config
from app.core.database import MarketCandleRecord, Session, init_db
from app.core.execution_guard import assert_paper_mode
from app.data.models import CandleBar
from app.data.providers import ANGEL_SYMBOL_TOKENS, AngelSmartAPIDataProvider
from app.data.validator import CandleIngestionValidator

# Ensure .env is loaded before provider initialization
_load_env_file()

DEFAULT_NIFTY_UNIVERSE = [
    "RELIANCE",
    "TCS",
    "INFY",
    "HDFCBANK",
    "ICICIBANK",
    "SBIN",
    "BHARTIARTL",
    "KOTAKBANK",
    "LT",
    "HINDUNILVR",
    "AXISBANK",
    "ITC",
    "BAJFINANCE",
    "ASIANPAINT",
    "MARUTI",
    "HCLTECH",
    "SUNPHARMA",
    "TITAN",
    "TATAMOTORS",
    "ULTRACEMCO",
]


def collect_target_dates(
    start_date_str: str | None = None,
    end_date_str: str | None = None,
    trading_days: int | None = None,
    calendar: NSECalendar | None = None,
) -> list[date]:
  """Computes candidate weekday/special-session dates for historical acquisition."""
  cal = calendar or NSECalendar()
  now_utc = datetime.now(timezone.utc)
  now_ist = to_ist(now_utc)

  if start_date_str and end_date_str:
    start_d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)
    if start_d > end_d:
      raise ValueError(f"start_date ({start_d}) cannot be after end_date ({end_d})")

    candidates: list[date] = []
    curr = start_d
    while curr <= end_d:
      # Include all weekdays and any designated special sessions (e.g. Muhurat trading)
      d_str = curr.isoformat()
      if curr.weekday() < 5 or d_str in cal.special_sessions:
        candidates.append(curr)
      curr += timedelta(days=1)
    return candidates

  # Fallback to rolling N days backwards from now
  n_days = trading_days or 5
  candidates = []
  curr = now_ist.date()
  checked = 0
  while len(candidates) < n_days and checked < 180:
    d_str = curr.isoformat()
    if curr.weekday() < 5 or d_str in cal.special_sessions:
      candidates.append(curr)
    curr -= timedelta(days=1)
    checked += 1

  candidates.reverse()
  return candidates


def download_historical_candles(
    symbols: list[str],
    start_date: str | None = None,
    end_date: str | None = None,
    trading_days: int | None = None,
    db_url: str | None = None,
) -> dict[str, int]:
  """Downloads, validates, and persists historical 1-minute candles from Angel One into SQLite."""
  assert_paper_mode()
  _load_env_file()
  cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(cfg.database_url)

  provider = AngelSmartAPIDataProvider()
  validator = CandleIngestionValidator()
  calendar = NSECalendar()

  now_utc = datetime.now(timezone.utc)
  now_ist = to_ist(now_utc)

  target_dates = collect_target_dates(
      start_date_str=start_date,
      end_date_str=end_date,
      trading_days=trading_days,
      calendar=calendar,
  )

  results: dict[str, int] = {sym: 0 for sym in symbols}

  print("=================================================================")
  print("AlphaTrade AI v2.0 — Historical 1-Minute Candle Ingestion Tool")
  print("=================================================================")
  print(f"[*] Universe Size:         {len(symbols)} symbols")
  print(f"[*] Constituents:          {', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''}")
  print(f"[*] Target Session Span:   {target_dates[0].isoformat()} to {target_dates[-1].isoformat()} ({len(target_dates)} candidate dates)")
  print(f"[*] Rate Limiter:          Angel One TokenBucket (3.0 req/s)")
  print(f"[*] Database:              {cfg.database_url}")
  print("=================================================================\n")

  with SessionLocal() as db:
    for session_idx, target_d in enumerate(target_dates, 1):
      s_date = target_d.isoformat()
      start_utc = ist_datetime(target_d.year, target_d.month, target_d.day, 9, 15)
      end_utc = ist_datetime(target_d.year, target_d.month, target_d.day, 15, 30)

      # Handle Muhurat special session if designated
      if s_date in calendar.special_sessions:
        spec = calendar.special_sessions[s_date]
        s_open = [int(x) for x in spec["open"].split(":")]
        s_close = [int(x) for x in spec["close"].split(":")]
        start_utc = ist_datetime(target_d.year, target_d.month, target_d.day, s_open[0], s_open[1])
        end_utc = ist_datetime(target_d.year, target_d.month, target_d.day, s_close[0], s_close[1])

      # Clamp if targeting current day and session still underway
      if s_date == now_ist.strftime("%Y-%m-%d") and now_utc < end_utc:
        end_utc = now_utc

      print(f"[{session_idx:2d}/{len(target_dates)}] Session {s_date} ({to_ist(start_utc).strftime('%H:%M')} to {to_ist(end_utc).strftime('%H:%M')} IST):")
      session_total = 0

      for sym in symbols:
        try:
          bars = provider.fetch_1m_candles(sym, start_utc, end_utc)
          if not bars:
            continue

          # Query existing timestamps in this specific window for fast deduplication
          existing_rows = db.conn.cursor().execute(
              """
              SELECT timestamp_utc FROM market_candles
              WHERE symbol = ? AND interval_minutes = 1
                AND timestamp_utc >= ? AND timestamp_utc <= ?
              """,
              (sym, start_utc.isoformat(), end_utc.isoformat()),
          ).fetchall()
          existing_timestamps = {r[0] for r in existing_rows}

          saved_count = 0
          for b in bars:
            iso_ts = b.timestamp_utc.isoformat()
            if iso_ts in existing_timestamps:
              continue

            valid, _ = validator.validate_bar(b)
            if not valid:
              continue

            rec = MarketCandleRecord(
                symbol=b.symbol,
                interval_minutes=1,
                timestamp_utc=b.timestamp_utc,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                source=b.source,
            )
            db.add(rec)
            existing_timestamps.add(iso_ts)
            saved_count += 1

          db.commit()
          results[sym] += saved_count
          session_total += saved_count
          if saved_count > 0:
            print(f"    • {sym:12s}: +{saved_count:3d} new bars (total fetched: {len(bars)})")
        except Exception as e:
          print(f"    • {sym:12s}: Ingestion error - {e}")

      if session_total == 0:
        print("    [!] No new bars stored (holiday, weekend, or already fully persisted)")

  total_ingested = sum(results.values())
  print("\n=================================================================")
  print(f"Historical Ingestion Complete: +{total_ingested} new candles stored in SQLite")
  print("=================================================================")
  return results


def main():
  parser = argparse.ArgumentParser(description="Download and store historical 1m NSE candles.")
  parser.add_argument(
      "--symbols",
      type=str,
      default="ALL",
      help="Comma-separated symbols or 'ALL' for standard 20 liquid NIFTY constituents",
  )
  parser.add_argument("--days", type=int, default=None, help="Number of past trading days to fetch")
  parser.add_argument("--start-date", type=str, default=None, help="Start date (YYYY-MM-DD)")
  parser.add_argument("--end-date", type=str, default=None, help="End date (YYYY-MM-DD)")
  args = parser.parse_args()

  if args.symbols.strip().upper() == "ALL":
    sym_list = DEFAULT_NIFTY_UNIVERSE
  else:
    sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

  download_historical_candles(
      symbols=sym_list,
      start_date=args.start_date,
      end_date=args.end_date,
      trading_days=args.days or (5 if not args.start_date else None),
  )


if __name__ == "__main__":
  main()
