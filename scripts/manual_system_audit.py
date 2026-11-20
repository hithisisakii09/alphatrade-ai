"""Comprehensive Manual Audit & Health Inspection Script for AlphaTrade AI v2.0.
Verifies all routes, audits paper vs live separation, cross-checks Angel One RMS values.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import _load_env_file
_load_env_file()
import json
import httpx
from decimal import Decimal
from datetime import datetime
from app.execution.gateway import LiveAngelSmartAPIGateway

BASE_URL = "http://127.0.0.1:8080"

def run_audit():
    results = {}
    print("=" * 70)
    print("  ALPHATRADE AI v2.0 — FULL SYSTEM & ROUTE AUDIT")
    print(f"  Execution Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 70)

    # 1. Route Audit List
    routes_to_test = [
        ("GET", "/health", "Health & Core Mode"),
        ("GET", "/portfolio", "Portfolio & Cash Balance"),
        ("GET", "/orders", "Orders Book"),
        ("GET", "/positions", "Active Positions"),
        ("GET", "/runner/status", "APScheduler & Poller Status"),
        ("GET", "/execution/gateway/status", "Execution Gateway Status"),
        ("GET", "/execution/reconciliation/status", "Shadow Broker Reconciliation"),
        ("GET", "/risk/status", "Risk Engine Status"),
        ("GET", "/risk/allocation", "Equal Risk Contribution Allocation"),
        ("GET", "/risk/var", "Parametric & Historical VaR"),
        ("GET", "/risk/margin-stress", "Margin Stress & Headroom Gate"),
        ("GET", "/surveillance/drift", "Execution Drift & KS-Test"),
        ("GET", "/market/status", "Market Hours & Ingestion Status"),
        ("GET", "/market/candles-summary", "Live Cache Summary"),
        ("GET", "/strategy/ensemble/signals?symbol=TATASTEEL", "Ensemble Signals (TATASTEEL)"),
        ("GET", "/strategy/ensemble/signals?symbol=POWERGRID", "Ensemble Signals (POWERGRID)"),
        ("GET", "/reports/daily", "Daily Session Report"),
        ("GET", "/reports/analytics", "Analytics & Alpha Metrics"),
        ("GET", "/system/preflight/status", "M30 Pre-Flight Gate Status"),
        ("GET", "/system/recovery/status", "M28 Disaster Recovery Status"),
    ]

    route_status = []
    for method, path, desc in routes_to_test:
        url = f"{BASE_URL}{path}"
        try:
            r = httpx.get(url, timeout=10)
            status_code = r.status_code
            is_ok = status_code == 200
            data = r.json() if is_ok else None
            route_status.append({
                "path": path,
                "desc": desc,
                "status_code": status_code,
                "ok": is_ok,
                "data_preview": str(data)[:120] if data else str(r.text)[:120]
            })
            print(f"[{'PASS' if is_ok else 'FAIL'}] {status_code} {path:<40} - {desc}")
        except Exception as e:
            route_status.append({
                "path": path,
                "desc": desc,
                "status_code": 0,
                "ok": False,
                "data_preview": f"Error: {e}"
            })
            print(f"[FAIL] ERR {path:<40} - {e}")

    results["routes"] = route_status

    # 2. Genuine Broker Check: Compare API Portfolio against direct Angel One RMS
    print("\n" + "-" * 70)
    print("  GENUINE BROKER VALUE CROSS-CHECK (ANGEL ONE SMARTAPI)")
    print("-" * 70)
    broker_audit = {}
    try:
        gw = LiveAngelSmartAPIGateway()
        if not gw.is_connected():
            gw.connect()
        rms_data = gw.get_margins()
        broker_cash = rms_data.available_cash
        broker_collateral = rms_data.collateral
        broker_used = rms_data.used_margin
        broker_net = rms_data.total_equity

        # Fetch /portfolio from API
        p_resp = httpx.get(f"{BASE_URL}/portfolio", timeout=5).json()
        api_cash = Decimal(str(p_resp.get("cash_balance_inr", "0.00")))
        api_deployed = Decimal(str(p_resp.get("deployed_exposure_inr", "0.00")))
        api_net = Decimal(str(p_resp.get("net_equity_inr", "0.00")))

        # Fetch broker positions
        broker_positions = gw.get_positions()
        pos_resp = httpx.get(f"{BASE_URL}/positions", timeout=5).json().get("positions", [])

        # Fetch broker orders
        broker_orders = gw.get_order_status("ALL") # or check order book
        order_resp = httpx.get(f"{BASE_URL}/orders", timeout=5).json().get("orders", [])

        broker_audit = {
            "angel_available_cash": str(broker_cash),
            "angel_utilized_margin": str(broker_used),
            "api_reported_cash": str(api_cash),
            "api_reported_deployed": str(api_deployed),
            "cash_match": broker_cash == api_cash,
            "broker_open_positions_count": len(broker_positions),
            "api_open_positions_count": len(pos_resp),
            "positions_match": len(broker_positions) == len(pos_resp),
            "api_orders_count": len(order_resp)
        }
        print(f"Angel One Real RMS Available Cash : Rs. {broker_cash}")
        print(f"FastAPI /portfolio Cash Reported : Rs. {api_cash}")
        print(f"Cash Exact Match                  : {'MATCH (GENUINE)' if broker_audit['cash_match'] else 'MISMATCH'}")
        print(f"Angel One Real Open Positions    : {len(broker_positions)}")
        print(f"FastAPI /positions Count         : {len(pos_resp)}")
        print(f"Positions Exact Match            : {'MATCH (GENUINE)' if broker_audit['positions_match'] else 'MISMATCH'}")
    except Exception as e:
        broker_audit = {"error": str(e)}
        print(f"Broker check error: {e}")

    results["broker_audit"] = broker_audit

    # 3. Paper Trading Detection
    print("\n" + "-" * 70)
    print("  PAPER TRADING LEAKAGE DETECTION")
    print("-" * 70)
    health = httpx.get(f"{BASE_URL}/health", timeout=5).json()
    gw_status = httpx.get(f"{BASE_URL}/execution/gateway/status", timeout=5).json()
    
    mode = health.get("execution_mode")
    default_gw = gw_status.get("default_gateway")
    
    is_strictly_live = (mode == "LIVE") and (default_gw == "ANGEL_LIVE")
    leakage_check = {
        "execution_mode": mode,
        "default_gateway": default_gw,
        "is_strictly_live": is_strictly_live,
        "paper_active_as_primary": default_gw == "PAPER_INTERNAL"
    }
    print(f"System Execution Mode : {mode}")
    print(f"Default Active Gateway: {default_gw}")
    print(f"Strict Live Routing   : {'VERIFIED (No paper order routing)' if is_strictly_live else 'WARNING: Paper leakage detected'}")
    results["leakage_check"] = leakage_check

    # Save results to a json file
    with open("scratch/manual_audit_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n" + "=" * 70)
    print("  AUDIT COMPLETE: All results stored in scratch/manual_audit_results.json")
    print("=" * 70)

if __name__ == "__main__":
    run_audit()
