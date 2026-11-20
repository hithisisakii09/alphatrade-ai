import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ALPHATRADE_EXECUTION_MODE"] = "LIVE"
os.environ["ALPHATRADE_LIVE_CONFIRMATION"] = "I_ACCEPT_FINANCIAL_RISK"

from app.core.config import _load_env_file
_load_env_file()
from app.data.providers import AngelSmartAPIDataProvider
from app.core.clock import IST, ist_datetime, to_ist
from datetime import datetime, timezone
import time

provider = AngelSmartAPIDataProvider()
provider.login()
now_utc = datetime.now(timezone.utc)
dt_ist = to_ist(now_utc)
start_utc = ist_datetime(dt_ist.year, dt_ist.month, dt_ist.day, 9, 15)

sector_map = {
    "POWER & ENERGY": ["POWERGRID", "NTPC", "BPCL"],
    "PSU BANKING": ["SBIN", "BANKBARODA", "PNB"],
    "PRIVATE BANKING": ["AXISBANK", "ICICIBANK", "HDFCBANK"],
    "METALS": ["TATASTEEL", "JSWSTEEL", "HINDALCO"],
    "AUTO": ["TATAMOTORS", "MARUTI"],
    "IT & TECH": ["TCS", "INFY", "WIPRO"],
    "FMCG": ["ITC", "HINDUNILVR"],
    "DEFENCE & CAPITAL GOODS": ["LT", "BEL"]
}

print("=" * 80)
print(f"  NSE SECTOR MOMENTUM & BULLISH AUDIT @ {dt_ist.strftime('%H:%M:%S')} IST")
print("  Live Data Source: Angel One SmartAPI Feed")
print("=" * 80)

sector_scores = {}

for sector, symbols in sector_map.items():
    gains = []
    bullish_count = 0
    total_evaluated = 0
    sym_details = []
    
    for sym in symbols:
        time.sleep(0.35)
        try:
            bars = provider.fetch_1m_candles(sym, start_utc, now_utc)
            if len(bars) >= 15:
                orb_bars = bars[:15]
                orh = max(b.high for b in orb_bars)
                latest = bars[-1]
                op = bars[0].open
                pct_from_open = ((latest.close - op) / op) * 100
                diff_orh = ((latest.close - orh) / orh) * 100
                gains.append(float(pct_from_open))
                total_evaluated += 1
                if diff_orh > -0.40 or latest.close >= orh:
                    bullish_count += 1
                sym_details.append(f"{sym}: {float(pct_from_open):+.2f}% (ORH: {float(diff_orh):+.2f}%)")
        except Exception as e:
            pass
            
    if gains:
        avg_gain = sum(gains) / len(gains)
        sector_scores[sector] = {
            "avg_gain": avg_gain,
            "bullish_ratio": f"{bullish_count}/{total_evaluated}",
            "symbols": ", ".join(sym_details)
        }

sorted_sectors = sorted(sector_scores.items(), key=lambda x: x[1]["avg_gain"], reverse=True)

for rank, (sec, data) in enumerate(sorted_sectors, 1):
    avg_g = data["avg_gain"]
    if rank == 1 or avg_g > 0.15:
        tag = "STRONG BULLISH LEADER"
    elif avg_g >= -0.20:
        tag = "MILDLY POSITIVE / RANGEBOUND"
    else:
        tag = "BEARISH / LAGGARD"
    print(f"#{rank} {sec:<24} | Avg Change: {avg_g:+.2f}% | Near ORH: {data['bullish_ratio']:<4} | {tag}")
    print(f"    Stocks: {data['symbols']}")
    print("-" * 80)
