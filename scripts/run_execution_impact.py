#!/usr/bin/env python3
"""AlphaTrade AI v2.0 - Standalone Execution Quality & Market Impact CLI.

Calculates:
- Square-Root Law of Market Impact (Almgren-Chriss framework)
- Temporary & Permanent Price Impact
- Implementation Shortfall Benchmark vs Arrival Price
- Historical Slippage (bps) & Participation Rates across database fills
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

# Ensure root workspace is on python sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import load_app_config
from app.core.money import to_decimal
from app.execution.impact import (
    ExecutionAnalyticsEngine,
    compute_square_root_market_impact,
)


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Calculate institutional market impact and analyze historical execution quality."
  )
  parser.add_argument(
      "--symbol",
      type=str,
      default="RELIANCE",
      help="NSE Equity symbol (default: 'RELIANCE').",
  )
  parser.add_argument(
      "--side",
      type=str,
      default="BUY",
      choices=["BUY", "SELL"],
      help="Order side (default: 'BUY').",
  )
  parser.add_argument(
      "--quantity",
      type=int,
      default=100,
      help="Order size in shares (default: 100).",
  )
  parser.add_argument(
      "--price",
      type=str,
      default="1340.00",
      help="Reference / Arrival price in INR (default: '1340.00').",
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
  symbol = args.symbol.strip().upper()
  qty = args.quantity
  raw_p = Decimal(args.price)
  side = args.side.upper()

  engine = ExecutionAnalyticsEngine(db_path=args.db)
  summary = engine.analyze_historical_executions()
  impact = engine.compute_order_market_impact(
      symbol=symbol,
      side=side,
      quantity=qty,
      raw_price=raw_p,
  )

  if args.json:
    out = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "historical_execution_summary": {
            "total_orders_analyzed": summary.total_orders_analyzed,
            "avg_slippage_bps": str(summary.avg_slippage_bps),
            "total_implementation_shortfall_inr": str(summary.total_implementation_shortfall_inr),
            "avg_participation_rate_pct": str(summary.avg_participation_rate_pct),
            "total_fees_and_impact_inr": str(summary.total_fees_and_impact_inr),
            "by_symbol": summary.by_symbol,
        },
        "order_impact_simulation": {
            "symbol": impact.symbol,
            "side": impact.side,
            "quantity": impact.quantity,
            "raw_arrival_price": str(impact.raw_arrival_price),
            "effective_execution_price": str(impact.effective_execution_price),
            "reference_bar_volume": impact.reference_volume,
            "participation_rate_pct": str(impact.participation_rate_pct),
            "daily_volatility_pct": str(impact.daily_volatility_pct),
            "temporary_impact_pct": str(impact.temporary_impact_pct),
            "temporary_impact_inr": str(impact.temporary_impact_inr),
            "permanent_impact_pct": str(impact.permanent_impact_pct),
            "permanent_impact_inr": str(impact.permanent_impact_inr),
            "total_impact_bps": str(impact.total_impact_bps),
            "implementation_shortfall_inr": str(impact.implementation_shortfall_inr),
            "is_participation_compliant": impact.is_participation_compliant,
            "max_allowed_participation_pct": str(impact.max_allowed_participation_pct),
        },
    }
    print(json.dumps(out, indent=2))
    return

  print("=" * 72)
  print("ALPHATRADE AI v2.0 - EXECUTION QUALITY & MARKET IMPACT ANALYTICS")
  print("=" * 72)
  print("1. HISTORICAL EXECUTION PERFORMANCE:")
  print(f"  • Orders Analyzed:              {summary.total_orders_analyzed}")
  print(f"  • Average Slippage:             {summary.avg_slippage_bps} bps")
  print(f"  • Implementation Shortfall:     ₹{summary.total_implementation_shortfall_inr:,.2f}")
  print(f"  • Average Participation Rate:   {summary.avg_participation_rate_pct}% of bar volume")
  print(f"  • Total Execution Friction:     ₹{summary.total_fees_and_impact_inr:,.2f} (fees + shortfall)")
  print("-" * 72)
  print(f"2. SQUARE-ROOT MARKET IMPACT SIMULATION ({side} {qty} {symbol} @ ₹{raw_p:,.2f}):")
  print(f"  • Reference Bar Volume:         {impact.reference_volume:,} shares (historical 1m average)")
  print(f"  • Participation Rate:           {impact.participation_rate_pct}% (Max: {impact.max_allowed_participation_pct}%)")
  print(f"  • Liquidity Status:             {'[COMPLIANT]' if impact.is_participation_compliant else '[EXCEEDED]'}")
  print(f"  • Daily Volatility (sigma):     {impact.daily_volatility_pct}%")
  print(f"  • Temporary Market Impact:      {impact.temporary_impact_pct * 100:.3f}% (₹{impact.temporary_impact_inr:,.2f})")
  print(f"  • Permanent Market Impact:      {impact.permanent_impact_pct * 100:.3f}% (₹{impact.permanent_impact_inr:,.2f})")
  print(f"  • Total Dynamic Slippage:       {impact.total_impact_bps} bps ({impact.total_impact_pct * 100:.3f}%)")
  print(f"  • Arrival Price:                ₹{impact.raw_arrival_price:,.2f}")
  print(f"  • Effective Execution Price:    ₹{impact.effective_execution_price:,.2f}")
  print(f"  • Implementation Shortfall:     ₹{impact.implementation_shortfall_inr:,.2f}")
  print("=" * 72)


if __name__ == "__main__":
  main()

