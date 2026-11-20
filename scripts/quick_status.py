"""Quick live status snapshot."""
import httpx
import json
from datetime import datetime

BASE = "http://127.0.0.1:8080"

def main():
    now = datetime.now().strftime("%H:%M:%S IST")
    print(f"\n{'='*60}")
    print(f"  ALPHATRADE AI — STATUS @ {now}")
    print(f"{'='*60}")

    # Health
    h = httpx.get(f"{BASE}/health", timeout=5).json()
    wd = h.get("watchdog", {})
    last_tick_s = wd.get("seconds_since_last_tick")
    tick_str = f"{last_tick_s:.0f}s ago" if last_tick_s is not None else "pre-session (starts 09:00 IST)"
    print(f"\n[SERVER]")
    print(f"  Mode    : {h.get('execution_mode')}")
    print(f"  Watchdog: {wd.get('status')}  (last tick: {tick_str})")
    print(f"  Window  : {'OPEN' if wd.get('active_window') else 'CLOSED (Pre-Market)'}")

    # Portfolio
    pf = httpx.get(f"{BASE}/portfolio", timeout=5).json()
    print(f"\n[PORTFOLIO]")
    print(f"  Cash     : Rs.{pf.get('cash_balance_inr', 'N/A')}")
    print(f"  Deployed : Rs.{pf.get('deployed_exposure_inr', '0.00')}")
    print(f"  PnL      : Rs.{pf.get('unrealized_pnl_inr', '0.00')}")
    print(f"  Net Eq   : Rs.{pf.get('net_equity_inr', 'N/A')}")

    # Orders
    orders = httpx.get(f"{BASE}/orders", timeout=5).json().get("orders", [])
    print(f"\n[ORDERS] — {len(orders)} placed today")
    for o in orders:
        print(f"  {o.get('symbol')} | {o.get('transaction_type')} | qty={o.get('quantity')} | "
              f"price={o.get('price')} | status={o.get('status')}")
    if not orders:
        print("  None yet — engine waiting for ORH breakout + RVOL>=1.75")

    # Positions
    pos = httpx.get(f"{BASE}/positions", timeout=5).json().get("positions", [])
    print(f"\n[POSITIONS] — {len(pos)} open")
    for p in pos:
        print(f"  {p.get('symbol')} | qty={p.get('quantity')} | avg={p.get('average_price')} | "
              f"pnl={p.get('unrealised_pnl', p.get('pnl', 'N/A'))}")
    if not pos:
        print("  None")

    # Ensemble regime
    syms = ["TATASTEEL", "NTPC", "POWERGRID", "WIPRO", "ITC", "TATAMOTORS"]
    print(f"\n[REGIME / SIGNAL STRENGTH]")
    for sym in syms:
        try:
            r = httpx.get(f"{BASE}/strategy/ensemble/signals?symbol={sym}", timeout=8).json()
            regime = r.get("regime", "?")
            orb_w = r.get("dynamic_weights", {}).get("ORB_TREND_V2", "?")
            icon = "BULL " if regime == "BULL" else ("BEAR " if regime == "BEAR" else "NEUT ")
            print(f"  {icon} {sym:12s}  ORB_weight={orb_w}  orders={r.get('orders_count',0)}")
        except Exception as e:
            print(f"  {sym}: {e}")

    # Scheduler
    runner = httpx.get(f"{BASE}/runner/status", timeout=5).json()
    jobs = runner.get("jobs", [])
    print(f"\n[SCHEDULER] — {len(jobs)} jobs active")
    key_jobs = ["nse_intraday_tick", "nse_entry_cutoff_1430", "nse_eod_liquidation_1500"]
    for j in jobs:
        if j.get("id") in key_jobs:
            print(f"  {j.get('id'):30s} next={j.get('next_run_time','?')}")

    print(f"\n{'='*60}")
    print(f"  Entry window closes at 14:30 IST")
    print(f"  Auto-liquidation at 15:00 IST")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
