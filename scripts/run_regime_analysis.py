"""
M19 — Standalone HMM Regime Analysis CLI
=========================================
Usage:
  python scripts/run_regime_analysis.py --symbol RELIANCE --bars 120
  python scripts/run_regime_analysis.py --symbol SBIN --bars 60 --all-symbols
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import load_app_config
from app.core.database import init_db
from app.market.regime import VolatilityRegimeClassifier, MarketRegime
from sqlalchemy import text


def run_regime_analysis(symbol: str, bars: int, db_url: str) -> None:
    engine, SessionLocal = init_db(db_url)

    with SessionLocal() as session:
        cur = session.conn.cursor()
        cur.execute(
            "SELECT timestamp_utc, open, high, low, close, volume "
            "FROM market_candles "
            "WHERE symbol = ? AND interval_minutes = 1 "
            "ORDER BY timestamp_utc DESC LIMIT ?",
            (symbol, bars),
        )
        rows = cur.fetchall()

    if not rows:
        print(f"[REGIME] No candle data found for {symbol} in market_candles table.")
        return

    rows = list(reversed(rows))
    timestamps = [r[0] for r in rows]
    opens  = [float(r[1]) for r in rows]
    highs  = [float(r[2]) for r in rows]
    lows   = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    vols   = [int(r[5])   for r in rows]

    clf = VolatilityRegimeClassifier(min_bars=20, max_iter=60)
    clf.fit(closes, highs, lows)

    if not clf.is_trained:
        print(f"[REGIME] Insufficient bars ({len(rows)} < 20). Cannot classify regime.")
        return

    state = clf.predict_current()
    dist  = clf.regime_distribution()
    seq   = clf.predict_sequence()

    print(f"\n{'='*60}")
    print(f"  HMM VOLATILITY REGIME ANALYSIS — {symbol}")
    print(f"{'='*60}")
    print(f"  Bars analyzed       : {len(rows)} (1-min candles)")
    print(f"  Period              : {timestamps[0]} → {timestamps[-1]}")
    print(f"  Current regime      : {state.regime.value}")
    print(f"  Confidence          : {state.confidence:.1%}")
    print(f"  Posterior probs     : BULL={state.bull_prob:.3f}  NEUTRAL={state.neutral_prob:.3f}  BEAR={state.bear_prob:.3f}")
    print(f"  HMM log-likelihood  : {state.log_likelihood:.4f}")
    print(f"\n  Regime distribution (Viterbi path):")
    for regime, frac in dist.items():
        bar = "█" * int(frac * 40)
        print(f"    {regime:8s}: {bar:<40s} {frac:.1%}")

    # Print last 10 regime labels alongside bar data
    print(f"\n  Last {min(10, len(seq))} bars:")
    print(f"  {'Timestamp':<26} {'Close':>10} {'Volume':>10} {'Regime'}")
    print(f"  {'-'*65}")
    for i in range(max(0, len(seq)-10), len(seq)):
        # seq index is offset by 1 (observations = len(closes)-1)
        ci = i + 1  # close index in original array
        reg = seq[i].value
        print(f"  {str(timestamps[ci]):<26} {closes[ci]:>10.2f} {vols[ci]:>10d} {reg}")

    print(f"\n{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="HMM Volatility Regime Analysis")
    parser.add_argument("--symbol", default="RELIANCE")
    parser.add_argument("--bars", type=int, default=120)
    args = parser.parse_args()

    cfg = load_app_config()
    run_regime_analysis(args.symbol, args.bars, cfg.database_url)


if __name__ == "__main__":
    main()

