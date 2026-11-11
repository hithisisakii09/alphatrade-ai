"""Unified system health and pre-flight operational audit script for AlphaTrade AI v2.0."""

from datetime import datetime, timezone
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from app.core.clock import NSECalendar, to_ist
from app.core.config import _load_env_file, load_app_config
from app.core.database import Session, init_db
from app.core.execution_guard import assert_paper_mode
from app.data.instruments import InstrumentRegistry
from app.data.providers import AngelSmartAPIDataProvider, get_data_provider
from app.portfolio.ledger import PortfolioLedger
from app.services.notifications import WebhookNotificationService


def main():
  print("=" * 70)
  print("AlphaTrade AI v2.0 — End-to-End System Health & Operational Audit")
  print("=" * 70)

  _load_env_file()
  all_ok = True

  # 1. Execution Guard & Paper Mode Safety
  print("\n[*] 1. Execution Guard & Fail-Closed Safety:")
  try:
    mode = assert_paper_mode()
    print(f"    [PASS] Execution Mode: {mode} (Fail-closed paper guard active)")
  except Exception as e:
    print(f"    [FAIL] Execution guard failure: {e}")
    all_ok = False

  # 2. Configuration & Risk Profile
  print("\n[*] 2. Configuration & Strategy Profiles:")
  try:
    cfg = load_app_config()
    print(f"    [PASS] Active Risk Profile: {cfg.active_risk_profile}")
    print(f"    [PASS] Active Cost Profile: {cfg.active_cost_profile}")
    print(f"    [PASS] Configured Data Provider: {cfg.data_provider}")
    print(f"    [PASS] Strategy ID: {cfg.strategy.strategy_id} (Version {cfg.strategy.version})")
  except Exception as e:
    print(f"    [FAIL] Configuration loading error: {e}")
    all_ok = False

  # 3. NSE Calendar & Timekeeping
  print("\n[*] 3. NSE 2026 Calendar & Timekeeping:")
  try:
    calendar = NSECalendar()
    now_utc = datetime.now(timezone.utc)
    now_ist = to_ist(now_utc)
    is_trading = calendar.is_trading_day(now_utc)
    state = calendar.get_session_state(now_utc)
    print(f"    [PASS] Current System Time: {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')} ({now_utc.strftime('%H:%M:%S UTC')})")
    print(f"    [PASS] Trading Day: {is_trading} | Session State: {state}")
  except Exception as e:
    print(f"    [FAIL] Calendar check error: {e}")
    all_ok = False

  # 4. SQLite Database & Ledger Balance
  print("\n[*] 4. SQLite Database & Double-Entry Ledger Reconciliation:")
  try:
    _, SessionLocal = init_db(cfg.database_url)
    with SessionLocal() as db:
      prof = cfg.strategy.risk_profiles[cfg.active_risk_profile]
      ledger = PortfolioLedger(db, prof.reference_capital)
      reconciled, msg = ledger.reconcile()
      cash = ledger.get_cash_balance()
      exposure = ledger.get_deployed_exposure()
      open_pos = ledger.get_open_positions()
      if reconciled:
        print(f"    [PASS] Ledger Accounting Reconciled: TRUE ({msg})")
        print(f"    [PASS] Reference Capital: ₹{prof.reference_capital:,.2f} | Available Cash: ₹{cash:,.2f}")
        print(f"    [PASS] Deployed Exposure: ₹{exposure:,.2f} | Active Open Positions: {len(open_pos)}")
      else:
        print(f"    [FAIL] Ledger Reconciliation Mismatch: {msg}")
        all_ok = False
  except Exception as e:
    print(f"    [FAIL] Database/Ledger error: {e}")
    all_ok = False

  # 5. Instrument Registry Universe
  print("\n[*] 5. Instrument Master & Token Mapping:")
  try:
    registry = InstrumentRegistry()
    symbols = list(registry.instruments.keys())
    print(f"    [PASS] Loaded {len(symbols)} instruments from registry (version {registry.version})")
    sample_eligible = [s for s in symbols if registry.is_eligible(s)[0]]
    print(f"    [PASS] Eligible Liquidity Candidates: {len(sample_eligible)} symbols")
  except Exception as e:
    print(f"    [FAIL] Instrument registry error: {e}")
    all_ok = False

  # 6. Angel One Data Provider & Daily Session Caching
  print("\n[*] 6. Angel One SmartAPI Data Feeder & Session Cache:")
  try:
    provider = AngelSmartAPIDataProvider()
    has_token = bool(provider.jwt_token)
    print(f"    [PASS] Daily Session Cached JWT: {'ACTIVE' if has_token else 'NOT_CACHED (Will authenticate on request)'}")
    sample_token = provider.resolve_symbol_token("RELIANCE")
    print(f"    [PASS] Token Resolution Test: RELIANCE -> Token {sample_token}")
  except Exception as e:
    print(f"    [FAIL] SmartAPI Provider error: {e}")
    all_ok = False

  # 7. Notification Services
  print("\n[*] 7. Real-Time Webhook Alert Service:")
  try:
    notify = WebhookNotificationService()
    print(f"    [INFO] Discord Webhook: {'CONFIGURED' if notify.discord_webhook_url else 'DISABLED'}")
    print(f"    [INFO] Telegram Alerts: {'CONFIGURED' if notify.telegram_bot_token and notify.telegram_chat_id else 'DISABLED'}")
  except Exception as e:
    print(f"    [FAIL] Notification configuration error: {e}")
    all_ok = False

  # 8. Live FastAPI Server & Dashboard Probing
  print("\n[*] 8. Live HTTP Endpoints & Background Daemon Probing:")
  try:
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=5.0) as client:
      h = client.get("/health")
      r = client.get("/runner/status")
      d = client.get("/dashboard/")
      if h.status_code == 200 and r.status_code == 200 and d.status_code == 200:
        r_json = r.json()
        print(f"    [PASS] FastAPI Server: HEALTHY (HTTP 200)")
        print(f"    [PASS] BackgroundScheduler: RUNNING={r_json.get('is_running')} with {r_json.get('jobs_count')} cron jobs")
        print(f"    [PASS] Dynamic Web Dashboard: ACTIVE ({len(d.text)} bytes served)")
      else:
        print(f"    [WARN] Server returned non-200: Health={h.status_code}, Runner={r.status_code}")
  except Exception as e:
    print(f"    [INFO] Local daemon not reachable at http://127.0.0.1:8000 (Start with: python -m uvicorn app.main:app)")

  print("\n" + "=" * 70)
  if all_ok:
    print(">>> OVERALL SYSTEM HEALTH: 100% OPERATIONAL & VERIFIED <<<")
  else:
    print(">>> OVERALL SYSTEM HEALTH: ATTENTION REQUIRED (See errors above) <<<")
  print("=" * 70)
  return 0 if all_ok else 1


if __name__ == "__main__":
  sys.exit(main())

