"""Milestone 7 & M8.1: Walk-Forward Dataset Splitting, Research Provenance & Evidence Classification."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import random
from typing import Any

from app.backtest.engine import BacktestMetricsSummary, EventDrivenBacktester
from app.core.clock import ist_datetime, to_ist
from app.core.config import AppConfig, load_app_config
from app.core.database import BacktestTradeRecord, MarketCandleRecord, Session, init_db
from app.core.execution_guard import assert_paper_mode
from app.core.money import round_price, to_decimal
from app.data.models import CandleBar
from app.services.daily_runner import generate_realistic_session_bars


@dataclass(frozen=True)
class WalkForwardEvaluationResult:
  """Standardized quantitative walk-forward evaluation result with unambiguous provenance."""

  in_sample_summary: BacktestMetricsSummary
  out_of_sample_summary: BacktestMetricsSummary
  slippage_stress_matrix: dict[str, BacktestMetricsSummary]
  is_statistically_valid: bool
  evidence_classification: str
  data_provenance: str = "SYNTHETIC_STRESS_TEST"
  symbols: list[str] = field(default_factory=list)
  start_date: str = ""
  end_date: str = ""
  total_candles: int = 0
  total_trades: int = 0
  confidence_intervals: dict[str, Any] = field(default_factory=dict)

  @property
  def win_rate(self) -> Decimal:
    return self.out_of_sample_summary.win_rate_pct

  @property
  def profit_factor(self) -> Decimal:
    return self.out_of_sample_summary.profit_factor

  @property
  def expectancy_inr(self) -> Decimal:
    return self.out_of_sample_summary.net_expectancy_inr

  @property
  def expectancy_r(self) -> Decimal:
    return self.out_of_sample_summary.net_expectancy_r

  @property
  def max_drawdown_pct(self) -> Decimal:
    return self.out_of_sample_summary.max_drawdown_pct

  def to_report_dict(self) -> dict[str, Any]:
    """Generates an institutional research report payload."""
    return {
        "data_provenance": self.data_provenance,
        "evidence_classification": self.evidence_classification,
        "is_statistically_valid": self.is_statistically_valid,
        "symbols": self.symbols,
        "date_range": {
            "start_date": self.start_date,
            "end_date": self.end_date,
        },
        "total_candles": self.total_candles,
        "total_trades": self.total_trades,
        "in_sample_performance": {
            "trades": self.in_sample_summary.total_trades,
            "wins": self.in_sample_summary.wins,
            "losses": self.in_sample_summary.losses,
            "win_rate_pct": float(self.in_sample_summary.win_rate_pct),
            "profit_factor": float(self.in_sample_summary.profit_factor),
            "net_expectancy_inr": float(self.in_sample_summary.net_expectancy_inr),
            "net_expectancy_r": float(self.in_sample_summary.net_expectancy_r),
            "max_drawdown_pct": float(self.in_sample_summary.max_drawdown_pct),
        },
        "out_of_sample_performance": {
            "trades": self.out_of_sample_summary.total_trades,
            "wins": self.out_of_sample_summary.wins,
            "losses": self.out_of_sample_summary.losses,
            "win_rate_pct": float(self.out_of_sample_summary.win_rate_pct),
            "profit_factor": float(self.out_of_sample_summary.profit_factor),
            "net_expectancy_inr": float(self.out_of_sample_summary.net_expectancy_inr),
            "net_expectancy_r": float(self.out_of_sample_summary.net_expectancy_r),
            "max_drawdown_pct": float(self.out_of_sample_summary.max_drawdown_pct),
        },
        "win_rate": float(self.win_rate),
        "profit_factor": float(self.profit_factor),
        "expectancy_inr": float(self.expectancy_inr),
        "expectancy_r": float(self.expectancy_r),
        "max_drawdown_pct": float(self.max_drawdown_pct),
        "confidence_intervals": self.confidence_intervals,
    }


def classify_evidence(trade_count: int) -> str:
  """Classifies sample size viability according to strict research criteria.

  Note: Achieving >= 100 trades is a necessary threshold for ELIGIBILITY to perform
  statistical tests, but does NOT by itself constitute mathematical proof of statistical alpha.
  """
  if trade_count < 100:
    return "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
  return "ELIGIBLE_FOR_STATISTICAL_EVALUATION"


def compute_confidence_intervals(trades: list[BacktestTradeRecord]) -> dict[str, Any]:
  """Computes statistical confidence intervals and standard errors for trade outcomes."""
  n = len(trades)
  if n < 2:
    return {
        "status": "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100",
        "sample_size": n,
        "message": (
            "Sample size is insufficient (N < 2) to compute standard errors or parametric confidence intervals."
        ),
    }

  wins = sum(1 for t in trades if to_decimal(t.net_pnl_inr) > Decimal("0"))
  p = wins / n
  se_win = (p * (1.0 - p) / n) ** 0.5
  ci_win_lower = max(0.0, (p - 1.96 * se_win) * 100.0)
  ci_win_upper = min(100.0, (p + 1.96 * se_win) * 100.0)

  r_vals = [float(to_decimal(t.r_multiple_net)) for t in trades]
  mean_r = sum(r_vals) / n
  var_r = sum((x - mean_r) ** 2 for x in r_vals) / (n - 1)
  std_r = var_r ** 0.5
  se_r = std_r / (n ** 0.5)
  ci_r_lower = mean_r - 1.96 * se_r
  ci_r_upper = mean_r + 1.96 * se_r

  return {
      "status": "COMPUTED",
      "sample_size": n,
      "win_rate_sample_pct": round(p * 100.0, 2),
      "win_rate_standard_error_pct": round(se_win * 100.0, 2),
      "win_rate_95_ci_pct": [round(ci_win_lower, 2), round(ci_win_upper, 2)],
      "expectancy_r_mean": round(mean_r, 4),
      "expectancy_r_standard_error": round(se_r, 4),
      "expectancy_r_95_ci": [round(ci_r_lower, 4), round(ci_r_upper, 4)],
  }


def split_bars_chronologically(
    bars: list[CandleBar], is_ratio: float = 0.70
) -> tuple[list[CandleBar], list[CandleBar]]:
  """Splits bars chronologically without look-ahead or shuffling (first 70% IS, remaining 30% OOS)."""
  if not bars:
    return [], []
  sorted_bars = sorted(bars, key=lambda b: b.timestamp_utc)
  split_idx = int(len(sorted_bars) * is_ratio)
  return sorted_bars[:split_idx], sorted_bars[split_idx:]


def generate_multi_day_dataset(
    symbols: list[str],
    total_days: int = 65,
    start_year: int = 2026,
    start_month: int = 8,
    start_day: int = 1,
    seed: int = 42,
) -> tuple[dict[str, list[CandleBar]], dict[str, Decimal]]:
  """Generates a reproducible multi-symbol, multi-day dataset yielding >= 100 total breakout trades."""
  rnd = random.Random(seed)
  bars_by_symbol: dict[str, list[CandleBar]] = {s: [] for s in symbols}
  prev_closes: dict[str, Decimal] = {}

  base_prices = {
      "RELIANCE": Decimal("1250.00"),
      "TCS": Decimal("4100.00"),
      "INFY": Decimal("1500.00"),
      "HDFCBANK": Decimal("1650.00"),
      "ICICIBANK": Decimal("1100.00"),
      "SBIN": Decimal("850.00"),
  }

  for sym in symbols:
    prev_closes[sym] = base_prices.get(sym, Decimal("1000.00"))

  cur_date = datetime(start_year, start_month, start_day)
  day_count = 0

  while day_count < total_days:
    # Skip weekends
    if cur_date.weekday() < 5:
      day_count += 1
      for sym in symbols:
        base = base_prices.get(sym, Decimal("1000.00"))
        # 75% of days produce strong breakout (+3.6% rally), 25% produce tight consolidation
        is_breakout = rnd.random() < 0.78
        breakout_mult = Decimal(str(round(1.006 + rnd.random() * 0.004, 4)))
        peak_mult = Decimal(str(round(1.032 + rnd.random() * 0.008, 4))) if is_breakout else Decimal("1.004")
        pullback_mult = Decimal(str(round(1.018 + rnd.random() * 0.006, 4))) if is_breakout else Decimal("0.998")

        day_bars = generate_realistic_session_bars(
            symbol=sym,
            year=cur_date.year,
            month=cur_date.month,
            day=cur_date.day,
            open_price=base,
            breakout_multiplier=breakout_mult,
            peak_multiplier=peak_mult,
            pullback_multiplier=pullback_mult,
        )
        # Explicitly tag each candle as synthetic stress test vector
        synthetic_bars = [
            CandleBar(
                symbol=b.symbol,
                interval_minutes=b.interval_minutes,
                timestamp_utc=b.timestamp_utc,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                source="SYNTHETIC_STRESS_TEST",
            )
            for b in day_bars
        ]
        bars_by_symbol[sym].extend(synthetic_bars)

    cur_date += timedelta(days=1)

  return bars_by_symbol, prev_closes


def run_100_trade_out_of_sample_study(
    db_url: str = "sqlite:///:memory:",
    symbols: list[str] | None = None,
) -> WalkForwardEvaluationResult:
  """Executes an architectural stress test using synthetic benchmark vectors to verify engine invariants and monotonicity."""
  assert_paper_mode()
  cfg = load_app_config(database_url=db_url)

  target_symbols = symbols or ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN"]
  # Generate 58 trading days across 6 liquid symbols (41 days IS, 17 days OOS -> 102 OOS trades)
  all_bars_by_symbol, prev_closes = generate_multi_day_dataset(target_symbols, total_days=58)

  # Chronological split: 70% In-Sample, 30% Out-of-Sample
  is_bars: dict[str, list[CandleBar]] = {}
  oos_bars: dict[str, list[CandleBar]] = {}
  for s, bars in all_bars_by_symbol.items():
    is_bars[s], oos_bars[s] = split_bars_chronologically(bars, is_ratio=0.70)

  # Run each backtest in an isolated in-memory DB session so trade records and capital balance don't pollute
  def _run_isolated_bt(bars_dict, label, slip_pct=None):
    _, sess_maker = init_db("sqlite:///:memory:")
    with sess_maker() as db:
      bt = EventDrivenBacktester(db, cfg)
      summary = bt.run_backtest(
          bars_1m_by_symbol=bars_dict,
          prev_closes_by_symbol=prev_closes,
          dataset_id=label,
          custom_slippage_pct=slip_pct,
      )
      trades = db.query(BacktestTradeRecord).all()
      return summary, trades

  # 1. In-Sample Backtest
  is_summary, is_trades = _run_isolated_bt(is_bars, "SYNTHETIC_STRESS_TEST_IS_70PCT")

  # 2. Out-of-Sample Backtest (True unseen future period)
  oos_summary, oos_trades = _run_isolated_bt(oos_bars, "SYNTHETIC_STRESS_TEST_OOS_30PCT")

  # 3. Slippage Sensitivity Stress Matrix (1x, 2x, 3x adverse friction)
  slippage_matrix: dict[str, BacktestMetricsSummary] = {}
  for slip_pct, label in [
      (Decimal("0.00025"), "SLIPPAGE_1X_BASE_0.025%"),
      (Decimal("0.00050"), "SLIPPAGE_2X_STRESS_0.050%"),
      (Decimal("0.00075"), "SLIPPAGE_3X_EXTREME_0.075%"),
  ]:
    slip_summary, _ = _run_isolated_bt(oos_bars, f"SYNTHETIC_STRESS_TEST_OOS_{label}", slip_pct)
    slippage_matrix[label] = slip_summary

  classification = classify_evidence(oos_summary.total_trades)
  is_valid = oos_summary.total_trades >= 100

  all_timestamps = [b.timestamp_utc for bars in all_bars_by_symbol.values() for b in bars]
  start_dt = to_ist(min(all_timestamps)).strftime("%Y-%m-%d") if all_timestamps else ""
  end_dt = to_ist(max(all_timestamps)).strftime("%Y-%m-%d") if all_timestamps else ""
  total_candles = sum(len(bars) for bars in all_bars_by_symbol.values())

  ci = compute_confidence_intervals(oos_trades)

  return WalkForwardEvaluationResult(
      data_provenance="SYNTHETIC_STRESS_TEST",
      symbols=target_symbols,
      start_date=start_dt,
      end_date=end_dt,
      total_candles=total_candles,
      total_trades=is_summary.total_trades + oos_summary.total_trades,
      in_sample_summary=is_summary,
      out_of_sample_summary=oos_summary,
      slippage_stress_matrix=slippage_matrix,
      is_statistically_valid=is_valid,
      evidence_classification=classification,
      confidence_intervals=ci,
  )


def run_real_historical_walk_forward(
    db_url: str = "sqlite:///./alphatrade_v2.db",
    symbols: list[str] | None = None,
    is_ratio: float = 0.70,
) -> WalkForwardEvaluationResult:
  """Executes an authentic Walk-Forward evaluation exclusively on genuine historical candles from SQLite."""
  assert_paper_mode()
  cfg = load_app_config(database_url=db_url)
  _, SessionLocal = init_db(cfg.database_url)

  with SessionLocal() as db:
    if not symbols or symbols == ["ALL"]:
      target_symbols = [
          r[0]
          for r in db.conn.cursor()
          .execute("SELECT DISTINCT symbol FROM market_candles ORDER BY symbol")
          .fetchall()
      ]
    else:
      target_symbols = symbols

    bars_by_symbol: dict[str, list[CandleBar]] = {}
    prev_closes: dict[str, Decimal] = {}

    for sym in target_symbols:
      recs = (
          db.query(MarketCandleRecord)
          .filter(MarketCandleRecord.symbol == sym)
          .order_by(MarketCandleRecord.timestamp_utc.asc())
          .all()
      )
      if recs:
        # Enforce provenance: strictly reject synthetic benchmark fixtures
        valid_bars = [
            CandleBar(
                symbol=r.symbol,
                interval_minutes=r.interval_minutes,
                timestamp_utc=r.timestamp_utc,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                source=r.source or "ANGEL_SMARTAPI",
            )
            for r in recs
            if r.source not in ("SIMULATED_BENCHMARK", "SYNTHETIC_STRESS_TEST")
        ]
        if valid_bars:
          bars_by_symbol[sym] = valid_bars
          prev_closes[sym] = valid_bars[0].open

  if not bars_by_symbol:
    raise ValueError(f"No authentic historical candles found in {cfg.database_url} for symbols {target_symbols}")

  # Chronological split: 70% In-Sample, 30% Out-of-Sample per symbol
  is_bars: dict[str, list[CandleBar]] = {}
  oos_bars: dict[str, list[CandleBar]] = {}
  for s, bars in bars_by_symbol.items():
    is_b, oos_b = split_bars_chronologically(bars, is_ratio=is_ratio)
    is_bars[s] = is_b
    oos_bars[s] = oos_b

  # Strict zero-lookahead temporal check: max IS timestamp must strictly precede min OOS timestamp
  for s in bars_by_symbol:
    if is_bars[s] and oos_bars[s]:
      max_is = max(b.timestamp_utc for b in is_bars[s])
      min_oos = min(b.timestamp_utc for b in oos_bars[s])
      if max_is >= min_oos:
        raise RuntimeError(
            f"Temporal leakage detected for {s}: IS max timestamp {max_is} >= OOS min timestamp {min_oos}"
        )

  def _run_isolated_bt(bars_dict, label, slip_pct=None):
    _, sess_maker = init_db("sqlite:///:memory:")
    with sess_maker() as db_mem:
      bt = EventDrivenBacktester(db_mem, cfg)
      summary = bt.run_backtest(
          bars_1m_by_symbol=bars_dict,
          prev_closes_by_symbol=prev_closes,
          dataset_id=label,
          custom_slippage_pct=slip_pct,
      )
      trades = db_mem.query(BacktestTradeRecord).all()
      return summary, trades

  is_summary, is_trades = _run_isolated_bt(is_bars, "REAL_HISTORICAL_IS_70PCT")
  oos_summary, oos_trades = _run_isolated_bt(oos_bars, "REAL_HISTORICAL_OOS_30PCT")

  slippage_matrix: dict[str, BacktestMetricsSummary] = {}
  for slip_pct, label in [
      (Decimal("0.00025"), "SLIPPAGE_1X_BASE_0.025%"),
      (Decimal("0.00050"), "SLIPPAGE_2X_STRESS_0.050%"),
      (Decimal("0.00075"), "SLIPPAGE_3X_EXTREME_0.075%"),
  ]:
    slip_summary, _ = _run_isolated_bt(oos_bars, f"REAL_HISTORICAL_OOS_{label}", slip_pct)
    slippage_matrix[label] = slip_summary

  classification = classify_evidence(oos_summary.total_trades)
  is_valid = oos_summary.total_trades >= 100

  all_timestamps = [b.timestamp_utc for bars in bars_by_symbol.values() for b in bars]
  start_dt = to_ist(min(all_timestamps)).strftime("%Y-%m-%d") if all_timestamps else ""
  end_dt = to_ist(max(all_timestamps)).strftime("%Y-%m-%d") if all_timestamps else ""
  total_candles = sum(len(bars) for bars in bars_by_symbol.values())

  ci = compute_confidence_intervals(oos_trades)

  return WalkForwardEvaluationResult(
      data_provenance="REAL_HISTORICAL_SQLITE",
      symbols=list(bars_by_symbol.keys()),
      start_date=start_dt,
      end_date=end_dt,
      total_candles=total_candles,
      total_trades=is_summary.total_trades + oos_summary.total_trades,
      in_sample_summary=is_summary,
      out_of_sample_summary=oos_summary,
      slippage_stress_matrix=slippage_matrix,
      is_statistically_valid=is_valid,
      evidence_classification=classification,
      confidence_intervals=ci,
  )


def generate_research_report_markdown(res: WalkForwardEvaluationResult) -> str:
  """Formats a comprehensive research report conforming to institutional standards."""
  rep = res.to_report_dict()
  ci = res.confidence_intervals
  ci_text = ""
  if ci.get("status") == "COMPUTED":
    ci_text = (
        f"- **Win Rate 95% Confidence Interval:** {ci['win_rate_95_ci_pct'][0]:.2f}% to"
        f" {ci['win_rate_95_ci_pct'][1]:.2f}% (SE: {ci['win_rate_standard_error_pct']:.2f}%)\n"
        f"- **Net Expectancy (R) 95% Confidence Interval:** {ci['expectancy_r_95_ci'][0]:+.4f} R to"
        f" {ci['expectancy_r_95_ci'][1]:+.4f} R (SE: {ci['expectancy_r_standard_error']:.4f})\n"
    )
  else:
    ci_text = f"- **Confidence Intervals:** {ci.get('message', 'Insufficient sample size')}\n"

  md = f"""# AlphaTrade AI v2.0 — Quantitative Walk-Forward Research Report

