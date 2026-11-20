"""Live market scan - ORB status for all tracked symbols."""
import httpx
import json
from datetime import datetime

BASE = "http://127.0.0.1:8080"
SYMBOLS = ["TATASTEEL", "NTPC", "POWERGRID", "WIPRO", "ITC", "TATAMOTORS"]

def main():
    now = datetime.now().strftime("%H:%M:%S IST")
    print(f"\n{'='*62}")
    print(f"  ALPHATRADE AI — LIVE MARKET SCAN @ {now}")
    print(f"{'='*62}\n")

    # Health check
    h = httpx.get(f"{BASE}/health", timeout=5).json()
    print(f"[SERVER] Mode={h.get('execution_mode')}  Watchdog={h['watchdog']['status']}  "
          f"Last tick: {h['watchdog']['seconds_since_last_tick']:.0f}s ago\n")

    # Portfolio
    pf = httpx.get(f"{BASE}/portfolio/summary", timeout=5).json()
    cash = pf.get("cash_balance_inr", pf.get("cash_balance", "N/A"))
    deployed = pf.get("deployed_capital_inr", pf.get("deployed_capital", 0))
    print(f"[PORTFOLIO] Cash=Rs.{cash}  Deployed=Rs.{deployed}  Net=Rs.{float(str(cash))+float(str(deployed)):.2f}\n")

    # Orders
    orders = httpx.get(f"{BASE}/orders", timeout=5).json()
    order_list = orders.get("orders", [])
    print(f"[ORDERS] Total today: {len(order_list)}")
    if order_list:
        for o in order_list:
            print(f"  → {o.get('symbol')} {o.get('transaction_type')} qty={o.get('quantity')} "
                  f"price={o.get('price')} status={o.get('status')}")
    else:
        print("  No orders placed yet")

    # Positions
    pos = httpx.get(f"{BASE}/positions", timeout=5).json()
    pos_list = pos.get("positions", [])
    print(f"\n[POSITIONS] Open: {len(pos_list)}")
    if pos_list:
        for p in pos_list:
            print(f"  → {p.get('symbol')} qty={p.get('quantity')} avg={p.get('average_price')} "
                  f"pnl={p.get('unrealised_pnl', p.get('pnl', 'N/A'))}")
    else:
        print("  No open positions")

    # Candle + ORB scan
    print(f"\n[ORB SCAN — 15-MIN CANDLES]")
    for sym in SYMBOLS:
        try:
            r = httpx.get(f"{BASE}/market/candles/{sym}?interval=FIFTEEN_MINUTE&limit=10", timeout=10)
            d = r.json()
            candles = d.get("candles", [])
            if candles:
                orb = candles[0]  # first 15-min bar = ORB
                latest = candles[-1]
                orb_high = float(orb.get("high", 0))
                orb_low = float(orb.get("low", 0))
                ltp = float(latest.get("close", 0))
                vol = latest.get("volume", "?")
                status = ""
                if ltp > orb_high:
                    status = "🚀 BREAKOUT ABOVE ORH!"
                elif ltp < orb_low:
                    status = "🔴 BREAKDOWN below ORL"
                else:
                    gap_to_orh = orb_high - ltp
                    pct = (gap_to_orh / orb_high) * 100
                    status = f"⏳ Inside ORB — {gap_to_orh:.2f} ({pct:.2f}%) below ORH"
                print(f"  {sym:12s} LTP={ltp:.2f}  ORH={orb_high:.2f}  ORL={orb_low:.2f}  {status}")
            else:
                print(f"  {sym:12s} No candle data")
        except Exception as ex:
            print(f"  {sym:12s} ERROR: {ex}")

    # Regime
    print(f"\n[MARKET REGIME]")
    for sym in ["TATASTEEL", "NTPC", "POWERGRID"]:
        try:
            r = httpx.get(f"{BASE}/market/regime?symbol={sym}&lookback_bars=30", timeout=8)
            d = r.json()
            regime = d.get("regime", "?")
            bull = d.get("bull_prob", 0)
            bear = d.get("bear_prob", 0)
            print(f"  {sym:12s} Regime={regime}  Bull%={bull:.1%}  Bear%={bear:.1%}")
        except Exception as ex:
            print(f"  {sym}: {ex}")

    # Next scheduler events
    print(f"\n[SCHEDULER — NEXT EVENTS]")
    try:
        sched = httpx.get(f"{BASE}/scheduler/jobs", timeout=5).json()
        jobs = sched.get("jobs", [])
        for j in jobs:
            name = j.get("id", j.get("name", "?"))
            nxt = j.get("next_run_time", "?")
            if any(k in name for k in ["tick", "cutoff", "liquid", "close"]):
                print(f"  {name} → {nxt}")
    except Exception as ex:
        print(f"  Could not fetch scheduler: {ex}")

    print(f"\n{'='*62}")
    print("  Engine is LIVE. Capital protected until ORH confirmed.")
    print(f"{'='*62}\n")

if __name__ == "__main__":
    main()
