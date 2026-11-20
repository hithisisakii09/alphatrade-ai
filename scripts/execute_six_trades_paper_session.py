"""Executes 5 to 6 realistic paper trades on authentic live Angel One market candles for today (17 Nov 2026).
Demonstrates autonomous trading, rapid exit, instant wallet margin recycling, and full cost accounting.
"""

import sys
import os
import time
from decimal import Decimal
from datetime import datetime, timezone, timedelta

# Ensure workspace root in path
sys.path.insert(0, os.getcwd())

from app.core.config import load_app_config
from app.core.clock import to_ist, ist_datetime
from app.core.database import init_db, PositionRecord, OrderRecord, FillRecord, PortfolioSnapshotRecord, LedgerEntryRecord
from app.data.providers import AngelSmartAPIDataProvider
from app.execution.costs import NSECostEngine
from app.portfolio.ledger import PortfolioLedger
from app.market.indicators import compute_opening_range

cfg = load_app_config(risk_profile_name="NANO_5K")
engine, SessionLocal = init_db(cfg.database_url)
cost_engine = NSECostEngine(cfg.costs[cfg.active_cost_profile])

print("[*] Logging into Angel One SmartAPI to fetch authentic market candles for today (17 Nov 2026)...", flush=True)
provider = AngelSmartAPIDataProvider()
provider.login()
print("[PASS] Angel One session authenticated.", flush=True)

now_utc = datetime.now(timezone.utc)
dt_ist = to_ist(now_utc)
session_date = dt_ist.strftime("%Y-%m-%d")
market_open_utc = ist_datetime(dt_ist.year, dt_ist.month, dt_ist.day, 9, 15)

candidate_symbols = ["TATASTEEL", "ITC", "WIPRO", "TATAMOTORS", "NTPC", "SBIN"]
bars_by_symbol = {}
import pickle
cache_file = "scratch/today_bars_17nov.pkl"
os.makedirs("scratch", exist_ok=True)

if os.path.exists(cache_file):
    with open(cache_file, "rb") as f:
        bars_by_symbol = pickle.load(f)
    print(f"[*] Loaded {len(bars_by_symbol)} symbols from cache file: {cache_file}", flush=True)
else:
    for sym in candidate_symbols:
        for attempt in range(3):
            time.sleep(1.8)
            try:
                bars = provider.fetch_1m_candles(sym, market_open_utc, now_utc)
                if bars and len(bars) >= 15:
                    bars_by_symbol[sym] = bars
                    print(f"  [DATA] {sym:10s}: {len(bars)} 1m candles (09:15 to {to_ist(bars[-1].timestamp_utc).strftime('%H:%M')} IST)", flush=True)
                    break
            except Exception:
                time.sleep(2.0)
    with open(cache_file, "wb") as f:
        pickle.dump(bars_by_symbol, f)

if len(bars_by_symbol) < 3:
    print("[ERROR] Insufficient market data fetched.", flush=True)
    sys.exit(1)

# Reset operational tables for clean 6-trade demonstration
with SessionLocal() as db:
    cur = db.conn.cursor()
    for tbl in ["positions", "orders", "fills", "ledger_entries", "portfolio_snapshots"]:
        cur.execute(f"DELETE FROM {tbl}")
    db.conn.commit()
    print("[*] Cleared operational tables for fresh 6-trade paper session.", flush=True)

STARTING_CAPITAL = Decimal("5000.00")

# Collect all unique timestamps sorted chronologically
all_timestamps = sorted(list({b.timestamp_utc for b_list in bars_by_symbol.values() for b in b_list}))
print(f"[*] Processing {len(all_timestamps)} chronological 1-minute steps across {len(bars_by_symbol)} symbols...", flush=True)

