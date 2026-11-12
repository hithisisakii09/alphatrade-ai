#!/usr/bin/env python3
"""AlphaTrade AI v2.0 - Standalone Institutional Portfolio VaR & CVaR CLI.

Calculates:
- Parametric Value-at-Risk (95% & 99%)
- Cornish-Fisher Modified VaR (accounting for Skewness & Kurtosis)
- Expected Shortfall (Conditional VaR / CVaR)
- Pairwise Correlation & Covariance Matrix
- Portfolio Diversification Benefit %
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

# Ensure root workspace is on python sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import load_app_config
from app.core.money import to_decimal
from app.risk.var import PortfolioVaREngine


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Calculate institutional Portfolio Value-at-Risk (VaR) and Expected Shortfall (CVaR)."
  )
  parser.add_argument(
      "--symbols",
      type=str,
      default="RELIANCE,TCS",
      help="Comma-separated list of symbols (default: 'RELIANCE,TCS').",
  )
  parser.add_argument(
      "--capital",
      type=str,
      default="100000.00",
      help="Total portfolio equity in INR (default: '100000.00').",
  )
  parser.add_argument(
      "--lookback",
      type=int,
      default=1500,
      help="Number of 1m bars for covariance estimation (default: 1500).",
  )
  parser.add_argument(
      "--db",
      type=str,
      default="alphatrade_v2.db",
      help="Path to SQLite database (default: 'alphatrade_v2.db').",
  )
  parser.add_argument(
      "--json",
      action="store_true",
      help="Output raw JSON instead of formatted terminal summary.",
  )
  return parser.parse_args()


def main() -> None:
  args = parse_args()
  symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
  total_equity = Decimal(args.capital)

  # Allocate equal weights across requested symbols
  n = len(symbols)
  equal_exposure = total_equity / Decimal(n) if n > 0 else Decimal("0")
  holdings = {s: equal_exposure for s in symbols}

  engine = PortfolioVaREngine(db_path=args.db)
  report = engine.compute_portfolio_risk(
      holdings=holdings,
      total_equity_inr=total_equity,
      lookback_bars=args.lookback,
  )

  if args.json:
    out = {
        "timestamp_utc": report.timestamp_utc.isoformat(),
        "total_equity_inr": str(report.total_equity_inr),
        "total_exposure_inr": str(report.total_exposure_inr),
        "holdings_count": report.metrics.holdings_count,
        "metrics": {
            "portfolio_volatility_daily_pct": str(report.metrics.portfolio_volatility_daily_pct),
            "var_95_pct": str(report.metrics.var_95_pct),
            "var_95_inr": str(report.metrics.var_95_inr),
            "var_99_pct": str(report.metrics.var_99_pct),
            "var_99_inr": str(report.metrics.var_99_inr),
            "cvar_95_pct": str(report.metrics.cvar_95_pct),
            "cvar_95_inr": str(report.metrics.cvar_95_inr),
            "cvar_99_pct": str(report.metrics.cvar_99_pct),
            "cvar_99_inr": str(report.metrics.cvar_99_inr),
            "cf_var_95_pct": str(report.metrics.cf_var_95_pct),
            "cf_var_95_inr": str(report.metrics.cf_var_95_inr),
            "undiversified_var_95_inr": str(report.metrics.undiversified_var_95_inr),
            "diversification_benefit_pct": str(report.metrics.diversification_benefit_pct),
            "portfolio_skewness": str(report.metrics.portfolio_skewness),
            "portfolio_excess_kurtosis": str(report.metrics.portfolio_excess_kurtosis),
        },
        "weights": {s: str(w) for s, w in report.weights.items()},
        "correlation_matrix": {
            s1: {s2: str(c) for s2, c in inner.items()}
            for s1, inner in report.correlation_matrix.items()
        },
    }
    print(json.dumps(out, indent=2))
    return

  m = report.metrics
  print("=" * 72)
  print("ALPHATRADE AI v2.0 - PORTFOLIO VALUE-AT-RISK & EXPECTED SHORTFALL")
  print("=" * 72)
  print(f"Timestamp (UTC):        {report.timestamp_utc.strftime('%Y-%m-%d %H:%M:%S')}")
  print(f"Holdings Analyzed:      {', '.join(symbols)} ({m.holdings_count} assets)")
  print(f"Total Portfolio Equity: ₹{report.total_equity_inr:,.2f}")
  print(f"Total Exposure:         ₹{report.total_exposure_inr:,.2f}")
  print(f"Daily Portfolio Vol:    {m.portfolio_volatility_daily_pct}%")
  print(f"Portfolio Skewness:     {m.portfolio_skewness}")
  print(f"Portfolio Kurtosis:     {m.portfolio_excess_kurtosis}")
  print("-" * 72)
  print("RISK METRICS (1-DAY HORIZON):")
  print(f"  • 95% Parametric VaR:           ₹{m.var_95_inr:,.2f}  ({m.var_95_pct * 100:.2f}%)")
  print(f"  • 95% Expected Shortfall (CVaR):₹{m.cvar_95_inr:,.2f}  ({m.cvar_95_pct * 100:.2f}%)")
  print(f"  • 99% Tail VaR:                 ₹{m.var_99_inr:,.2f}  ({m.var_99_pct * 100:.2f}%)")
  print(f"  • 99% Expected Shortfall (CVaR):₹{m.cvar_99_inr:,.2f}  ({m.cvar_99_pct * 100:.2f}%)")
  print(f"  • 95% Cornish-Fisher Mod-VaR:   ₹{m.cf_var_95_inr:,.2f}  ({m.cf_var_95_pct * 100:.2f}%)")
  print("-" * 72)
  print("DIVERSIFICATION ATTRIBUTION:")
  print(f"  • Undiversified Sum-of-VaR:     ₹{m.undiversified_var_95_inr:,.2f}")
  print(f"  • Diversified Portfolio VaR:    ₹{m.var_95_inr:,.2f}")
  print(f"  • Diversification Benefit:      {m.diversification_benefit_pct}% risk reduction")
  print("-" * 72)
  print("PAIRWISE CORRELATION MATRIX:")
  header = "Symbol".ljust(12) + "".join(s.rjust(12) for s in symbols)
  print(header)
  for s1 in symbols:
    row = s1.ljust(12)
    for s2 in symbols:
      corr_val = report.correlation_matrix.get(s1, {}).get(s2, Decimal("0.0"))
      row += f"{float(corr_val):+12.4f}"
    print(row)
  print("=" * 72)


if __name__ == "__main__":
  main()

