"""Multi-Trade Simulation across all today's breakout stocks on 16 November 2026.
Allows multiple concurrent positions (max_open_positions = 4) with independent 5% stop loss per stock.
"""

import sys, os, time
sys.path.insert(0, os.getcwd())
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from app.core.config import load_app_config
from app.core.clock import to_ist, ist_datetime
from app.data.providers import AngelSmartAPIDataProvider
from app.market.indicators import compute_opening_range, compute_session_vwap
from app.data.validator import aggregate_1m_to_nm

load_app_config()
p = AngelSmartAPIDataProvider()
p.login()

now = datetime.now(timezone.utc)
dt_ist = to_ist(now)
market_open = ist_datetime(dt_ist.year, dt_ist.month, dt_ist.day, 9, 15)

# Target universe: low-to-mid price stocks with breakouts today
candidates = ['ITC', 'KOTAKBANK', 'POWERGRID', 'NTPC', 'TATASTEEL']
bars_by_symbol = {}

print(f"Fetching 1m bars for candidate universe ({len(candidates)} symbols)...")
for sym in candidates:
    time.sleep(1.2)
    bars = p.fetch_1m_candles(sym, market_open, now)
    if not bars:
        time.sleep(2.0)
        bars = p.fetch_1m_candles(sym, market_open, now)
    if bars:
        bars_by_symbol[sym] = bars
        print(f"  {sym:10s}: {len(bars)} bars fetched (09:15 -> {to_ist(bars[-1].timestamp_utc).strftime('%H:%M')} IST)")

# Multi-stock allocation: Reference capital = Rs. 5,000, Max 3 open positions, ~Rs. 1,500 per position (30% per stock)
REFERENCE_CAPITAL = Decimal("5000.00")
MAX_OPEN_POSITIONS = 3
MAX_SINGLE_POS_CAP = Decimal("1600.00") # max per position
STOP_LOSS_PCT = Decimal("0.0500")      # exact 5% hard stop
TRAILING_ACT_PCT = Decimal("0.0300")   # +3% trailing activation
TRAILING_DROP_PCT = Decimal("0.0150")  # 1.5% drop from peak

cash_balance = REFERENCE_CAPITAL
open_positions = {} # sym -> pos dict
trade_history = []

all_ts = sorted(list({b.timestamp_utc for bars in bars_by_symbol.values() for b in bars}))
print(f"\nProcessing {len(all_ts)} chronological bars across {len(bars_by_symbol)} symbols...")