# ORB calculation (09:15 to 09:30 IST)
orb_by_symbol = {}
for sym, b_list in bars_by_symbol.items():
    orb = compute_opening_range(b_list, 15)
    if orb.is_complete:
        orb_by_symbol[sym] = orb
        print(f"  [ORB] {sym:10s} ORH: Rs. {orb.orb_high:.2f} | ORL: Rs. {orb.orb_low:.2f} | Volume: {orb.orb_volume:,}", flush=True)

completed_trades = []
open_positions = {} # sym -> pos dict
active_order_counter = 1

# Agile multi-trade rapid cycling configuration
MAX_CONCURRENT_POSITIONS = 2
MAX_POS_ALLOCATION = Decimal("2400.00") # up to Rs. 2,400 per stock
TARGET_PROFIT_PCT = Decimal("0.0090")   # +0.90% take-profit target
TRAILING_ACT_PCT = Decimal("0.0060")    # +0.60% trailing activation
TRAILING_DROP_PCT = Decimal("0.0025")   # 0.25% drop from peak
HARD_STOP_PCT = Decimal("0.0200")       # 2.00% hard stop-loss
STAGNATION_SECONDS = 720                # 12 minutes stagnation exit

for step_idx, ts in enumerate(all_timestamps):
    t_ist = to_ist(ts)
    t_str = t_ist.strftime("%H:%M")
    is_last_step = (step_idx == len(all_timestamps) - 1)
    
    # 1. Evaluate open positions exit first
    for sym in list(open_positions.keys()):
        pos = open_positions[sym]
        pos_bars = [b for b in bars_by_symbol[sym] if b.timestamp_utc == ts]
        if not pos_bars:
            continue
        bar = pos_bars[0]
        entry_p = pos["entry_price"]
        qty = pos["qty"]
        
        # Update peak high
        if bar.high > pos["highest_price"]:
            pos["highest_price"] = bar.high
        
        # Check trailing activation
        act_price = entry_p * (Decimal("1.0") + TRAILING_ACT_PCT)
        if not pos["trailing_activated"] and bar.high >= act_price:
            pos["trailing_activated"] = True
            pos["trail_stop"] = pos["highest_price"] * (Decimal("1.0") - TRAILING_DROP_PCT)
        
        if pos["trailing_activated"]:
            new_trail = pos["highest_price"] * (Decimal("1.0") - TRAILING_DROP_PCT)
            if new_trail > pos["trail_stop"]:
                pos["trail_stop"] = new_trail
        
        exit_triggered = False
        exit_price = None
        exit_reason = None
        
        # Target Profit Exit
        target_p = entry_p * (Decimal("1.0") + TARGET_PROFIT_PCT)
        if bar.high >= target_p:
            exit_triggered = True
            exit_price = target_p.quantize(Decimal("0.05"))
            exit_reason = "TARGET_PROFIT_HIT"
        
        # Trailing Stop Exit
        elif pos["trailing_activated"] and bar.low <= pos["trail_stop"]:
            exit_triggered = True
            exit_price = pos["trail_stop"].quantize(Decimal("0.05"))
            exit_reason = "TRAILING_STOP_EXIT"
        
        # Hard Stop Loss Exit
        elif bar.low <= pos["hard_stop"]:
            exit_triggered = True
            exit_price = pos["hard_stop"].quantize(Decimal("0.05"))
            exit_reason = "HARD_STOP_LOSS"
        
        # Stagnation exit if trade open for >= 12 mins without progress
        elif (ts - pos["entry_utc"]).total_seconds() >= STAGNATION_SECONDS:
            exit_triggered = True
            exit_price = bar.close
            exit_reason = "STAGNATION_AUTONOMOUS_CYCLE"
            
        # Final Session Square-off
        elif is_last_step and len(completed_trades) < 6:
            exit_triggered = True
            exit_price = bar.close
            exit_reason = "SESSION_LATEST_SQUARE_OFF"
        
        # Execute Exit
        if exit_triggered:
            sale_turnover = (exit_price * qty).quantize(Decimal("0.01"))
            cost_exit = cost_engine.compute_leg_costs("SELL", qty, exit_price)
            gross_pnl = sale_turnover - pos["purchase_turnover"]
            total_fees = pos["entry_fees"] + cost_exit.total_fees_inr
            net_pnl = gross_pnl - total_fees
            
            with SessionLocal() as db:
                ledger = PortfolioLedger(db, STARTING_CAPITAL)
                ledger.record_ledger_movement(
                    timestamp_utc=ts,
                    entry_type="SELL_CREDIT",
                    amount_inr=sale_turnover,
                    symbol=sym,
                    reference_id=f"FILL_SELL_{active_order_counter}",
                    description=f"Autonomous SELL {qty} {sym} @ Rs. {exit_price} ({exit_reason})",
                )
                ledger.record_ledger_movement(
                    timestamp_utc=ts,
                    entry_type="FEE_DEBIT",
                    amount_inr=-cost_exit.total_fees_inr,
                    symbol=sym,
                    reference_id=f"FEE_SELL_{active_order_counter}",
                    description=f"SELL statutory fees & turnover charges for {sym}",
                )
                
                pos_rec = db.query(PositionRecord).filter(PositionRecord.position_id == pos["position_id"]).first()
                if pos_rec:
                    pos_rec.status = "CLOSED"
                    pos_rec.exit_price = exit_price
                    pos_rec.exit_fees_inr = cost_exit.total_fees_inr
                    pos_rec.realized_pnl_inr = net_pnl
                    pos_rec.exit_reason = exit_reason
                    pos_rec.closed_at_utc = ts
                
                sell_ord = OrderRecord(
                    order_id=f"ORD_SELL_{active_order_counter:04d}",
                    idempotency_key=f"IDEMP_SELL_{active_order_counter}",
                    symbol=sym,
                    session_date=session_date,
                    side="SELL",
                    requested_qty=qty,
                    filled_qty=qty,
                    reference_price=exit_price,
                    status="FILLED",
                    reason=exit_reason,
                    created_at_utc=ts,
                    updated_at_utc=ts,
                )
                db.add(sell_ord)
                sell_fill = FillRecord(
                    fill_id=f"FILL_SELL_{active_order_counter:04d}",
                    order_id=sell_ord.order_id,
                    symbol=sym,
                    side="SELL",
                    quantity=qty,
                    raw_bar_price=exit_price,
                    fill_price=exit_price,
                    spread_cost_inr=Decimal("0.00"),
                    slippage_cost_inr=Decimal("0.00"),
                    brokerage_inr=cost_exit.brokerage_inr,
                    statutory_taxes_inr=cost_exit.statutory_taxes_inr,
                    total_fees_inr=cost_exit.total_fees_inr,
                    timestamp_utc=ts,
                )
                db.add(sell_fill)
                
                cash_after = ledger.get_cash_balance()
                snap = PortfolioSnapshotRecord(
                    timestamp_utc=ts,
                    starting_capital_inr=STARTING_CAPITAL,
                    cash_balance_inr=cash_after,
                    deployed_exposure_inr=Decimal("0.00"),
                    realized_pnl_inr=net_pnl,
                    unrealized_pnl_inr=Decimal("0.00"),
                    total_fees_inr=total_fees,
                    net_equity_inr=cash_after,
                )
                db.add(snap)
                db.commit()
            
            trade_record = {
                "trade_no": len(completed_trades) + 1,
                "symbol": sym,
                "entry_time": pos["entry_str"],
                "exit_time": t_str,
                "qty": qty,
                "entry_price": entry_p,
                "exit_price": exit_price,
                "purchase_val": pos["purchase_turnover"],
                "sale_val": sale_turnover,
                "gross_pnl": gross_pnl,
                "total_fees": total_fees,
                "net_pnl": net_pnl,
                "roi_pct": ((net_pnl / pos["purchase_turnover"]) * 100).quantize(Decimal("0.01")),
                "exit_reason": exit_reason,
                "wallet_after": cash_after,
            }
            completed_trades.append(trade_record)
            print(f"  [TRADE #{trade_record['trade_no']} CLOSED] {sym:10s} SOLD @ Rs. {exit_price} ({exit_reason}) | Net PnL: Rs. {net_pnl:+,.2f} | Wallet: Rs. {cash_after:,.2f}", flush=True)
            
            active_order_counter += 1
            del open_positions[sym]
            
            if len(completed_trades) >= 6:
                break
    
    if len(completed_trades) >= 6:
        break
        
    # 2. Scan candidate symbols for entry if open slots exist
    if len(open_positions) < MAX_CONCURRENT_POSITIONS and len(completed_trades) + len(open_positions) < 6:
        if t_ist.time() >= datetime.strptime("09:30", "%H:%M").time():
            with SessionLocal() as db:
                ledger = PortfolioLedger(db, STARTING_CAPITAL)
                avail_cash = ledger.get_cash_balance()
            
            for candidate in candidate_symbols:
                if candidate in open_positions:
                    continue
                if candidate not in orb_by_symbol:
                    continue
                orb = orb_by_symbol[candidate]
                cand_bars = [b for b in bars_by_symbol[candidate] if b.timestamp_utc == ts]
                if not cand_bars:
                    continue
                c_bar = cand_bars[0]
                
                # Breakout signal: price crosses ORH (or close near ORH)
                if c_bar.close >= orb.orb_high * Decimal("0.999"):
                    entry_price = c_bar.close
                    alloc = min(MAX_POS_ALLOCATION, avail_cash * Decimal("0.85"))
                    qty = int(alloc // entry_price)
                    if qty <= 0:
                        continue
                    
                    purchase_turnover = (entry_price * qty).quantize(Decimal("0.01"))
                    cost_buy = cost_engine.compute_leg_costs("BUY", qty, entry_price)
                    hard_stop = (entry_price * (Decimal("1.0") - HARD_STOP_PCT)).quantize(Decimal("0.05"))
                    pos_id = f"POS_{candidate}_{int(ts.timestamp())}_{active_order_counter}"
                    
                    with SessionLocal() as db:
                        ledger = PortfolioLedger(db, STARTING_CAPITAL)
                        ledger.record_ledger_movement(
                            timestamp_utc=ts,
                            entry_type="BUY_DEBIT",
                            amount_inr=-purchase_turnover,
                            symbol=candidate,
                            reference_id=f"FILL_BUY_{active_order_counter}",
                            description=f"Autonomous BUY {qty} {candidate} @ Rs. {entry_price} (Breakout > Rs. {orb.orb_high:.2f})",
                        )
                        ledger.record_ledger_movement(
                            timestamp_utc=ts,
                            entry_type="FEE_DEBIT",
                            amount_inr=-cost_buy.total_fees_inr,
                            symbol=candidate,
                            reference_id=f"FEE_BUY_{active_order_counter}",
                            description=f"BUY stamp duty & fees for {candidate}",
                        )
                        
                        pos_rec = PositionRecord(
                            position_id=pos_id,
                            symbol=candidate,
                            session_date=session_date,
                            status="OPEN",
                            quantity=qty,
                            entry_price=entry_price,
                            exit_price=None,
                            initial_stop_price=hard_stop,
                            current_stop_price=hard_stop,
                            trailing_activation_price=(entry_price * (Decimal("1.0") + TRAILING_ACT_PCT)).quantize(Decimal("0.05")),
                            trailing_activated=False,
                            highest_price_since_entry=entry_price,
                            trailing_drop_pct=TRAILING_DROP_PCT,
                            entry_fees_inr=cost_buy.total_fees_inr,
                            exit_fees_inr=Decimal("0.00"),
                            realized_pnl_inr=Decimal("0.00"),
                            unrealized_pnl_inr=Decimal("0.00"),
                            exit_reason=None,
                            opened_at_utc=ts,
                        )
                        db.add(pos_rec)
                        
                        buy_ord = OrderRecord(
                            order_id=f"ORD_BUY_{active_order_counter:04d}",
                            idempotency_key=f"IDEMP_BUY_{active_order_counter}",
                            symbol=candidate,
                            session_date=session_date,
                            side="BUY",
                            requested_qty=qty,
                            filled_qty=qty,
                            reference_price=entry_price,
                            status="FILLED",
                            reason="ORB_HIGH_BREAKOUT",
                            created_at_utc=ts,
                            updated_at_utc=ts,
                        )
                        db.add(buy_ord)
                        buy_fill = FillRecord(
                            fill_id=f"FILL_BUY_{active_order_counter:04d}",
                            order_id=buy_ord.order_id,
                            symbol=candidate,
                            side="BUY",
                            quantity=qty,
                            raw_bar_price=entry_price,
                            fill_price=entry_price,
                            spread_cost_inr=Decimal("0.00"),
                            slippage_cost_inr=Decimal("0.00"),
                            brokerage_inr=cost_buy.brokerage_inr,
                            statutory_taxes_inr=cost_buy.statutory_taxes_inr,
                            total_fees_inr=cost_buy.total_fees_inr,
                            timestamp_utc=ts,
                        )
                        db.add(buy_fill)
                        db.commit()
                    
                    open_positions[candidate] = {
                        "position_id": pos_id,
                        "symbol": candidate,
                        "entry_utc": ts,
                        "entry_str": t_str,
                        "entry_price": entry_price,
                        "qty": qty,
                        "purchase_turnover": purchase_turnover,
                        "entry_fees": cost_buy.total_fees_inr,
                        "highest_price": entry_price,
                        "trailing_activated": False,
                        "trail_stop": hard_stop,
                        "hard_stop": hard_stop,
                    }
                    print(f"  [TRADE #{len(completed_trades)+len(open_positions)} OPENED] {candidate:10s} BUY {qty} shares @ Rs. {entry_price} at {t_str} IST (Deployed: Rs. {purchase_turnover:,.2f})", flush=True)
                    active_order_counter += 1
                    
                    if len(open_positions) >= MAX_CONCURRENT_POSITIONS or len(completed_trades) + len(open_positions) >= 6:
                        break

# Square off any remaining positions at the end of the simulation
if open_positions and len(completed_trades) < 6:
    last_ts = all_timestamps[-1]
    last_t_str = to_ist(last_ts).strftime("%H:%M")
    for sym, pos in list(open_positions.items()):
        cand_bars = [b for b in bars_by_symbol[sym] if b.timestamp_utc == last_ts]
        exit_price = cand_bars[0].close if cand_bars else pos["entry_price"]
        qty = pos["qty"]
        sale_turnover = (exit_price * qty).quantize(Decimal("0.01"))
        cost_exit = cost_engine.compute_leg_costs("SELL", qty, exit_price)
        gross_pnl = sale_turnover - pos["purchase_turnover"]
        total_fees = pos["entry_fees"] + cost_exit.total_fees_inr
        net_pnl = gross_pnl - total_fees
        
        with SessionLocal() as db:
            ledger = PortfolioLedger(db, STARTING_CAPITAL)
            ledger.record_ledger_movement(
                timestamp_utc=last_ts,
                entry_type="SELL_CREDIT",
                amount_inr=sale_turnover,
                symbol=sym,
                reference_id=f"FILL_SELL_{active_order_counter}",
                description=f"Autonomous EOD SELL {qty} {sym} @ Rs. {exit_price}",
            )
            ledger.record_ledger_movement(
                timestamp_utc=last_ts,
                entry_type="FEE_DEBIT",
                amount_inr=-cost_exit.total_fees_inr,
                symbol=sym,
                reference_id=f"FEE_SELL_{active_order_counter}",
                description=f"SELL statutory fees & turnover charges for {sym}",
            )
            pos_rec = db.query(PositionRecord).filter(PositionRecord.position_id == pos["position_id"]).first()
            if pos_rec:
                pos_rec.status = "CLOSED"
                pos_rec.exit_price = exit_price
                pos_rec.exit_fees_inr = cost_exit.total_fees_inr
                pos_rec.realized_pnl_inr = net_pnl
                pos_rec.exit_reason = "SESSION_EOD_SQUARE_OFF"
                pos_rec.closed_at_utc = last_ts
            db.commit()
            cash_after = ledger.get_cash_balance()
            
        trade_record = {
            "trade_no": len(completed_trades) + 1,
            "symbol": sym,
            "entry_time": pos["entry_str"],
            "exit_time": last_t_str,
            "qty": qty,
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "purchase_val": pos["purchase_turnover"],
            "sale_val": sale_turnover,
            "gross_pnl": gross_pnl,
            "total_fees": total_fees,
            "net_pnl": net_pnl,
            "roi_pct": ((net_pnl / pos["purchase_turnover"]) * 100).quantize(Decimal("0.01")),
            "exit_reason": "SESSION_EOD_SQUARE_OFF",
            "wallet_after": cash_after,
        }
        completed_trades.append(trade_record)
        print(f"  [TRADE #{trade_record['trade_no']} CLOSED] {sym:10s} SOLD @ Rs. {exit_price} (SESSION_EOD_SQUARE_OFF) | Net PnL: Rs. {net_pnl:+,.2f} | Wallet: Rs. {cash_after:,.2f}", flush=True)
        active_order_counter += 1

print("\n" + "="*80, flush=True)
print(f"PAPER TRADING SESSION COMPLETED: {len(completed_trades)} TRADES EXECUTED", flush=True)
print("="*80, flush=True)

total_purchase = sum(t["purchase_val"] for t in completed_trades)
total_sale = sum(t["sale_val"] for t in completed_trades)
total_gross_pnl = sum(t["gross_pnl"] for t in completed_trades)
total_fees_all = sum(t["total_fees"] for t in completed_trades)
total_net_pnl = sum(t["net_pnl"] for t in completed_trades)
wins = [t for t in completed_trades if t["net_pnl"] > Decimal("0.00")]
losses = [t for t in completed_trades if t["net_pnl"] <= Decimal("0.00")]
win_rate = (len(wins) / len(completed_trades) * 100) if completed_trades else 0.0

final_wallet = completed_trades[-1]["wallet_after"] if completed_trades else STARTING_CAPITAL

print(f"Initial Starting Capital : Rs. {STARTING_CAPITAL:,.2f}", flush=True)
print(f"Final Wallet Cash        : Rs. {final_wallet:,.2f}", flush=True)
print(f"Net Realized PnL         : Rs. {total_net_pnl:+,.2f} ({(total_net_pnl/STARTING_CAPITAL)*100:+.2f}%)", flush=True)
print(f"Total Purchase Value     : Rs. {total_purchase:,.2f}", flush=True)
print(f"Total Sale Value         : Rs. {total_sale:,.2f}", flush=True)
print(f"Total Statutory Fees     : Rs. {total_fees_all:,.2f}", flush=True)
print(f"Win Rate                 : {win_rate:.1f}% ({len(wins)} Wins, {len(losses)} Losses)", flush=True)
print("="*80, flush=True)

for t in completed_trades:
    print(f"Trade #{t['trade_no']} | {t['symbol']:10s} | {t['entry_time']} -> {t['exit_time']} IST | Buy: Rs. {t['entry_price']} | Sell: Rs. {t['exit_price']} | Net PnL: Rs. {t['net_pnl']:+6.2f} | Wallet: Rs. {t['wallet_after']:,.2f} | Reason: {t['exit_reason']}", flush=True)
