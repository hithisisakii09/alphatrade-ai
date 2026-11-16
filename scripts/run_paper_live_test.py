"""AlphaTrade AI v2.0 — Paper Testing & Autonomous Execution Verification
Executes paper trading with zero real money risk on authentic Angel One live market data
using the NANO_5K profile (Rs. 5,000 capital, low-price stocks, 5% hard stop-loss).
"""

import sys
import os
from decimal import Decimal
from datetime import datetime, timezone, timedelta

# Ensure workspace root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import load_app_config
from app.core.clock import IST, to_ist, ist_datetime
from app.core.database import init_db
from app.data.providers import AngelSmartAPIDataProvider
from app.services.daily_runner import ScheduledDailyRunner
from app.portfolio.ledger import PortfolioLedger
from app.execution.paper_broker import PaperBroker
from app.market.indicators import compute_opening_range, compute_session_vwap

def run_live_paper_test():
    print("======================================================================")
    print(" ALPHATRADE AI v2.0 — REAL-TIME PAPER TRADING VALIDATION (NANO_5K)")
    print("======================================================================")
    
    # 1. Load configuration
    cfg = load_app_config(risk_profile_name="NANO_5K")
    print(f"[*] Execution Mode       : {cfg.execution_mode} (Fail-Closed Safety Active)")
    print(f"[*] Active Risk Profile  : {cfg.active_risk_profile}")
    prof = cfg.strategy.risk_profiles[cfg.active_risk_profile]
    print(f"[*] Reference Capital    : Rs. {prof.reference_capital:,.2f}")
    print(f"[*] Hard Stop-Loss       : {prof.initial_stop_pct * Decimal('100')}% below purchase value")
    print(f"[*] Max Capital Deployed : {prof.max_capital_deployed_pct * Decimal('100')}% (Rs. {prof.reference_capital * prof.max_capital_deployed_pct:,.2f})")
    print(f"[*] Max Open Positions   : {prof.max_open_positions}")
    print(f"[*] Daily Loss Limit     : Rs. {prof.reference_capital * prof.max_daily_loss_pct:,.2f} ({prof.max_daily_loss_pct * Decimal('100')}%)")
    
    # 2. Authenticate and fetch today's live session candles
    print("\n[*] Connecting to Angel One SmartAPI live market data feed...")
    provider = AngelSmartAPIDataProvider()
    provider.login()
    print("[PASS] Angel One session authenticated successfully.")
    
    now_utc = datetime.now(timezone.utc)
    dt_ist = to_ist(now_utc)
    session_date = dt_ist.strftime("%Y-%m-%d")
    market_open_utc = ist_datetime(dt_ist.year, dt_ist.month, dt_ist.day, 9, 15)
    
    symbols_universe = ["TATASTEEL", "NTPC", "POWERGRID", "TATAMOTORS", "WIPRO", "ITC"]
    print(f"[*] Target Low-Price Universe ({len(symbols_universe)} symbols): {', '.join(symbols_universe)}")
    print(f"[*] Session Date         : {session_date} (Current Time: {dt_ist.strftime('%H:%M:%S')} IST)")
    
    bars_by_symbol = {}
    print("\n[*] Fetching authentic live 1m candles for today's session:")
    for sym in symbols_universe:
        import time
        time.sleep(0.4)  # respect rate limit
        bars = provider.fetch_1m_candles(sym, market_open_utc, now_utc)
        bars_by_symbol[sym] = bars
        if bars:
            first_bar = bars[0]
            last_bar = bars[-1]
            first_ist = to_ist(first_bar.timestamp_utc).strftime('%H:%M')
            last_ist = to_ist(last_bar.timestamp_utc).strftime('%H:%M')
            print(f"  - {sym:10s}: {len(bars):2d} bars ({first_ist} -> {last_ist} IST) | Open: Rs.{first_bar.open:>7.2f} | Latest: Rs.{last_bar.close:>7.2f}")
        else:
            print(f"  - {sym:10s}:  0 bars (waiting for market update)")
            
    # 3. Initialize fresh Daily Runner and Ledger
    print("\n[*] Initializing Clean Paper Trading Engine & Scheduled Runner...")
    runner = ScheduledDailyRunner(config=cfg, symbols_universe=symbols_universe)
    
    # 4. Execute 09:15 Market Open Job
    print("[*] Executing job_market_open (resets kill switch, initializes ledger)...")
    res_open = runner.job_market_open(market_open_utc)
    print(f"  -> {res_open}")
    
    # 5. Check Opening Ranges formed (09:15 - 09:30 IST)
    print("\n[*] Opening Range (09:15 - 09:30 IST) Formation & 5% Stop Analysis:")
    orb_results = {}
    for sym, bars in bars_by_symbol.items():
        if len(bars) >= 15:
            orb = compute_opening_range(bars, 15)
            vwap = compute_session_vwap(bars)
            stop_price_5pct = (orb.orb_high * Decimal("0.95")).quantize(Decimal("0.05"))
            orb_results[sym] = {
                "orb_high": orb.orb_high,
                "orb_low": orb.orb_low,
                "vwap": vwap,
                "stop_price_at_orh": stop_price_5pct,
            }
            print(f"  - {sym:10s}: ORH=Rs.{orb.orb_high:>7.2f} | ORL=Rs.{orb.orb_low:>7.2f} | VWAP=Rs.{vwap:>7.2f} | 5% Stop at Breakout=Rs.{stop_price_5pct:>7.2f}")
        else:
            print(f"  - {sym:10s}: Incomplete ORB ({len(bars)}/15 bars)")
            
    # 6. Step bar-by-bar through the session to simulate live intraday evaluation
    print("\n[*] Running chronological bar-by-bar Paper Execution across all symbols:")
    
    # Find common timestamps or iterate chronologically
    all_timestamps = sorted(list({b.timestamp_utc for bars in bars_by_symbol.values() for b in bars}))
    print(f"  -> Total chronological time steps to process: {len(all_timestamps)} bars")
    
    signals_count = 0
    orders_count = 0
    fills_count = 0
    exits_count = 0
    
    for idx, ts in enumerate(all_timestamps):
        current_bars_slice = {}
        for sym, bars in bars_by_symbol.items():
            sym_slice = [b for b in bars if b.timestamp_utc <= ts]
            if sym_slice:
                current_bars_slice[sym] = sym_slice
                
        # Call intraday tick
        tick_summary = runner.job_intraday_tick(
            now_utc=ts,
            custom_bars_by_symbol=current_bars_slice,
            symbols=symbols_universe,
        )
        
        if tick_summary.get("signals_generated"):
            for sig in tick_summary["signals_generated"]:
                signals_count += 1
                t_str = to_ist(ts).strftime("%H:%M")
                print(f"  [SIGNAL @ {t_str} IST] {sig['symbol']} Breakout above ORH {sig['orb_high']} at Rs.{sig['price']}")
                
        if tick_summary.get("orders_placed"):
            for ord_info in tick_summary["orders_placed"]:
                orders_count += 1
                t_str = to_ist(ts).strftime("%H:%M")
                print(f"  [ORDER @ {t_str} IST] BUY {ord_info['qty']} shares of {ord_info['symbol']} (Order: {ord_info['order_id']})")
                
        if tick_summary.get("orders_filled"):
            for fill in tick_summary["orders_filled"]:
                fills_count += 1
                t_str = to_ist(ts).strftime("%H:%M")
                fill_p = Decimal(fill['fill_price'])
                stop_5pct = (fill_p * Decimal("0.95")).quantize(Decimal("0.05"))
                trail_act = (fill_p * Decimal("1.03")).quantize(Decimal("0.05"))
                print(f"  [FILL @ {t_str} IST] EXECUTED {fill['symbol']} @ Rs.{fill_p:.2f} | 5% Hard Stop: Rs.{stop_5pct:.2f} | Trailing Act: Rs.{trail_act:.2f}")
                
        if tick_summary.get("positions_closed"):
            for closed in tick_summary["positions_closed"]:
                exits_count += 1
                t_str = to_ist(ts).strftime("%H:%M")
                print(f"  [EXIT @ {t_str} IST] CLOSED {closed['symbol']} @ Rs.{closed['exit_price']} | Reason: {closed['reason']}")

    # 7. Final Portfolio Reconciliation
    print("\n[*] Performing End-of-Test Ledger Reconciliation...")
    _, SessionLocal = init_db(cfg.database_url)
    with SessionLocal() as db:
        ledger = PortfolioLedger(db, prof.reference_capital)
        reconciled, msg = ledger.reconcile()
        snap = ledger.take_snapshot(now_utc)
        open_positions = ledger.get_open_positions()
        
        print("\n======================================================================")
        print(" FINAL PAPER PORTFOLIO LEDGER SUMMARY (ZERO REAL MONEY USED)")
        print("======================================================================")
        print(f" Starting Reference Capital : Rs. {prof.reference_capital:,.2f}")
        print(f" Ending Cash Balance        : Rs. {snap.cash_balance_inr:,.2f}")
        print(f" Deployed Exposure          : Rs. {snap.deployed_exposure_inr:,.2f}")
        print(f" Unrealized MTM P&L         : Rs. {snap.unrealized_pnl_inr:,.2f}")
        print(f" Realized P&L               : Rs. {snap.realized_pnl_inr:,.2f}")
        print(f" Total Transaction Fees     : Rs. {snap.total_fees_inr:,.2f}")
        print(f" Net Portfolio Equity       : Rs. {snap.net_equity_inr:,.2f}")
        print(f" Ledger Reconciled Status   : {'[PASS] RECONCILED OK' if reconciled else '[FAIL] MISMATCH'} ({msg})")
        print(f" Open Positions Count       : {len(open_positions)}")
        for pos in open_positions:
            entry = Decimal(pos.entry_price)
            curr_stop = Decimal(pos.current_stop_price)
            stop_drop_pct = ((entry - curr_stop) / entry * Decimal("100")).quantize(Decimal("0.01"))
            print(f"   -> {pos.symbol}: Qty={pos.quantity} | Entry=Rs.{entry:.2f} | Current Stop=Rs.{curr_stop:.2f} (-{stop_drop_pct}%) | Status={pos.status}")
        print(f" Signals Generated          : {signals_count}")
        print(f" Orders Submitted           : {orders_count}")
        print(f" Fills Executed             : {fills_count}")
        print(f" Exits Completed            : {exits_count}")
        print("======================================================================")
        
    return {
        "reconciled": reconciled,
        "signals": signals_count,
        "orders": orders_count,
        "fills": fills_count,
        "exits": exits_count,
        "ending_cash": str(snap.cash_balance_inr),
        "net_equity": str(snap.net_equity_inr),
        "realized_pnl": str(snap.realized_pnl_inr),
    }

if __name__ == "__main__":
    run_live_paper_test()
