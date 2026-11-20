"""AlphaTrade AI — Full System Status Check"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx, json

BASE = "http://127.0.0.1:8080"

def get(path):
    try:
        return httpx.get(BASE + path, timeout=5).json()
    except Exception as e:
        return {"error": str(e)}

h    = get("/health")
p    = get("/portfolio")
pos  = get("/positions")
ords = get("/orders")
sig  = get("/signals")
sched = get("/runner/status")
gw   = get("/execution/gateway/status")

print("=" * 60)
print("  ALPHATRADE AI — FULL SYSTEM STATUS @ 09:28 IST")
print("=" * 60)

print("\n[1] EXECUTION & HEALTH")
print(f"    Mode           : {h.get('execution_mode','?')}")
print(f"    Version        : {h.get('version','?')}")
print(f"    Watchdog       : {h.get('watchdog',{}).get('status','?')}")
print(f"    Active Window  : {h.get('watchdog',{}).get('active_window','?')}")

print("\n[2] LIVE GATEWAY")
print(f"    Default GW     : {gw.get('default_gateway','?')}")
for g in gw.get("gateways", []):
    name = g.get("name","?")
    proto = g.get("protocol","?")
    conn = g.get("connected","?")
    print(f"    {name} ({proto}): connected={conn}")

print("\n[3] PORTFOLIO (Real Angel One Funds)")
print(f"    Cash Balance   : Rs. {p.get('cash_balance_inr','?')}")
print(f"    Deployed       : Rs. {p.get('deployed_exposure_inr','?')}")
print(f"    Net Equity     : Rs. {p.get('net_equity_inr','?')}")
print(f"    Reconciled     : {p.get('reconciled','?')}")
cache = p.get("candle_cache", {})
print(f"    Candle Cache   : {cache.get('symbols_cached',0)} symbols, {cache.get('total_bars_cached',0)} bars, {cache.get('hit_rate_pct',0):.1f}% hit rate")

print("\n[4] OPEN POSITIONS (Live Angel One)")
if isinstance(pos, list):
    if pos:
        for p_ in pos:
            print(f"    {p_.get('symbol')} qty={p_.get('quantity')} entry=Rs.{p_.get('entry_price_inr')} unrealized=Rs.{p_.get('unrealized_pnl_inr')}")
    else:
        print("    No open positions yet")
else:
    print(f"    {pos}")

print("\n[5] ORDERS (Today's Session)")
if isinstance(ords, list):
    print(f"    Total orders placed : {len(ords)}")
    for o in ords[:5]:
        print(f"    {o.get('symbol')} {o.get('side')} qty={o.get('filled_qty')} @ Rs.{o.get('fill_price_inr','?')} status={o.get('status')}")
else:
    print(f"    {ords}")

print("\n[6] SIGNALS (Breakout Scanner)")
if isinstance(sig, list):
    print(f"    Signals generated   : {len(sig)}")
    for s in sig[:3]:
        print(f"    {s.get('symbol')} signal={s.get('signal_type','?')} price=Rs.{s.get('signal_price','?')} status={s.get('status','?')}")
elif isinstance(sig, dict):
    print(f"    {json.dumps(sig)[:300]}")

print("\n[7] SCHEDULER (APScheduler Jobs)")
if isinstance(sched, dict):
    jobs = sched.get("jobs", [])
    print(f"    Active jobs : {len(jobs)}")
    for j in jobs[:5]:
        name = j.get("id","?")
        next_run = j.get("next_run_time","scheduled")
        print(f"    {name} -> next: {next_run}")
    is_active = sched.get("is_active", sched.get("running","?"))
    print(f"    Scheduler running : {is_active}")

print("\n" + "=" * 60)
print("  MARKET STATUS: 09:15-09:30 IST = Opening Range forming")
print("  Next event: 09:30 IST - ORB breakout scan activates")
print("=" * 60)
