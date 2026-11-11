"""CLI script to safely download, validate, and persist historical 1-minute candles from Angel One into SQLite."""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.clock import NSECalendar, ist_datetime, to_ist
from app.core.config import _load_env_file, load_app_config
from app.core.database import MarketCandleRecord, Session, init_db
from app.core.execution_guard import assert_paper_mode
from app.data.models import CandleBar
from app.data.providers import AngelSmartAPIDataProvider
from app.data.validator import CandleIngestionValidator

# Ensure .env is loaded before provider initialization
_load_env_file()


def download_historical_candles(
    symbols: list[str],
    trading_days: int = 5,
    db_url: str | None = None,
) -> dict[str, int]:
  assert_paper_mode()
  _load_env_file()
  cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(cfg.database_url)

  provider = AngelSmartAPIDataProvider()
  validator = CandleIngestionValidator()
  calendar = NSECalendar()

  now_utc = datetime.now(timezone.utc)
  now_ist = to_ist(now_utc)

  # Collect target past trading dates
  target_dates: list[datetime] = []
  curr = now_utc.date()
  checked_days = 0
  while len(target_dates) < trading_days and checked_days < 60:
    dt = datetime(curr.year, curr.month, curr.day, 12, 0, tzinfo=timezone.utc)
    if calendar.is_trading_day(dt):
      target_dates.append(dt)
    curr -= timedelta(days=1)
    checked_days += 1

  target_dates.reverse()
  results: dict[str, int] = {sym: 0 for sym in symbols}

  print("=================================================================")
  print("AlphaTrade AI v2.0 — Historical 1-Minute Candle Ingestion Tool")
  print("=================================================================")
  print(f"[*] Target Symbols: {', '.join(symbols)}")
  print(f"[*] Target Trading Days ({len(target_dates)}): {[to_ist(d).strftime('%Y-%m-%d') for d in target_dates]}")
  print(f"[*] Database: {cfg.database_url}")
  print("=================================================================\n")

  with SessionLocal() as db:
    for target_dt in target_dates:
      d_ist = to_ist(target_dt)
      s_date = d_ist.strftime("%Y-%m-%d")
      start_utc = ist_datetime(d_ist.year, d_ist.month, d_ist.day, 9, 15)
      end_utc = ist_datetime(d_ist.year, d_ist.month, d_ist.day, 15, 30)

      # If targeting today and market is currently active, clamp end_utc to now
      if s_date == now_ist.strftime("%Y-%m-%d") and now_utc < end_utc:
        end_utc = now_utc

      print(f"[*] Session {s_date} (09:15 to {to_ist(end_utc).strftime('%H:%M')} IST):")
      for sym in symbols:
        try:
          bars = provider.fetch_1m_candles(sym, start_utc, end_utc)
          if not bars:
            print(f"    • {sym:12s}: 0 bars returned")
            continue

          saved_count = 0
          existing_rows = db.conn.cursor().execute(
              "SELECT timestamp_utc FROM market_candles WHERE symbol = ? AND interval_minutes = 1",
              (sym,),
          ).fetchall()
          existing_timestamps = {r[0] for r in existing_rows}

          for b in bars:
            iso_ts = b.timestamp_utc.isoformat()
            if iso_ts in existing_timestamps:
              continue

            valid, reason = validator.validate_bar(b)
            if not valid:
              continue

            # Idempotent insert into SQLite market_candles
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
          print(f"    • {sym:12s}: {saved_count:3d} new 1m bars validated & stored in DB (total fetched: {len(bars)})")
        except Exception as e:
          print(f"    • {sym:12s}: Ingestion error - {e}")

  print("\n=================================================================")
  print("Historical Ingestion Summary:")
  for sym, count in results.items():
    print(f"  • {sym:12s}: {count:4d} total bars stored in SQLite")
  print("=================================================================")
  return results


def main():
  parser = argparse.ArgumentParser(description="Download and store historical 1m NSE candles.")
  parser.add_argument("--symbols", type=str, default="RELIANCE,TCS,INFY", help="Comma-separated symbols")
  parser.add_argument("--days", type=int, default=3, help="Number of past trading days to fetch")
  args = parser.parse_args()

  sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
  download_historical_candles(symbols=sym_list, trading_days=args.days)


if __name__ == "__main__":
  main()