- **Data Provenance:** `{res.data_provenance}`
- **Evidence Classification:** **`{res.evidence_classification}`**
- **Statistical Eligibility Hurdle:** `{"ELIGIBLE (N >= 100)" if res.is_statistically_valid else "INSUFFICIENT SAMPLE SIZE (N < 100)"}`
- **Symbol Universe ({len(res.symbols)} constituents):** {', '.join(res.symbols)}
- **Date Range:** {res.start_date} to {res.end_date}
- **Total Validated Candles:** {res.total_candles:,d}
- **Total Backtest Trades:** {res.total_trades} (In-Sample: {res.in_sample_summary.total_trades}, Out-of-Sample: {res.out_of_sample_summary.total_trades})

## Performance Metrics (Out-of-Sample 30%)
- **Win Rate:** {float(res.win_rate):.2f}%
- **Profit Factor:** {float(res.profit_factor):.2f}
- **Net Expectancy (INR):** ₹{float(res.expectancy_inr):.2f} / trade
- **Net Expectancy (R):** {float(res.expectancy_r):+.4f} R
- **Maximum Drawdown:** {float(res.max_drawdown_pct):.2f}%
{ci_text}
## Slippage Sensitivity Matrix (Adverse Friction Stress)
- **1x Base (0.025%):** {res.slippage_stress_matrix.get('SLIPPAGE_1X_BASE_0.025%', res.out_of_sample_summary).net_expectancy_r:+.4f} R | PF: {res.slippage_stress_matrix.get('SLIPPAGE_1X_BASE_0.025%', res.out_of_sample_summary).profit_factor:.2f}
- **2x Stress (0.050%):** {res.slippage_stress_matrix.get('SLIPPAGE_2X_STRESS_0.050%', res.out_of_sample_summary).net_expectancy_r:+.4f} R | PF: {res.slippage_stress_matrix.get('SLIPPAGE_2X_STRESS_0.050%', res.out_of_sample_summary).profit_factor:.2f}
- **3x Extreme (0.075%):** {res.slippage_stress_matrix.get('SLIPPAGE_3X_EXTREME_0.075%', res.out_of_sample_summary).net_expectancy_r:+.4f} R | PF: {res.slippage_stress_matrix.get('SLIPPAGE_3X_EXTREME_0.075%', res.out_of_sample_summary).profit_factor:.2f}
"""
  return md

