"""CLI tool to execute Walk-Forward In-Sample vs Out-of-Sample research with strict provenance enforcement."""

import argparse
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import _load_env_file, load_app_config
from app.core.database import BacktestRunRecord, init_db
from app.core.execution_guard import assert_paper_mode
from app.research.walk_forward import (
    generate_research_report_markdown,
    run_100_trade_out_of_sample_study,
    run_real_historical_walk_forward,
)

_load_env_file()


def main():
  parser = argparse.ArgumentParser(description="Run Walk-Forward OOS Quantitative Study.")
  parser.add_argument(
      "--source",
      type=str,
      choices=["HISTORICAL", "SYNTHETIC"],
      default="HISTORICAL",
      help="Dataset source: HISTORICAL (genuine SQLite candles) or SYNTHETIC (stress-test vectors)",
  )
  parser.add_argument(
      "--symbols",
      type=str,
      default="ALL",
      help="Comma-separated symbols or ALL (defaults to ALL)",
  )
  parser.add_argument("--db-url", type=str, default=None, help="Database URL (defaults to alphatrade_v2.db)")
  args = parser.parse_args()

  assert_paper_mode()
  cfg = load_app_config(database_url=args.db_url)

  sym_list = None if args.symbols == "ALL" else [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

  print("==========================================================================================")
  print(f"AlphaTrade AI v2.0 — Walk-Forward Research Engine [{args.source} MODE]")
  print("==========================================================================================")
  print(f"[*] Strategy ID: {cfg.strategy.strategy_id} (Version {cfg.strategy.version})")
  print(f"[*] Active Risk Profile: {cfg.active_risk_profile}")
  print(f"[*] Dataset Source: {args.source}")
  print(f"[*] Target Symbols: {args.symbols}")
  print(f"[*] Chronological Split: 70% In-Sample / 30% Out-of-Sample (Strict Zero-Lookahead)")
  print(f"[*] Statistical Eligibility Hurdle: >= 100 closed Out-of-Sample trades")
  print("==========================================================================================\n")

  if args.source == "HISTORICAL":
    print("[*] Loading genuine historical candles from SQLite and executing chronological walk-forward...")
    res = run_real_historical_walk_forward(db_url=cfg.database_url, symbols=sym_list)
  else:
    print("[*] Generating synthetic multi-day dataset and executing architectural stress test...")
    res = run_100_trade_out_of_sample_study(db_url=cfg.database_url, symbols=sym_list)

  is_sum = res.in_sample_summary
  oos_sum = res.out_of_sample_summary

  # Calculate Walk-Forward Efficiency (WFE = OOS Net Expectancy / IS Net Expectancy)
  is_exp = float(is_sum.net_expectancy_r)
  oos_exp = float(oos_sum.net_expectancy_r)
  wfe = (oos_exp / is_exp * 100) if is_exp != 0 else 0.0

  print("\n==========================================================================================")
  print(f"WALK-FORWARD PERFORMANCE SUMMARY [{res.data_provenance}]:")
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
  print(f"[*] Walk-Forward Efficiency (WFE Ratio): {wfe:.2f}% (Benchmark: >= 50%)")
  print(f"[*] Statistical Eligibility (>= 100 Trades): {'ELIGIBLE' if res.is_statistically_valid else 'INCONCLUSIVE (< 100 TRADES)'} ({oos_sum.total_trades} trades)")
  print(f"[*] Evidence Classification:                  {res.evidence_classification}")
  print(f"[*] Data Provenance:                          {res.data_provenance}")

  ci = res.confidence_intervals
  if ci.get("status") == "COMPUTED":
    print(f"[*] Win Rate 95% Confidence Interval:        {ci['win_rate_95_ci_pct'][0]:.2f}% to {ci['win_rate_95_ci_pct'][1]:.2f}% (SE: {ci['win_rate_standard_error_pct']:.2f}%)")
    print(f"[*] Net Expectancy (R) 95% CI:               {ci['expectancy_r_95_ci'][0]:+.4f} R to {ci['expectancy_r_95_ci'][1]:+.4f} R (SE: {ci['expectancy_r_standard_error']:.4f})")
  else:
    print(f"[*] Confidence Intervals:                     {ci.get('message', 'Sample size insufficient')}")
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


if __name__ == "__main__":
  main()

