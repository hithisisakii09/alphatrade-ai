"""CLI tool to execute systematic hyperparameter sweeps & sensitivity matrices on SQLite market candles."""

import argparse
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.engine import EventDrivenBacktester
from app.core.config import _load_env_file, load_app_config
from app.core.database import MarketCandleRecord, Session, init_db
from app.core.execution_guard import assert_paper_mode
from app.data.models import CandleBar

_load_env_file()


def run_parameter_sweep(
    symbols: list[str] | None = None,
    opening_range_variants: list[int] | None = None,
    stop_variants: list[Decimal] | None = None,
    slippage_variants: list[Decimal] | None = None,
    db_url: str | None = None,
) -> None:
  assert_paper_mode()
  base_cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(base_cfg.database_url)

  or_list = opening_range_variants or [15, 30]
  stop_list = stop_variants or [Decimal("0.0100"), Decimal("0.0125"), Decimal("0.0150")]
  slip_list = slippage_variants or [Decimal("0.00025"), Decimal("0.00050"), Decimal("0.00075")]

  print("==========================================================================================")
  print("AlphaTrade AI v2.0 — Quantitative Hyperparameter Sensitivity Sweep")
  print("==========================================================================================")
  print(f"[*] Base Strategy: {base_cfg.strategy.strategy_id} (Version {base_cfg.strategy.version})")
  print(f"[*] Opening Range Duration Variants: {or_list} minutes")
  print(f"[*] Initial Stop Loss Variants: {[f'{float(s)*100:.2f}%' for s in stop_list]}")
  print(f"[*] Slippage Stress Variants: {[f'{float(s)*100:.3f}%' for s in slip_list]}")
  print(f"[*] Database: {base_cfg.database_url}")
  print("==========================================================================================\n")

  bars_by_symbol: dict[str, list[CandleBar]] = {}
  prev_closes: dict[str, Decimal] = {}

  with SessionLocal() as db:
    target_syms = symbols
    if not target_syms or target_syms == ["ALL"]:
      target_syms = [
          r[0]
          for r in db.conn.cursor()
          .execute("SELECT DISTINCT symbol FROM market_candles ORDER BY symbol")
          .fetchall()
      ]
    print(f"[*] Loading market candles for {len(target_syms)} symbols...")

    for sym in target_syms:
      recs = (
          db.query(MarketCandleRecord)
          .filter(MarketCandleRecord.symbol == sym)
          .order_by(MarketCandleRecord.timestamp_utc.asc())
          .all()
      )
      if not recs:
        continue
      bars_by_symbol[sym] = [
          CandleBar(
              symbol=r.symbol,
              interval_minutes=r.interval_minutes,
              timestamp_utc=r.timestamp_utc,
              open=r.open,
              high=r.high,
              low=r.low,
              close=r.close,
              volume=r.volume,
              source=r.source,
          )
          for r in recs
      ]
      prev_closes[sym] = bars_by_symbol[sym][0].open

  if not bars_by_symbol:
    print("[ERROR] No market candles found in SQLite database.")
    return

  total_bars = sum(len(b) for b in bars_by_symbol.values())
  print(f"[*] Successfully loaded {total_bars} 1m bars across {len(bars_by_symbol)} symbols.\n")
  print("[*] Executing sensitivity matrix across parameter configurations...")

  results = []
  sweep_idx = 0

  for or_min in or_list:
    for stop_pct in stop_list:
      for slip_pct in slip_list:
        sweep_idx += 1
        cfg = deepcopy(base_cfg)
        cfg.strategy.opening_range_minutes = or_min
        active_prof = cfg.strategy.risk_profiles[cfg.active_risk_profile]
        active_prof.initial_stop_pct = stop_pct

        _, BacktestSession = init_db("sqlite:///:memory:")
        with BacktestSession() as bt_db:
          bt = EventDrivenBacktester(bt_db, cfg)
          summary = bt.run_backtest(
              bars_1m_by_symbol=bars_by_symbol,
              prev_closes_by_symbol=prev_closes,
              dataset_id=f"SWEEP_OR{or_min}_ST{int(stop_pct*10000)}_SL{int(slip_pct*100000)}",
              custom_slippage_pct=slip_pct,
          )
          results.append({
              "or_min": or_min,
              "stop_pct": stop_pct,
              "slip_pct": slip_pct,
              "trades": summary.total_trades,
              "wins": summary.wins,
              "losses": summary.losses,
              "win_rate": summary.win_rate_pct,
              "profit_factor": summary.profit_factor,
              "net_exp_inr": summary.net_expectancy_inr,
              "net_exp_r": summary.net_expectancy_r,
              "max_dd": summary.max_drawdown_pct,
              "cost_to_risk": summary.avg_cost_to_risk_pct,
              "classification": summary.evidence_classification,
          })

  # Format output table
  print("\n==================================================================================================================")
  print("HYPERPARAMETER SENSITIVITY MATRIX RESULTS:")
  print("==================================================================================================================")
  print(
      f"{'OR':4s} | {'STOP':6s} | {'SLIP':7s} | {'TRADES':6s} | {'WIN %':7s} | {'PF':6s} | "
      f"{'NET EXP (INR)':14s} | {'NET EXP (R)':11s} | {'MAX DD':7s} | {'COST/RISK':9s} | {'CLASSIFICATION'}"
  )
  print("-" * 114)

  for r in results:
    print(
        f"{r['or_min']:2d}m  | "
        f"{float(r['stop_pct'])*100:4.2f}% | "
        f"{float(r['slip_pct'])*100:5.3f}% | "
        f"{r['trades']:6d} | "
        f"{float(r['win_rate']):5.1f}%  | "
        f"{float(r['profit_factor']):5.2f} | "
        f"₹{float(r['net_exp_inr']):11.2f}   | "
        f"{float(r['net_exp_r']):+7.2f} R    | "
        f"{float(r['max_dd']):5.2f}% | "
        f"{float(r['cost_to_risk']):6.1f}%   | "
        f"{r['classification']}"
    )
  print("==================================================================================================================\n")


def main():
  parser = argparse.ArgumentParser(description="Run quantitative parameter sweep.")
  parser.add_argument("--symbols", type=str, default="ALL", help="Comma-separated symbols or 'ALL'")
  args = parser.parse_args()

  sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
  run_parameter_sweep(symbols=sym_list)


if __name__ == "__main__":
  main()

