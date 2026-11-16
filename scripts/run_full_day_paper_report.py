"""Complete Paper Trading Audit & Execution Report for Today's Live Session (16 Nov 2026).
Simulates full chronological bar-by-bar execution using authentic live market data from Angel One SmartAPI.
Provides detailed trade counts, purchase value, sale value, fee breakdown, and net profit.
"""

import sys
import os
import time
from decimal import Decimal
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import load_app_config
from app.core.clock import IST, to_ist, ist_datetime
from app.core.database import init_db
from app.data.providers import AngelSmartAPIDataProvider
from app.services.daily_runner import ScheduledDailyRunner
from app.portfolio.ledger import PortfolioLedger
from app.market.indicators import compute_opening_range, compute_session_vwap

def run_full_session():
    cfg = load_app_config(risk_profile_name="NANO_5K")
    prof = cfg.strategy.risk_profiles["NANO_5K"]
    
    print("=" * 80)
    print("  ALPHATRADE AI v2.0 — COMPLETE TODAY'S REAL-MARKET PAPER TRADING REPORT")
    print("=" * 80)
    print(f" Execution Date      : {datetime.now(IST).strftime('%d %B %Y')}")
    print(f" Session Elapsed     : 09:15 IST -> {datetime.now(IST).strftime('%H:%M:%S IST')}")
    print(f" Risk Profile        : {cfg.active_risk_profile} (Capital: Rs. {prof.reference_capital:,.2f})")
    print(f" Max Capital Deployed: {prof.max_capital_deployed_pct * 100}% (Rs. {prof.reference_capital * prof.max_capital_deployed_pct:,.2f})")
    print(f" Max Open Positions  : {prof.max_open_positions}")
    print(f" Hard Stop-Loss Rate : {prof.initial_stop_pct * 100}% below purchase value")
    print(f" Trailing Stop Rules : Activates at +{prof.trailing_activation_pct * 100}%, trails by {prof.trailing_drop_pct * 100}%")
    print(f" Execution Guard     : {cfg.execution_mode} (Strict Paper Mode - Zero Financial Risk)")
    print("-" * 80)

    # 1. Fetch all live candles with rate-limiting safety
    provider = AngelSmartAPIDataProvider()
    provider.login()
    
    symbols = ["TATASTEEL", "NTPC", "POWERGRID", "TATAMOTORS", "WIPRO", "ITC"]
    now_utc = datetime.now(timezone.utc)
    market_open_utc = ist_datetime(to_ist(now_utc).year, to_ist(now_utc).month, to_ist(now_utc).day, 9, 15)
    
    bars_by_symbol = {}
    print("\n[*] Ingesting authentic 1-minute live candles from Angel One SmartAPI:")
    for sym in symbols:
        time.sleep(1.0) # safe inter-call spacing
        bars = provider.fetch_1m_candles(sym, market_open_utc, now_utc)
        if not bars:
            time.sleep(1.5)
            bars = provider.fetch_1m_candles(sym, market_open_utc, now_utc)
        bars_by_symbol[sym] = bars
        if bars:
            o_p = bars[0].open
            c_p = bars[-1].close
            chg = ((c_p - o_p) / o_p * Decimal("100")).quantize(Decimal("0.01"))
            print(f"  -> {sym:10s}: {len(bars):2d} bars | Open: Rs.{o_p:>7.2f} | Latest: Rs.{c_p:>7.2f} ({chg:>+6.2f}%)")
        else:
            print(f"  -> {sym:10s}: 0 bars")
            
    # 2. Reset operational DB for clean single-session run
    from scripts.reset_operational_data import tables_to_clear
    import sqlite3
    conn = sqlite3.connect(cfg.database_url.replace("sqlite:///", ""))
    cur = conn.cursor()
    for t in tables_to_clear:
        cur.execute(f"DELETE FROM [{t}]")
    conn.commit()
    conn.close()
    
    # 3. Initialize Runner and Execute Session
    runner = ScheduledDailyRunner(config=cfg, symbols_universe=symbols)
    runner.job_market_open(market_open_utc)
    
    all_ts = sorted(list({b.timestamp_utc for bars in bars_by_symbol.values() for b in bars}))
    
    trade_events = []
    
    for ts in all_ts:
        curr_slice = {}
        for sym in symbols:
            s = [b for b in bars_by_symbol.get(sym, []) if b.timestamp_utc <= ts]
            curr_slice[sym] = s
        summary = runner.job_intraday_tick(now_utc=ts, custom_bars_by_symbol=curr_slice, symbols=symbols)
        
        for sig in summary.get("signals_generated", []):
            trade_events.append({"type": "SIGNAL", "time": to_ist(ts).strftime("%H:%M"), **sig})
        for ord_info in summary.get("orders_placed", []):
            trade_events.append({"type": "ORDER", "time": to_ist(ts).strftime("%H:%M"), **ord_info})
        for fill in summary.get("orders_filled", []):
            trade_events.append({"type": "FILL", "time": to_ist(ts).strftime("%H:%M"), **fill})
        for ex in summary.get("positions_closed", []):
            trade_events.append({"type": "EXIT", "time": to_ist(ts).strftime("%H:%M"), **ex})

    # 4. Final Ledger & Trade Accounting
    _, SessionLocal = init_db(cfg.database_url)
    with SessionLocal() as db:
        ledger = PortfolioLedger(db, prof.reference_capital)
        reconciled, msg = ledger.reconcile()
        
        mark_prices = {sym: bars[-1].close for sym, bars in bars_by_symbol.items() if bars}
        snap = ledger.take_snapshot(now_utc, mark_prices=mark_prices)
        
        from app.core.database import PositionRecord, OrderRecord, FillRecord
        positions = db.query(PositionRecord).all()
        orders = db.query(OrderRecord).all()
        fills = db.query(FillRecord).all()
        
        print("\n" + "=" * 80)
        print("  DETAILED TRADE-BY-TRADE AUDIT & P&L BREAKDOWN")
        print("=" * 80)
        
        total_purchase_value = Decimal("0.00")
        total_current_or_sale_value = Decimal("0.00")
        total_gross_pnl = Decimal("0.00")
        total_fees = Decimal("0.00")
        
        if not positions:
            print("  No trades were entered during this session.")
        else:
            for idx, pos in enumerate(positions, 1):
                qty = pos.quantity
                entry_p = Decimal(str(pos.entry_price))
                curr_p = mark_prices.get(pos.symbol, entry_p)
                purchase_val = (entry_p * qty).quantize(Decimal("0.01"))
                
                if pos.status == "CLOSED":
                    exit_p = Decimal(str(pos.exit_price))
                    sale_val = (exit_p * qty).quantize(Decimal("0.01"))
                    gross_pnl = sale_val - purchase_val
                    status_str = f"CLOSED ({pos.exit_reason})"
                    eff_p = exit_p
                    eff_val = sale_val
                else:
                    eff_p = curr_p
                    eff_val = (eff_p * qty).quantize(Decimal("0.01"))
                    gross_pnl = eff_val - purchase_val
                    status_str = "OPEN (Active)"
                    
                total_purchase_value += purchase_val
                total_current_or_sale_value += eff_val
                total_gross_pnl += gross_pnl
                
                # Transaction fees calculation
                # Delivery/Intraday NSE fees
                stt = (eff_val * Decimal("0.00025")).quantize(Decimal("0.01"))
                ex_trn = ((purchase_val + eff_val) * Decimal("0.0000345")).quantize(Decimal("0.01"))
                sebi = ((purchase_val + eff_val) * Decimal("0.000001")).quantize(Decimal("0.01"))
                stamp = (purchase_val * Decimal("0.00003")).quantize(Decimal("0.01"))
                gst = ((ex_trn + sebi) * Decimal("0.18")).quantize(Decimal("0.01"))
                trade_fees = stt + ex_trn + sebi + stamp + gst
                total_fees += trade_fees
                
                net_pnl = gross_pnl - trade_fees
                ret_on_trade = (net_pnl / purchase_val * Decimal("100")).quantize(Decimal("0.01"))
                ret_on_cap = (net_pnl / prof.reference_capital * Decimal("100")).quantize(Decimal("0.01"))
                
                stop_p = Decimal(str(pos.current_stop_price))
                stop_dist = ((entry_p - stop_p) / entry_p * Decimal("100")).quantize(Decimal("0.01"))
                
                print(f"  TRADE #{idx:02d}: {pos.symbol} (Side: BUY -> {status_str})")
                print(f"    - Quantity            : {qty} shares")
                print(f"    - Purchase Price      : Rs. {entry_p:,.2f}")
                print(f"    - Total Purchase Value: Rs. {purchase_val:,.2f}")
                print(f"    - Current/Sale Price  : Rs. {eff_p:,.2f}")
                print(f"    - Current/Sale Value  : Rs. {eff_val:,.2f}")
                print(f"    - Hard Stop-Loss (-5%): Rs. {stop_p:,.2f} (-{stop_dist}%)")
                print(f"    - Trailing Activation : Rs. {Decimal(str(pos.trailing_activation_price)):,.2f} (+3.00%)")
                print(f"    - Trailing State      : {'ACTIVATED' if pos.trailing_activated else 'WAITING'}")
                print(f"    - Gross P&L           : {'+' if gross_pnl >= 0 else ''}Rs. {gross_pnl:,.2f}")
                print(f"    - Estimated Fees (NSE): Rs. {trade_fees:,.2f} (STT: {stt}, Turnover: {ex_trn}, GST: {gst})")
                print(f"    - Net Profit / Loss   : {'+' if net_pnl >= 0 else ''}Rs. {net_pnl:,.2f}")
                print(f"    - Return on Trade     : {'+' if ret_on_trade >= 0 else ''}{ret_on_trade}%")
                print(f"    - Return on Rs.5,000  : {'+' if ret_on_cap >= 0 else ''}{ret_on_cap}%")
                print("-" * 80)
                
        total_net_profit = total_gross_pnl - total_fees
        ret_total = (total_net_profit / prof.reference_capital * Decimal("100")).quantize(Decimal("0.01"))
        
        print("\n" + "=" * 80)
        print("  SESSION SUMMARY & METRICS")
        print("=" * 80)
        print(f"  Total Trades Executed       : {len(positions)} trade(s)")
        print(f"  Total Orders Placed         : {len(orders)}")
        print(f"  Total Fills Completed       : {len(fills)}")
        print(f"  Total Purchase Value        : Rs. {total_purchase_value:,.2f}")
        print(f"  Total Current / Sale Value  : Rs. {total_current_or_sale_value:,.2f}")
        print(f"  Total Gross Profit          : {'+' if total_gross_pnl >= 0 else ''}Rs. {total_gross_pnl:,.2f}")
        print(f"  Total Transaction Costs/Fees: Rs. {total_fees:,.2f}")
        print(f"  TOTAL NET PROFIT (AFTER TAX): {'+' if total_net_profit >= 0 else ''}Rs. {total_net_profit:,.2f}")
        print(f"  NET RETURN ON CAPITAL (Rs.5K): {'+' if ret_total >= 0 else ''}{ret_total}%")
        print(f"  Starting Capital            : Rs. {prof.reference_capital:,.2f}")
        print(f"  Available Cash Balance      : Rs. {snap.cash_balance_inr:,.2f}")
        print(f"  Deployed Position Exposure  : Rs. {snap.deployed_exposure_inr:,.2f}")
        print(f"  Net Portfolio Equity        : Rs. {snap.net_equity_inr:,.2f}")
        print(f"  Ledger Reconciliation Status: {'[PASS] 100% RECONCILED OK' if reconciled else '[FAIL] MISMATCH'}")
        print("=" * 80)

if __name__ == "__main__":
    run_full_session()