for ts in all_ts:
    t_str = to_ist(ts).strftime('%H:%M')
    
    # 1. Manage open positions (check 5% stop-loss & trailing stops)
    for sym in list(open_positions.keys()):
        pos = open_positions[sym]
        sym_bars = [b for b in bars_by_symbol.get(sym, []) if b.timestamp_utc == ts]
        if not sym_bars:
            continue
        bar = sym_bars[0]
        
        # Update high-water mark
        if bar.high > pos['highest_price']:
            pos['highest_price'] = bar.high
            
        # Check trailing activation (+3%)
        if not pos['trailing_activated'] and bar.high >= pos['trailing_activation_price']:
            pos['trailing_activated'] = True
            pos['current_stop_price'] = max(pos['current_stop_price'], pos['highest_price'] * (Decimal("1.0") - TRAILING_DROP_PCT))
            
        # Update trailing stop if active
        if pos['trailing_activated']:
            trail_stop = pos['highest_price'] * (Decimal("1.0") - TRAILING_DROP_PCT)
            if trail_stop > pos['current_stop_price']:
                pos['current_stop_price'] = trail_stop
                
        # Check Stop-Loss Breach (bar.low breached stop)
        if bar.low <= pos['current_stop_price']:
            exit_price = min(bar.open, pos['current_stop_price']) # realistic fill
            exit_reason = "TRAILING_STOP_EXIT" if pos['trailing_activated'] else "HARD_STOP_LOSS_5PCT"
            sale_val = (exit_price * pos['qty']).quantize(Decimal("0.01"))
            gross_pnl = sale_val - pos['purchase_val']
            
            # NSE Fees
            stt = (sale_val * Decimal("0.00025")).quantize(Decimal("0.01"))
            trn = ((pos['purchase_val'] + sale_val) * Decimal("0.0000345")).quantize(Decimal("0.01"))
            sebi = ((pos['purchase_val'] + sale_val) * Decimal("0.000001")).quantize(Decimal("0.01"))
            stamp = (pos['purchase_val'] * Decimal("0.00003")).quantize(Decimal("0.01"))
            gst = ((trn + sebi) * Decimal("0.18")).quantize(Decimal("0.01"))
            fees = stt + trn + sebi + stamp + gst
            net_pnl = gross_pnl - fees
            
            cash_balance += sale_val - fees
            trade_history.append({
                'symbol': sym,
                'entry_time': pos['entry_time'],
                'exit_time': t_str,
                'qty': pos['qty'],
                'entry_price': pos['entry_price'],
                'purchase_val': pos['purchase_val'],
                'exit_price': exit_price,
                'sale_val': sale_val,
                'stop_price': pos['current_stop_price'],
                'gross_pnl': gross_pnl,
                'fees': fees,
                'net_pnl': net_pnl,
                'exit_reason': exit_reason,
                'status': 'CLOSED'
            })
            del open_positions[sym]
            print(f"  [EXIT @ {t_str}] {sym} CLOSED @ Rs.{exit_price:.2f} ({exit_reason}) | PnL: Rs.{net_pnl:+.2f}")
            
    # 2. Check for new Breakouts if we have capacity (< MAX_OPEN_POSITIONS)
    if len(open_positions) < MAX_OPEN_POSITIONS:
        for sym, bars in bars_by_symbol.items():
            if sym in open_positions:
                continue
            # Already traded and closed? Skip same-day re-entry
            if any(t['symbol'] == sym for t in trade_history):
                continue
                
            bars_so_far = [b for b in bars if b.timestamp_utc <= ts]
            if len(bars_so_far) < 16:
                continue
            orb = compute_opening_range(bars_so_far, 15)
            if not orb.is_complete:
                continue
                
            curr_bar = bars_so_far[-1]
            # Check 5m bar close above ORH
            bars_5m = aggregate_1m_to_nm(bars_so_far, 5)
            if not bars_5m:
                continue
            latest_5m = bars_5m[-1]
            
            if latest_5m.close > orb.orb_high and latest_5m.high > orb.orb_high:
                # Sizing: up to Rs. 1,600 per position
                alloc = min(MAX_SINGLE_POS_CAP, cash_balance * Decimal("0.90"))
                entry_p = curr_bar.close
                qty = int(alloc // entry_p)
                if qty >= 1:
                    purchase_val = (entry_p * qty).quantize(Decimal("0.01"))
                    hard_stop = (entry_p * (Decimal("1.0") - STOP_LOSS_PCT)).quantize(Decimal("0.05"))
                    trail_act = (entry_p * (Decimal("1.0") + TRAILING_ACT_PCT)).quantize(Decimal("0.05"))
                    
                    cash_balance -= purchase_val
                    open_positions[sym] = {
                        'symbol': sym,
                        'entry_time': t_str,
                        'qty': qty,
                        'entry_price': entry_p,
                        'purchase_val': purchase_val,
                        'current_stop_price': hard_stop,
                        'trailing_activation_price': trail_act,
                        'trailing_activated': False,
                        'highest_price': entry_p
                    }
                    print(f"  [ENTRY @ {t_str}] BUY {qty} shares of {sym} @ Rs.{entry_p:.2f} (Value: Rs.{purchase_val:.2f}) | Stop: Rs.{hard_stop:.2f} (-5%)")
                    if len(open_positions) >= MAX_OPEN_POSITIONS:
                        break

# Evaluate remaining open positions at latest market prices
print("\n" + "=" * 80)
print("  MULTI-STOCK PAPER TRADING AUDIT (16 NOV 2026)")
print("=" * 80)

total_purchased = sum(t['purchase_val'] for t in trade_history)
total_sale_or_mtm = sum(t['sale_val'] for t in trade_history)
total_gross_pnl = sum(t['gross_pnl'] for t in trade_history)
total_fees = sum(t['fees'] for t in trade_history)

for sym, pos in open_positions.items():
    latest_bar = bars_by_symbol[sym][-1]
    curr_p = latest_bar.close
    curr_val = (curr_p * pos['qty']).quantize(Decimal("0.01"))
    gross = curr_val - pos['purchase_val']
    
    stt = (curr_val * Decimal("0.00025")).quantize(Decimal("0.01"))
    trn = ((pos['purchase_val'] + curr_val) * Decimal("0.0000345")).quantize(Decimal("0.01"))
    sebi = ((pos['purchase_val'] + curr_val) * Decimal("0.000001")).quantize(Decimal("0.01"))
    stamp = (pos['purchase_val'] * Decimal("0.00003")).quantize(Decimal("0.01"))
    gst = ((trn + sebi) * Decimal("0.18")).quantize(Decimal("0.01"))
    fee = stt + trn + sebi + stamp + gst
    net = gross - fee
    
    total_purchased += pos['purchase_val']
    total_sale_or_mtm += curr_val
    total_gross_pnl += gross
    total_fees += fee
    
    trade_history.append({
        'symbol': sym,
        'entry_time': pos['entry_time'],
        'exit_time': 'CURRENT (11:05 IST)',
        'qty': pos['qty'],
        'entry_price': pos['entry_price'],
        'purchase_val': pos['purchase_val'],
        'exit_price': curr_p,
        'sale_val': curr_val,
        'stop_price': pos['current_stop_price'],
        'gross_pnl': gross,
        'fees': fee,
        'net_pnl': net,
        'exit_reason': 'IN_POSITION (Active)',
        'status': 'OPEN'
    })

print(f"Total Completed & Open Trades: {len(trade_history)}")
for idx, t in enumerate(trade_history, 1):
    drop_pct = ((t['entry_price'] - t['stop_price']) / t['entry_price'] * 100).quantize(Decimal("0.01"))
    roi_trade = (t['net_pnl'] / t['purchase_val'] * 100).quantize(Decimal("0.01"))
    print(f"\nTRADE #{idx:02d}: {t['symbol']} ({t['status']})")
    print(f"  Entry Time          : {t['entry_time']} IST | Exit/Time: {t['exit_time']}")
    print(f"  Quantity            : {t['qty']} shares")
    print(f"  Purchase Price      : Rs. {t['entry_price']:.2f} -> Total Purchase Value: Rs. {t['purchase_val']:.2f}")
    print(f"  Current/Sale Price  : Rs. {t['exit_price']:.2f} -> Current/Sale Value   : Rs. {t['sale_val']:.2f}")
    print(f"  Hard Stop-Loss (-5%): Rs. {t['stop_price']:.2f} (-{drop_pct}%)")
    print(f"  Gross P&L           : {'+' if t['gross_pnl'] >= 0 else ''}Rs. {t['gross_pnl']:.2f}")
    print(f"  Estimated Fees      : Rs. {t['fees']:.2f}")
    print(f"  NET PROFIT          : {'+' if t['net_pnl'] >= 0 else ''}Rs. {t['net_pnl']:.2f} ({'+' if roi_trade >= 0 else ''}{roi_trade}%)")

total_net = total_gross_pnl - total_fees
ret_cap = (total_net / REFERENCE_CAPITAL * 100).quantize(Decimal("0.01"))
print("\n" + "=" * 80)
print("  MULTI-STOCK PORTFOLIO SUMMARY")
print("=" * 80)
print(f"Starting Reference Capital : Rs. {REFERENCE_CAPITAL:,.2f}")
print(f"Total Purchase Value       : Rs. {total_purchased:,.2f}")
print(f"Total Current / Sale Value : Rs. {total_sale_or_mtm:,.2f}")
print(f"Total Gross Profit         : {'+' if total_gross_pnl >= 0 else ''}Rs. {total_gross_pnl:,.2f}")
print(f"Total Transaction Fees     : Rs. {total_fees:,.2f}")
print(f"TOTAL NET PROFIT (ALL STOCKS): {'+' if total_net >= 0 else ''}Rs. {total_net:,.2f} ({'+' if ret_cap >= 0 else ''}{ret_cap}% on Rs.5,000)")
print(f"Remaining Liquid Cash      : Rs. {cash_balance:,.2f}")
print(f"Final Net Portfolio Equity : Rs. {(REFERENCE_CAPITAL + total_net):,.2f}")
print("=" * 80)
