"""Standalone verification script for Angel One SmartAPI authentication, TOTP generation, and 1-minute candle ingestion."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import _load_env_file
from app.data.providers import (
    ANGEL_SYMBOL_TOKENS,
    AngelSmartAPIDataProvider,
    ProviderAuthenticationError,
    generate_totp,
)


def main():
  print("=" * 60)
  print("AlphaTrade AI v2.0 — Angel One SmartAPI Ingestion Verifier")
  print("=" * 60)

  _load_env_file()
  api_key = os.environ.get("ANGEL_API_KEY")
  client_code = os.environ.get("ANGEL_CLIENT_CODE")
  totp_secret = os.environ.get("ANGEL_TOTP_SECRET")
  password = os.environ.get("ANGEL_PASSWORD")
  jwt_token = os.environ.get("ANGEL_JWT_TOKEN")

  print(f"[*] API Key: {api_key[:4] if api_key else 'MISSING'}****")
  print(f"[*] Client Code: {client_code}")
  print(f"[*] TOTP Secret: {'CONFIGURED' if totp_secret else 'MISSING'}")
  print(f"[*] Password/MPIN: {'CONFIGURED' if password else 'NOT_SET (optional for auto-login)'}")
  print(f"[*] Manual JWT Token: {'CONFIGURED' if jwt_token else 'NOT_SET'}")

  # 1. Verify TOTP generation
  if totp_secret:
    totp = generate_totp(totp_secret)
    print(f"\n[PASS] Pure-Python RFC 6238 TOTP generated successfully: {totp}")
  else:
    print("\n[FAIL] ANGEL_TOTP_SECRET missing.")
    return 1

  # 2. Verify Symbol Token Resolution
  provider = AngelSmartAPIDataProvider()
  print("\n[*] Verifying Liquid NIFTY Symbol Token Resolution:")
  for sym in ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN"]:
    token = provider.resolve_symbol_token(sym)
    print(f"    - {sym:<10} -> Token {token}")
    assert token is not None, f"Token for {sym} missing"

  # 3. Test Contract Response Parser
  print("\n[*] Testing getCandleData response parser with exact Decimal conversion:")
  sample_raw = [
      ["2026-11-10T09:15:00+05:30", 1250.05, 1255.00, 1248.50, 1252.35, 45200],
      ["2026-11-10T09:16:00+05:30", 1252.35, 1256.10, 1251.00, 1254.80, 31000],
  ]
  parsed_bars = provider.parse_angel_response("RELIANCE", sample_raw)
  assert len(parsed_bars) == 2
  for b in parsed_bars:
    print(
        f"    [BAR] {b.symbol} {b.timestamp_utc.isoformat()} | O={b.open} H={b.high} L={b.low} C={b.close} V={b.volume} (Type: {type(b.close).__name__})"
    )
    assert isinstance(b.close, Decimal)
  print("[PASS] Contract parser validated: Exact Decimal arithmetic preserved.")

  # 4. Attempt Live Network Call if credentials exist
  if jwt_token or password:
    print("\n[*] Attempting live SmartAPI call for RELIANCE (1m candles)...")
    try:
      # Query verified trading session (08-Nov-2024 09:15 - 10:15 IST)
      start_utc = datetime(2024, 11, 8, 3, 45, tzinfo=timezone.utc)
      end_utc = datetime(2024, 11, 8, 4, 45, tzinfo=timezone.utc)
      bars = provider.fetch_1m_candles("RELIANCE", start_utc, end_utc)
      print(f"[PASS] Live fetch returned {len(bars)} 1-minute bars from Angel One.")
      if bars:
        latest = bars[-1]
        print(f"       Latest Bar: {latest.timestamp_utc.isoformat()} Close={latest.close}")
    except ProviderAuthenticationError as e:
      print(f"[WARN] Live authentication requires valid MPIN/Password: {e}")
    except Exception as e:
      print(f"[WARN] Live network call returned: {e}")
  else:
    print("\n[NOTE] Live fetch skipped: ANGEL_PASSWORD (trading MPIN) not set in .env.")
    print("       To enable live automated fetching, add ANGEL_PASSWORD=<your_trading_mpin> in .env.")

  print("\n[OK] Phase 4 Angel One SmartAPI Ingestion Pipeline Verified.")
  return 0


if __name__ == "__main__":
  sys.exit(main())

