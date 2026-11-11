"""CLI tool to execute the Milestone 7 Walk-Forward In-Sample vs Out-of-Sample research study."""

import argparse
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import _load_env_file, load_app_config
from app.core.database import BacktestRunRecord, init_db
from app.core.execution_guard import assert_paper_mode
from app.research.walk_forward import run_100_trade_out_of_sample_study

_load_env_file()


def main():
  parser = argparse.ArgumentParser(description="Run Milestone 7 Walk-Forward OOS Study & Slippage Matrix.")
  parser.add_argument("--db-url", type=str, default=None, help="Database URL (defaults to alphatrade_v2.db)")
  args = parser.parse_args()

  assert_paper_mode()
  cfg = load_app_config(database_url=args.db_url)

  print("==========================================================================================")
  print("AlphaTrade AI v2.0 — Milestone 7: Walk-Forward Out-of-Sample Quantitative Study")
  print("==========================================================================================")
  print(f"[*] Strategy ID: {cfg.strategy.strategy_id} (Version {cfg.strategy.version})")
  print(f"[*] Active Risk Profile: {cfg.active_risk_profile}")
  print(f"[*] Chronological Split: 70% In-Sample / 30% Out-of-Sample (Strictly Zero-Lookahead)")
  print(f"[*] Minimum Hurdle: >= 100 closed Out-of-Sample trades")
  print("==========================================================================================\n")

  print("[*] Generating multi-day dataset and executing chronological In-Sample vs Out-of-Sample study...")
  res = run_100_trade_out_of_sample_study(db_url=cfg.database_url)

  is_sum = res.in_sample_summary
  oos_sum = res.out_of_sample_summary

  # Calculate Walk-Forward Efficiency (WFE = OOS Net Expectancy / IS Net Expectancy)
  is_exp = float(is_sum.net_expectancy_r)
  oos_exp = float(oos_sum.net_expectancy_r)
  wfe = (oos_exp / is_exp * 100) if is_exp != 0 else 0.0

  print("\n==========================================================================================")
  print("WALK-FORWARD PERFORMANCE SUMMARY:")
  print("==========================================================================================")
  print(f"{'METRIC':28s} | {'IN-SAMPLE (70%)':20s} | {'OUT-OF-SAMPLE (30%)':20s}")
  print("-" * 75)
  print(f"{'Total Closed Trades':28s} | {is_sum.total_trades:<20d} | {oos_sum.total_trades:<20d}")
  print(f"{'Win Rate':28s} | {float(is_sum.win_rate_pct):<19.2f}% | {float(oos_sum.win_rate_pct):<19.2f}%")
  print(f"{'Profit Factor':28s} | {float(is_sum.profit_factor):<20.2f} | {float(oos_sum.profit_factor):<20.2f}")
  print(f"{'Net Expectancy (INR)':28s} | ₹{float(is_sum.net_expectancy_inr):<19.2f} | ₹{float(oos_sum.net_expectancy_inr):<19.2f}")
  print(f"{'Net Expectancy (R)':28s} | {float(is_sum.net_expectancy_r):<+19.4f} R | {float(oos_sum.net_expectancy_r):<+19.4f} R")
  print(f"{'Max Drawdown':28s} | {float(is_sum.max_drawdown_pct):<19.2f}% | {float(oos_sum.max_drawdown_pct):<19.2f}%")
  print(f"{'Avg Cost-to-Risk Ratio':28s} | {float(is_sum.avg_cost_to_risk_pct):<19.2f}% | {float(oos_sum.avg_cost_to_risk_pct):<19.2f}%")
  print("------------------------------------------------------------------------------------------")
  print(f"[*] Walk-Forward Efficiency (WFE Ratio): {wfe:.2f}% (Industry Benchmark: >= 50%)")
  print(f"[*] Statistical Significance (>= 100 Trades): {'PASSED' if res.is_statistically_valid else 'FAILED'} ({oos_sum.total_trades} trades)")
  print(f"[*] Final Evidence Classification:           {res.evidence_classification}")
  print("==========================================================================================\n")

  print("==========================================================================================")
  print("OUT-OF-SAMPLE SLIPPAGE STRESS MATRIX (1x, 2x, 3x Adverse Execution Friction):")
  print("==========================================================================================")
  print(f"{'SLIPPAGE TIER':26s} | {'TRADES':8s} | {'WIN %':8s} | {'PF':8s} | {'NET EXP (INR)':16s} | {'NET EXP (R)':12s}")
  print("-" * 88)
  for label, s in res.slippage_stress_matrix.items():
    print(
        f"{label:26s} | {s.total_trades:<8d} | {float(s.win_rate_pct):<7.2f}% | "
        f"{float(s.profit_factor):<8.2f} | ₹{float(s.net_expectancy_inr):<15.2f} | {float(s.net_expectancy_r):<+10.4f} R"
    )
  print("==========================================================================================\n")


if __name__ == "__main__":
  main()

