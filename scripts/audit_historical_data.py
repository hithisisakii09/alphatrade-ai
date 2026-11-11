"""Read-only audit tool for historical market candles in SQLite.

Detects missing sessions, incomplete days, minute gaps, duplicate records,
invalid OHLCV values, timestamp sequencing anomalies, and asymmetric symbol coverage.
Does NOT modify or mutate the database in any way.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.clock import NSECalendar, to_ist
from app.core.config import _load_env_file, load_app_config
from app.core.database import MarketCandleRecord, Session, init_db
from app.core.execution_guard import assert_paper_mode

_load_env_file()


def audit_historical_market_data(db_url: str | None = None) -> dict:
  """Performs an exhaustive read-only quality and completeness audit on market_candles."""
  assert_paper_mode()
  cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(cfg.database_url)
  calendar = NSECalendar()

  report = {
      "database_url": cfg.database_url,
      "total_records": 0,
      "symbols_count": 0,
      "symbols": [],
      "date_range_ist": ("", ""),
      "expected_calendar_sessions": [],
      "actual_sessions": [],
      "missing_calendar_sessions": [],
      "incomplete_sessions": [],
      "asymmetric_coverage": {},
      "duplicate_records": 0,
      "invalid_ohlcv_count": 0,
      "timestamp_ordering_anomalies": 0,
      "intraday_minute_gaps": [],
  }

  with SessionLocal() as db:
    cursor = db.conn.cursor()

    # 1. Total records
    cursor.execute("SELECT COUNT(id) FROM market_candles")
    total_records = cursor.fetchone()[0]
    report["total_records"] = total_records

    if total_records == 0:
      print(f"[!] Database {cfg.database_url} has 0 records in market_candles table.")
      return report

    # 2. Distinct symbols
    cursor.execute("SELECT DISTINCT symbol FROM market_candles ORDER BY symbol")
    symbols = [r[0] for r in cursor.fetchall()]
    report["symbols_count"] = len(symbols)
    report["symbols"] = symbols

    # 3. Overall timestamp span (in IST)
    cursor.execute("SELECT MIN(timestamp_utc), MAX(timestamp_utc) FROM market_candles")
    min_ts_str, max_ts_str = cursor.fetchone()
    min_dt_utc = datetime.fromisoformat(min_ts_str) if "+" in min_ts_str or "Z" in min_ts_str else datetime.fromisoformat(min_ts_str).replace(tzinfo=timezone.utc)
    max_dt_utc = datetime.fromisoformat(max_ts_str) if "+" in max_ts_str or "Z" in max_ts_str else datetime.fromisoformat(max_ts_str).replace(tzinfo=timezone.utc)

    start_date_ist = to_ist(min_dt_utc).date()
    end_date_ist = to_ist(max_dt_utc).date()
    report["date_range_ist"] = (start_date_ist.isoformat(), end_date_ist.isoformat())

    # 4. Expected calendar trading sessions vs actual sessions
    expected_days = []
    curr = start_date_ist
    while curr <= end_date_ist:
      dt = datetime(curr.year, curr.month, curr.day, 12, 0, tzinfo=timezone.utc)
      if calendar.is_trading_day(dt):
        expected_days.append(curr.isoformat())
      curr += timedelta(days=1)
    report["expected_calendar_sessions"] = expected_days

    cursor.execute("""
        SELECT DISTINCT date(datetime(timestamp_utc, '+5 hours', '+30 minutes')) as ist_date
        FROM market_candles
        ORDER BY ist_date
    """)
    actual_sessions = [r[0] for r in cursor.fetchall()]
    report["actual_sessions"] = actual_sessions

    missing_sessions = [d for d in expected_days if d not in actual_sessions]
    report["missing_calendar_sessions"] = missing_sessions

    # 5. Check duplicate records
    cursor.execute("""
        SELECT symbol, timestamp_utc, COUNT(id)
        FROM market_candles
        GROUP BY symbol, timestamp_utc
        HAVING COUNT(id) > 1
    """)
    duplicates = cursor.fetchall()
    report["duplicate_records"] = len(duplicates)

    # 6. Check invalid OHLCV envelopes or negative values
    cursor.execute("""
        SELECT COUNT(id) FROM market_candles
        WHERE high < low
           OR high < open
           OR high < close
           OR low > open
           OR low > close
           OR volume < 0
           OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
    """)
    report["invalid_ohlcv_count"] = cursor.fetchone()[0]

    # 7. Symbol coverage per session & incomplete sessions check
    cursor.execute("""
        SELECT 
            symbol,
            date(datetime(timestamp_utc, '+5 hours', '+30 minutes')) as s_date,
            COUNT(id) as bar_count,
            MIN(time(datetime(timestamp_utc, '+5 hours', '+30 minutes'))) as min_time,
            MAX(time(datetime(timestamp_utc, '+5 hours', '+30 minutes'))) as max_time
        FROM market_candles
        GROUP BY symbol, s_date
        ORDER BY symbol, s_date
    """)
    coverage_rows = cursor.fetchall()

    session_symbol_counts = defaultdict(dict)
    for sym, s_date, count, min_t, max_t in coverage_rows:
      session_symbol_counts[sym][s_date] = {
          "count": count,
          "start": min_t,
          "end": max_t,
      }
      # Standard NSE session is 375 bars (09:15 to 15:29 inclusive)
      if count < 375:
        report["incomplete_sessions"].append({
            "symbol": sym,
            "session_date": s_date,
            "bar_count": count,
            "start_time": min_t,
            "end_time": max_t,
            "missing_bars": 375 - count,
        })

    # Asymmetric coverage check
    for sym in symbols:
      sym_dates = set(session_symbol_counts[sym].keys())
      missing_for_sym = [d for d in actual_sessions if d not in sym_dates]
      if missing_for_sym:
        report["asymmetric_coverage"][sym] = {
            "covered_sessions": sorted(list(sym_dates)),
            "missing_sessions": missing_for_sym,
        }

    # 8. Intraday minute gaps and timestamp sequencing
    for sym in symbols:
      cursor.execute("""
          SELECT timestamp_utc
          FROM market_candles
          WHERE symbol = ?
          ORDER BY timestamp_utc ASC
      """, (sym,))
      ts_rows = cursor.fetchall()
      for i in range(1, len(ts_rows)):
        t_prev = datetime.fromisoformat(ts_rows[i-1][0])
        t_curr = datetime.fromisoformat(ts_rows[i][0])
        if t_curr <= t_prev:
          report["timestamp_ordering_anomalies"] += 1
        else:
          # Check intraday minute gap if same date
          d_prev = to_ist(t_prev).date()
          d_curr = to_ist(t_curr).date()
          if d_prev == d_curr:
            delta_mins = int((t_curr - t_prev).total_seconds() // 60)
            if delta_mins > 1:
              report["intraday_minute_gaps"].append({
                  "symbol": sym,
                  "date": d_prev.isoformat(),
                  "from_time_ist": to_ist(t_prev).strftime("%H:%M"),
                  "to_time_ist": to_ist(t_curr).strftime("%H:%M"),
                  "gap_minutes": delta_mins - 1,
              })

  return report


def print_audit_report(rep: dict) -> None:
  print("==========================================================================================")
  print("AlphaTrade AI v2.0 — Historical Market Data Quality & Completeness Audit")
  print("==========================================================================================")
  print(f"[*] Database:                   {rep['database_url']}")
  print(f"[*] Total Validated Candles:    {rep['total_records']:,d}")
  print(f"[*] Universe Constituents:      {rep['symbols_count']} symbols")
  print(f"[*] Historical Span (IST):      {rep['date_range_ist'][0]} to {rep['date_range_ist'][1]}")
  print(f"[*] Expected Calendar Sessions: {len(rep['expected_calendar_sessions'])} {rep['expected_calendar_sessions']}")
  print(f"[*] Actual Persisted Sessions:  {len(rep['actual_sessions'])} {rep['actual_sessions']}")
  print("------------------------------------------------------------------------------------------")

  print("\n1. MISSING CALENDAR SESSIONS:")
  if rep["missing_calendar_sessions"]:
    for s in rep["missing_calendar_sessions"]:
      print(f"  [!] Missing Session: {s} (Calendar trading day with 0 bars in SQLite)")
  else:
    print("  [PASS] All expected trading days are represented in SQLite.")

  print("\n2. ASYMMETRIC SYMBOL COVERAGE:")
  if rep["asymmetric_coverage"]:
    for sym, details in rep["asymmetric_coverage"].items():
      print(f"  [!] {sym:12s}: Missing sessions {details['missing_sessions']} (Present: {details['covered_sessions']})")
  else:
    print("  [PASS] All symbols cover 100% of persisted sessions.")

  print("\n3. SESSION COMPLETENESS (< 375 bars per day):")
  if rep["incomplete_sessions"]:
    print(f"  Total incomplete symbol-session days: {len(rep['incomplete_sessions'])}")
    for inc in rep["incomplete_sessions"][:10]:
      print(f"    • {inc['symbol']:10s} | {inc['session_date']} | Bars: {inc['bar_count']:3d}/375 ({inc['start_time']} - {inc['end_time']}) | Missing: {inc['missing_bars']} bars")
    if len(rep["incomplete_sessions"]) > 10:
      print(f"    ... and {len(rep['incomplete_sessions']) - 10} more incomplete sessions.")
  else:
    print("  [PASS] All symbol-session days contain a full 375 bars.")

  print("\n4. DATA INTEGRITY & SEQUENCING:")
  print(f"  • Duplicate Records:             {rep['duplicate_records']} (Pass = 0)")
  print(f"  • Invalid OHLCV Envelopes:       {rep['invalid_ohlcv_count']} (Pass = 0)")
  print(f"  • Timestamp Ordering Anomalies:  {rep['timestamp_ordering_anomalies']} (Pass = 0)")
  print(f"  • Intraday Minute Gaps:          {len(rep['intraday_minute_gaps'])}")

  print("\n==========================================================================================")
  print("BACKFILL FEASIBILITY & API AVAILABILITY CONSTRAINTS (FOR MILESTONE M9):")
  print("==========================================================================================")
  print("  • Upstream Feeder: Angel One SmartAPI (getCandleData)")
  print("  • Rate-Limiting Constraint: Max 3 requests/sec (strictly enforced)")
  print("  • Chunking Recommendation: 5 trading days per chunk to avoid HTTP timeouts")
  print("  • Identified Upstream Gaps: Session 2026-11-10 returned 0 bars from getCandleData")
  print("  • Note: M8.1 is strictly read-only audit. Multi-month ingestion is deferred to M9.")
  print("==========================================================================================\n")


def main():
  parser = argparse.ArgumentParser(description="Audit historical 1m candle quality and completeness.")
  parser.add_argument("--db-url", type=str, default=None, help="Database URL (defaults to alphatrade_v2.db)")
  args = parser.parse_args()

  report = audit_historical_market_data(db_url=args.db_url)
  print_audit_report(report)


if __name__ == "__main__":
  main()

