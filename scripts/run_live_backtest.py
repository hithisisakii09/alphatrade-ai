"""CLI tool to run EventDrivenBacktester on candles from SQLite or Angel One and report metrics."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.backtest.engine import EventDrivenBacktester
from app.core.clock import to_ist
from app.core.config import _load_env_file, load_app_config
from app.core.database import BacktestRunRecord, BacktestTradeRecord, MarketCandleRecord, Session, init_db
from app.core.execution_guard import assert_paper_mode
from app.data.models import CandleBar

# Load environment configuration
_load_env_file()


def run_backtest_from_db(
    symbols: list[str],
    dataset_id: str = "HISTORICAL_INGESTION_RUN",
    db_url: str | None = None,
) -> None:
  assert_paper_mode()
  cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(cfg.database_url)

  print("=================================================================")
  print("AlphaTrade AI v2.0 — Event-Driven Quantitative Backtester")
  print("=================================================================")
  print(f"[*] Strategy: {cfg.strategy.strategy_id} (Version {cfg.strategy.version})")
  print(f"[*] Risk Profile: {cfg.active_risk_profile} (Capital: ₹{cfg.strategy.risk_profiles[cfg.active_risk_profile].reference_capital:,.2f})")
  print(f"[*] Exit Mode: {cfg.strategy.risk_profiles[cfg.active_risk_profile].exit_mode}")
  print(f"[*] Target Symbols: {', '.join(symbols)}")
  print(f"[*] Database: {cfg.database_url}")
  print("=================================================================\n")

  bars_by_symbol: dict[str, list[CandleBar]] = {}
  prev_closes: dict[str, Decimal] = {}

  with SessionLocal() as db:
    for sym in symbols:
      recs = (
          db.query(MarketCandleRecord)
          .filter(MarketCandleRecord.symbol == sym)
          .order_by(MarketCandleRecord.timestamp_utc.asc())
          .all()
      )
      if not recs:
        print(f"[*] {sym}: No bars found in market_candles table. (Run download_historical_candles.py first)")
        continue

      candle_bars = [
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
      bars_by_symbol[sym] = candle_bars
      prev_closes[sym] = candle_bars[0].open
      print(f"[*] {sym}: Loaded {len(candle_bars)} 1m bars from database.")

    if not bars_by_symbol:
      print("\n[ERROR] No candle bars found for specified symbols. Please ingest data first.")
      return

    print("\n[*] Executing chronological simulation with pessimistic intrabar stops...")
    _, BacktestSession = init_db("sqlite:///:memory:")
    with BacktestSession() as bt_db:
      bt = EventDrivenBacktester(bt_db, cfg)
      summary = bt.run_backtest(
          bars_1m_by_symbol=bars_by_symbol,
          prev_closes_by_symbol=prev_closes,
          dataset_id=dataset_id,
      )

      # Fetch recorded trades from isolated backtest db
      trades = (
          bt_db.query(BacktestTradeRecord)
          .filter(BacktestTradeRecord.run_id == summary.run_id)
          .all()
      )

      # Persist run and trades to main database for historical tracking
      run_rec = (
          bt_db.query(BacktestRunRecord)
          .filter(BacktestRunRecord.run_id == summary.run_id)
          .first()
      )
      if run_rec:
        db.add(BacktestRunRecord(**{k: v for k, v in run_rec.__dict__.items() if k != "id"}))
      for t in trades:
        db.add(BacktestTradeRecord(**{k: v for k, v in t.__dict__.items() if k != "id"}))
      db.commit()

    print("\n=================================================================")
    print(f"BACKTEST PERFORMANCE REPORT (Run ID: {summary.run_id})")
    print("=================================================================")
    print(f"• Total Trades:            {summary.total_trades}")
    print(f"• Winning Trades:          {summary.wins}")
    print(f"• Losing Trades:           {summary.losses}")
    print(f"• Win Rate:                {summary.win_rate_pct}%")
    print(f"• Profit Factor:           {summary.profit_factor}")
    print(f"• Net Expectancy (INR):    ₹{summary.net_expectancy_inr:,.2f} per trade")
    print(f"• Net Expectancy (R):      +{summary.net_expectancy_r} R")
    print(f"• Max Drawdown:            {summary.max_drawdown_pct}%")
    print(f"• Avg Holding Duration:    {summary.avg_holding_minutes} minutes")
    print(f"• Avg Cost-to-Risk Ratio:  {summary.avg_cost_to_risk_pct}%")
    print(f"• Evidence Classification: {summary.evidence_classification}")
    print("=================================================================")

    if trades:
      print("\nTRADE LOG:")
      print(f"{'SYMBOL':10s} {'DATE':10s} {'QTY':5s} {'ENTRY':10s} {'EXIT':10s} {'NET PNL':12s} {'R-MULT':8s} {'REASON'}")
      print("-" * 85)
      for t in trades:
        print(
            f"{t.symbol:10s} {t.session_date:10s} {t.quantity:5d} "
            f"₹{t.entry_price:9.2f} ₹{t.exit_price:9.2f} "
            f"₹{t.net_pnl_inr:11.2f} {t.r_multiple_net:7.2f}R {t.exit_reason}"
        )
    else:
      print("\n[INFO] No completed trades triggered in this sample period (price remained inside opening range or did not meet breakout criteria).")


def main():
  parser = argparse.ArgumentParser(description="Run event-driven backtest on SQLite candles.")
  parser.add_argument("--symbols", type=str, default="RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK", help="Comma-separated symbols")
  parser.add_argument("--dataset", type=str, default="SQLITE_INGESTION_RUN", help="Dataset run identifier")
  args = parser.parse_args()

  sym_list = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
  run_backtest_from_db(symbols=sym_list, dataset_id=args.dataset)


if __name__ == "__main__":
  main()

