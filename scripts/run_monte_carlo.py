"""Milestone M15: Standalone CLI to run Monte Carlo Bootstrap Resampling & Risk of Ruin analysis."""

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import load_app_config
from app.core.database import BacktestTradeRecord, PositionRecord, init_db
from app.core.money import to_decimal
from app.research.monte_carlo import run_monte_carlo_resampling


def main():
  parser = argparse.ArgumentParser(description="AlphaTrade AI v2.0 - Monte Carlo Bootstrap & Risk of Ruin Simulator")
  parser.add_argument("--source", choices=["LIVE", "BACKTEST", "ALL"], default="ALL", help="Trade source to resample")
  parser.add_argument("--iterations", type=int, default=5000, help="Number of Monte Carlo iterations (default: 5000)")
  parser.add_argument("--ruin-pct", type=float, default=0.10, help="Ruin drawdown threshold (e.g. 0.10 for 10%)")
  parser.add_argument("--capital", type=float, default=100000.0, help="Starting capital INR")
  parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
  parser.add_argument("--json", action="store_true", help="Output raw JSON payload")
  args = parser.parse_args()

  cfg = load_app_config()
  _, SessionLocal = init_db(cfg.database_url)

  with SessionLocal() as db:
    pnls: list[Decimal] = []
    if args.source in ("LIVE", "ALL"):
      positions = db.query(PositionRecord).filter(PositionRecord.status == "CLOSED").all()
      live_pnls = [to_decimal(p.realized_pnl_inr) for p in positions]
      pnls.extend(live_pnls)
      print(f"Ingested {len(live_pnls)} closed live paper trades.", file=sys.stderr)

    if args.source in ("BACKTEST", "ALL"):
      bt_trades = db.query(BacktestTradeRecord).all()
      bt_pnls = [to_decimal(t.net_pnl_inr) for t in bt_trades]
      pnls.extend(bt_pnls)
      print(f"Ingested {len(bt_pnls)} backtest trades.", file=sys.stderr)

    if not pnls:
      print("No trade records found in database to simulate.", file=sys.stderr)
      sys.exit(1)

    res = run_monte_carlo_resampling(
        trade_pnls=pnls,
        starting_capital_inr=Decimal(str(args.capital)),
        iterations=args.iterations,
        ruin_drawdown_pct=Decimal(str(args.ruin_pct)),
        seed=args.seed,
    )

    if args.json:
      print(json.dumps(res.to_dict(), indent=2))
      return

    print("\n=======================================================")
    print("  AlphaTrade AI v2.0 — Monte Carlo Simulation Results")
    print("=======================================================")
    print(f"Total Iterations:             {res.total_iterations}")
    print(f"Sample Trade Pool Size:       {res.sample_trade_count}")
    print(f"Starting Capital:             ₹{res.starting_capital_inr:,.2f}")
    print(f"Ruin Drawdown Threshold:      {float(res.ruin_drawdown_threshold_pct) * 100:.1f}%")
    print("-------------------------------------------------------")
    print(f"Probability of Ruin:          {res.ruin_probability_pct}%")
    print(f"Probability of Drawdown >= 5%: {res.drawdown_5pct_prob}%")
    print(f"Probability of Drawdown >= 10%: {res.drawdown_10pct_prob}%")
    print(f"Probability of Drawdown >= 15%: {res.drawdown_15pct_prob}%")
    print("-------------------------------------------------------")
    print(f"Median Max Drawdown:          {res.median_max_drawdown_pct}%")
    print(f"95th Percentile Max Drawdown: {res.p95_max_drawdown_pct}%")
    print(f"99th Percentile Max Drawdown: {res.p99_max_drawdown_pct}%")
    print("-------------------------------------------------------")
    print(f"5th Percentile Ending Equity: ₹{res.p05_ending_equity_inr:,.2f}")
    print(f"Median Ending Equity:         ₹{res.median_ending_equity_inr:,.2f}")
    print(f"95th Percentile Ending Equity: ₹{res.p95_ending_equity_inr:,.2f}")
    print("=======================================================\n")


if __name__ == "__main__":
  main()
