"""Fetches today's live 1-minute NSE candles from Angel One SmartAPI and computes live Opening Range (ORB)."""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.clock import IST, ist_datetime, to_ist
from app.core.config import _load_env_file
from app.data.models import CandleBar
from app.data.providers import AngelSmartAPIDataProvider, ProviderAuthenticationError, ProviderDataError
from app.data.validator import CandleIngestionValidator
from app.market.indicators import compute_opening_range


def main():
  _load_env_file()
  print("=" * 65)
  print("AlphaTrade AI v2.0 — Live Opening Range (ORB) Ingestion")
  print("=" * 65)

  provider = AngelSmartAPIDataProvider()
  validator = CandleIngestionValidator()

  now_utc = datetime.now(timezone.utc)
  now_ist = to_ist(now_utc)
  print(f"[*] Current System Time: {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')} ({now_utc.strftime('%H:%M:%S UTC')})")

  target_symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN"]
  start_utc = ist_datetime(now_ist.year, now_ist.month, now_ist.day, 9, 15)
  end_utc = now_utc

  print(f"[*] Query Window: 09:15 IST ({start_utc.strftime('%H:%M UTC')}) to {now_ist.strftime('%H:%M IST')} ({end_utc.strftime('%H:%M UTC')})")

  results = []
  for sym in target_symbols:
    token = provider.resolve_symbol_token(sym)
    print(f"\n[*] Querying {sym} (Token {token})...")
    try:
      bars = provider.fetch_1m_candles(sym, start_utc, end_utc)
    except ProviderAuthenticationError as e:
      print(f"    [AUTH ERROR] {e}")
      return 1
    except ProviderDataError as e:
      print(f"    [DATA NOTICE] {e}")
      continue
    except Exception as e:
      print(f"    [NETWORK ERROR] {e}")
      continue

    if not bars:
      print(f"    [INFO] No live candles returned yet for {sym} (market may be in pre-market or range empty).")
      continue

    valid_bars = []
    for b in bars:
      is_valid, _ = validator.validate_bar(b)
      if is_valid:
        valid_bars.append(b)

    orb = compute_opening_range(valid_bars, 15)
    latest_bar = valid_bars[-1]
    curr_price = latest_bar.close

    status = "INSIDE_RANGE"
    if orb and orb.is_complete:
      if curr_price > orb.orb_high:
        status = "BREAKOUT_ABOVE_ORH 🚀"
      elif curr_price < orb.orb_low:
        status = "BREAKDOWN_BELOW_ORL ⚠️"

    res_entry = {
        "symbol": sym,
        "bars_count": len(valid_bars),
        "orb_high": orb.orb_high if orb and orb.is_complete else None,
        "orb_low": orb.orb_low if orb and orb.is_complete else None,
        "current_price": curr_price,
        "status": status,
        "latest_time_ist": to_ist(latest_bar.timestamp_utc).strftime("%H:%M IST"),
    }
    results.append(res_entry)

    print(f"    [BARS] {len(valid_bars)} 1m bars validated")
    if orb and orb.is_complete:
      print(f"    [ORB 15M] ORH = ₹{orb.orb_high:,.2f} | ORL = ₹{orb.orb_low:,.2f}")
    print(f"    [LATEST]  ₹{curr_price:,.2f} at {res_entry['latest_time_ist']} --> Status: {status}")

  print("\n" + "=" * 65)
  print(f"{'SYMBOL':<12} {'BARS':<6} {'ORH':<10} {'ORL':<10} {'CURRENT':<10} {'STATUS'}")
  print("-" * 65)
  for r in results:
    orh_str = f"₹{r['orb_high']:,.2f}" if r["orb_high"] else "FORMING"
    orl_str = f"₹{r['orb_low']:,.2f}" if r["orb_low"] else "FORMING"
    print(f"{r['symbol']:<12} {r['bars_count']:<6} {orh_str:<10} {orl_str:<10} ₹{r['current_price']:<9,.2f} {r['status']}")
  print("=" * 65)
  return 0


if __name__ == "__main__":
  sys.exit(main())
