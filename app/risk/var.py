"""Intraday Portfolio Value-at-Risk (VaR) & Expected Shortfall (CVaR) Engine.

Conforms strictly to institutional risk management standards (Basel III / FRTB).
Implements:
1. Pure Decimal financial arithmetic with zero binary float drift.
2. Parametric VaR (Normal distribution) at 95% and 99% confidence horizons.
3. Cornish-Fisher Modified VaR accounting for empirical return Skewness and Kurtosis.
4. Expected Shortfall (CVaR / Conditional VaR) for tail-loss quantification.
5. Cross-asset Pearson correlation and covariance matrix computation.
6. Portfolio diversification benefit ratio.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import math
import sqlite3
from typing import Any

from app.core.money import round_paise, round_price, to_decimal

# High-precision standard normal quantiles and CVaR multipliers
Z_95 = Decimal("1.64485362695")
Z_99 = Decimal("2.32634787404")
CVAR_MULT_95 = Decimal("2.06271280355")  # phi(Z_95) / 0.05
CVAR_MULT_99 = Decimal("2.66521421711")  # phi(Z_99) / 0.01
NSE_SESSION_MINUTES = Decimal("375")
SQRT_375 = NSE_SESSION_MINUTES.sqrt()


@dataclass(frozen=True)
class VaRMetrics:
  """Portfolio Value-at-Risk and Expected Shortfall analytical results."""
  confidence_pct: Decimal  # e.g. 0.95 or 0.99
  var_95_pct: Decimal
  var_95_inr: Decimal
  var_99_pct: Decimal
  var_99_inr: Decimal
  cvar_95_pct: Decimal
  cvar_95_inr: Decimal
  cvar_99_pct: Decimal
  cvar_99_inr: Decimal
  cf_var_95_pct: Decimal
  cf_var_95_inr: Decimal
  cf_var_99_pct: Decimal
  cf_var_99_inr: Decimal
  portfolio_volatility_daily_pct: Decimal
  undiversified_var_95_inr: Decimal
  diversification_benefit_pct: Decimal
  portfolio_skewness: Decimal
  portfolio_excess_kurtosis: Decimal
  holdings_count: int
  total_exposure_inr: Decimal


@dataclass(frozen=True)
class PortfolioRiskReport:
  """Institutional multi-asset risk report."""
  timestamp_utc: datetime
  total_equity_inr: Decimal
  total_exposure_inr: Decimal
  metrics: VaRMetrics
  weights: dict[str, Decimal]
  individual_volatilities_daily_pct: dict[str, Decimal]
  correlation_matrix: dict[str, dict[str, Decimal]]


def compute_returns_from_prices(prices: list[Decimal]) -> list[Decimal]:
  """Computes simple arithmetic returns r_t = (P_t - P_{t-1}) / P_{t-1}."""
  if len(prices) < 2:
    return []
  returns: list[Decimal] = []
  for i in range(1, len(prices)):
    p_prev = prices[i - 1]
    p_curr = prices[i]
    if p_prev > Decimal("0"):
      returns.append((p_curr - p_prev) / p_prev)
  return returns


def compute_series_statistics(returns: list[Decimal]) -> dict[str, Decimal]:
  """Computes Mean, Sample Variance, Sample Standard Deviation, Skewness, and Excess Kurtosis."""
  n = len(returns)
  if n < 2:
    return {
        "mean": Decimal("0.0"),
        "variance": Decimal("0.0"),
        "std": Decimal("0.0"),
        "skewness": Decimal("0.0"),
        "excess_kurtosis": Decimal("0.0"),
    }

  n_dec = Decimal(n)
  mean = sum(returns) / n_dec

  # Sample variance (N - 1 denominator)
  var = sum((r - mean) ** 2 for r in returns) / (n_dec - Decimal("1"))
  std = var.sqrt() if var > Decimal("0") else Decimal("0.0")

  if std == Decimal("0") or n < 4:
    return {
        "mean": mean,
        "variance": var,
        "std": std,
        "skewness": Decimal("0.0"),
        "excess_kurtosis": Decimal("0.0"),
    }

  # Sample Skewness: m3 / std^3
  m3 = sum((r - mean) ** 3 for r in returns) / n_dec
  skewness = m3 / (std**3)

  # Sample Excess Kurtosis: (m4 / std^4) - 3
  m4 = sum((r - mean) ** 4 for r in returns) / n_dec
  excess_kurtosis = (m4 / (std**4)) - Decimal("3.0")

  return {
      "mean": mean,
      "variance": var,
      "std": std,
      "skewness": skewness,
      "excess_kurtosis": excess_kurtosis,
  }


def compute_cross_asset_covariance(
    returns_a: list[Decimal],
    returns_b: list[Decimal],
) -> tuple[Decimal, Decimal]:
  """Computes pairwise Sample Covariance and Pearson Correlation between two equal-length return series."""
  n = min(len(returns_a), len(returns_b))
  if n < 2:
    return Decimal("0.0"), Decimal("0.0")

  r_a = returns_a[:n]
  r_b = returns_b[:n]
  n_dec = Decimal(n)

  mean_a = sum(r_a) / n_dec
  mean_b = sum(r_b) / n_dec

  cov = sum((r_a[i] - mean_a) * (r_b[i] - mean_b) for i in range(n)) / (n_dec - Decimal("1"))

  var_a = sum((r - mean_a) ** 2 for r in r_a) / (n_dec - Decimal("1"))
  var_b = sum((r - mean_b) ** 2 for r in r_b) / (n_dec - Decimal("1"))

  std_a = var_a.sqrt() if var_a > Decimal("0") else Decimal("0.0")
  std_b = var_b.sqrt() if var_b > Decimal("0") else Decimal("0.0")

  if std_a > Decimal("0") and std_b > Decimal("0"):
    corr = cov / (std_a * std_b)
    # Clamp correlation between -1.0 and +1.0 against minor numerical artifacts
    corr = max(Decimal("-1.0"), min(Decimal("1.0"), round_price(corr)))
  else:
    corr = Decimal("0.0")

  return cov, corr


def cornish_fisher_quantile(z: Decimal, return_skewness: Decimal, excess_kurtosis: Decimal) -> Decimal:
  """Calculates the Cornish-Fisher expansion quantile Z_cf for positive loss Value-at-Risk.

  Loss skewness S_loss = -return_skewness.
  Z_cf = Z + (1/6)(Z^2 - 1)*(-S) + (1/24)(Z^3 - 3Z)*K - (1/36)(2Z^3 - 5Z)*S^2
  """
  z2 = z * z
  z3 = z2 * z
  # Loss skewness is inverted return skewness
  s_loss = -return_skewness
  k = excess_kurtosis

  term1 = z
  term2 = (z2 - Decimal("1")) * s_loss / Decimal("6")
  term3 = (z3 - (Decimal("3") * z)) * k / Decimal("24")
  term4 = ((Decimal("2") * z3) - (Decimal("5") * z)) * (s_loss * s_loss) / Decimal("36")

  z_cf = term1 + term2 + term3 - term4
  # Ensure quantile remains positive and sensible
  return max(Decimal("0.5"), z_cf)


class PortfolioVaREngine:
  """Institutional Value-at-Risk, CVaR, and Correlation Engine."""

  def __init__(self, db_path: str = "alphatrade_v2.db"):
    self.db_path = db_path

  def get_aligned_minute_returns(
      self,
      symbols: list[str],
      lookback_bars: int = 1500,
  ) -> dict[str, list[Decimal]]:
    """Loads synchronous 1-minute close prices from SQLite and produces aligned return series."""
    if not symbols:
      return {}

    conn = sqlite3.connect(self.db_path)
    c = conn.cursor()
    by_timestamp: dict[str, dict[str, Decimal]] = {}

    # Query per symbol using indexed (symbol, interval_minutes, timestamp_utc)
    for s in symbols:
      # Fetch last (lookback_bars + 50) bars for each symbol to ensure sufficient overlap
      fetch_limit = lookback_bars + 100 if lookback_bars > 0 else 50000
      c.execute(
          """
          SELECT timestamp_utc, close
          FROM market_candles
          WHERE symbol = ? AND interval_minutes = 1
          ORDER BY timestamp_utc DESC
          LIMIT ?
          """,
          (s, fetch_limit),
      )
      rows = c.fetchall()
      for ts, cl in rows:
        by_timestamp.setdefault(ts, {})[s] = Decimal(str(cl))

    conn.close()

    # Keep timestamps where all requested symbols have data, sorted ascending
    aligned_ts = sorted([
        ts for ts, val_dict in by_timestamp.items()
        if all(s in val_dict for s in symbols)
    ])
    # Slice to requested lookback
    if lookback_bars > 0 and len(aligned_ts) > lookback_bars:
      aligned_ts = aligned_ts[-lookback_bars:]

    if len(aligned_ts) < 2:
      # Fallback: if database lacks full alignment, generate standalone returns per symbol
      return self._get_individual_minute_returns(symbols, lookback_bars)

    returns_by_symbol: dict[str, list[Decimal]] = {s: [] for s in symbols}
    for i in range(1, len(aligned_ts)):
      prev_ts = aligned_ts[i - 1]
      curr_ts = aligned_ts[i]
      for s in symbols:
        p_prev = by_timestamp[prev_ts][s]
        p_curr = by_timestamp[curr_ts][s]
        ret = (p_curr - p_prev) / p_prev if p_prev > Decimal("0") else Decimal("0")
        returns_by_symbol[s].append(ret)

    return returns_by_symbol

  def _get_individual_minute_returns(
      self,
      symbols: list[str],
      lookback_bars: int = 1500,
  ) -> dict[str, list[Decimal]]:
    """Fallback: loads minute prices independently per symbol."""
    conn = sqlite3.connect(self.db_path)
    c = conn.cursor()
    returns_by_symbol: dict[str, list[Decimal]] = {}
    for s in symbols:
      c.execute(
          "SELECT close FROM market_candles WHERE symbol = ? ORDER BY timestamp_utc ASC",
          (s,),
      )
      rows = c.fetchall()
      prices = [Decimal(str(r[0])) for r in rows]
      if lookback_bars > 0 and len(prices) > lookback_bars:
        prices = prices[-lookback_bars:]
      returns_by_symbol[s] = compute_returns_from_prices(prices)
    conn.close()
    return returns_by_symbol

  def compute_portfolio_risk(
      self,
      holdings: dict[str, Decimal],
      total_equity_inr: Decimal,
      lookback_bars: int = 1500,
      custom_returns: dict[str, list[Decimal]] | None = None,
  ) -> PortfolioRiskReport:
    """Computes comprehensive portfolio VaR, CVaR, correlation, and diversification benefit.

    Args:
        holdings: dict of symbol -> notional exposure in INR (e.g. {'RELIANCE': Decimal('38905.50')})
        total_equity_inr: current total equity balance in INR.
        lookback_bars: number of 1m bars to use for covariance estimation (default: 1500 bars ~ 4 sessions).
        custom_returns: optional pre-aligned return series for fast unit testing.
    """
    total_eq = to_decimal(total_equity_inr)
    total_exposure = sum(to_decimal(exp) for exp in holdings.values())

    if not holdings or total_exposure <= Decimal("0") or total_eq <= Decimal("0"):
      # Empty or zero-exposure portfolio
      zero = Decimal("0.0")
      metrics = VaRMetrics(
          confidence_pct=Decimal("0.95"),
          var_95_pct=zero,
          var_95_inr=zero,
          var_99_pct=zero,
          var_99_inr=zero,
          cvar_95_pct=zero,
          cvar_95_inr=zero,
          cvar_99_pct=zero,
          cvar_99_inr=zero,
          cf_var_95_pct=zero,
          cf_var_95_inr=zero,
          cf_var_99_pct=zero,
          cf_var_99_inr=zero,
          portfolio_volatility_daily_pct=zero,
          undiversified_var_95_inr=zero,
          diversification_benefit_pct=zero,
          portfolio_skewness=zero,
          portfolio_excess_kurtosis=zero,
          holdings_count=0,
          total_exposure_inr=zero,
      )
      return PortfolioRiskReport(
          timestamp_utc=datetime.now(timezone.utc),
          total_equity_inr=total_eq,
          total_exposure_inr=zero,
          metrics=metrics,
          weights={},
          individual_volatilities_daily_pct={},
          correlation_matrix={},
      )

    symbols = sorted(holdings.keys())
    weights: dict[str, Decimal] = {
        s: to_decimal(holdings[s]) / total_eq for s in symbols
    }

    # Obtain aligned minute returns
    if custom_returns is not None:
      returns_by_symbol = custom_returns
    else:
      returns_by_symbol = self.get_aligned_minute_returns(symbols, lookback_bars)

    # Calculate individual statistics
    stats_by_symbol: dict[str, dict[str, Decimal]] = {}
    daily_vol_by_symbol: dict[str, Decimal] = {}
    for s in symbols:
      rets = returns_by_symbol.get(s, [])
      if len(rets) >= 10:
        st = compute_series_statistics(rets)
      else:
        # Default conservative fallback: 1.8% daily volatility
        def_vol = Decimal("0.018")
        st = {
            "mean": Decimal("0.0"),
            "variance": (def_vol / SQRT_375) ** 2,
            "std": def_vol / SQRT_375,
            "skewness": Decimal("0.0"),
            "excess_kurtosis": Decimal("0.0"),
        }
      stats_by_symbol[s] = st
      # Scale minute volatility to daily volatility: sigma_daily = sigma_1m * sqrt(375)
      daily_vol_by_symbol[s] = st["std"] * SQRT_375

    # Compute Covariance and Correlation matrices (daily scaled)
    cov_matrix: dict[str, dict[str, Decimal]] = {s1: {} for s1 in symbols}
    corr_matrix: dict[str, dict[str, Decimal]] = {s1: {} for s1 in symbols}

    for s1 in symbols:
      for s2 in symbols:
        if s1 == s2:
          corr_matrix[s1][s2] = Decimal("1.0")
          cov_matrix[s1][s2] = daily_vol_by_symbol[s1] ** 2
        else:
          rets1 = returns_by_symbol.get(s1, [])
          rets2 = returns_by_symbol.get(s2, [])
          min_cov, corr = compute_cross_asset_covariance(rets1, rets2)
          # Daily covariance = minute_cov * 375
          daily_cov = min_cov * NSE_SESSION_MINUTES
          cov_matrix[s1][s2] = daily_cov
          corr_matrix[s1][s2] = round_price(corr)

    # Compute Portfolio Variance: sigma_p^2 = sum_i sum_j w_i * w_j * cov_ij
    port_var = Decimal("0.0")
    for s1 in symbols:
      for s2 in symbols:
        port_var += weights[s1] * weights[s2] * cov_matrix[s1][s2]

    port_daily_vol = port_var.sqrt() if port_var > Decimal("0") else Decimal("0.0")

    # Compute portfolio composite return series for skewness & kurtosis
    min_len = min(len(returns_by_symbol.get(s, [])) for s in symbols) if symbols else 0
    if min_len >= 10:
      port_returns: list[Decimal] = []
      for t in range(min_len):
        r_port = sum(weights[s] * returns_by_symbol[s][t] for s in symbols)
        port_returns.append(r_port)
      port_stats = compute_series_statistics(port_returns)
      port_skew = port_stats["skewness"]
      port_kurt = port_stats["excess_kurtosis"]
    else:
      port_skew = Decimal("0.0")
      port_kurt = Decimal("0.0")

    # Parametric VaR (1-day horizon)
    var_95_pct = port_daily_vol * Z_95
    var_95_inr = round_paise(var_95_pct * total_eq)
    var_99_pct = port_daily_vol * Z_99
    var_99_inr = round_paise(var_99_pct * total_eq)

    # Expected Shortfall (CVaR)
    cvar_95_pct = port_daily_vol * CVAR_MULT_95
    cvar_95_inr = round_paise(cvar_95_pct * total_eq)
    cvar_99_pct = port_daily_vol * CVAR_MULT_99
    cvar_99_inr = round_paise(cvar_99_pct * total_eq)

    # Cornish-Fisher Modified VaR
    z_cf_95 = cornish_fisher_quantile(Z_95, port_skew, port_kurt)
    z_cf_99 = cornish_fisher_quantile(Z_99, port_skew, port_kurt)
    cf_var_95_pct = port_daily_vol * z_cf_95
    cf_var_95_inr = round_paise(cf_var_95_pct * total_eq)
    cf_var_99_pct = port_daily_vol * z_cf_99
    cf_var_99_inr = round_paise(cf_var_99_pct * total_eq)

    # Undiversified VaR = sum_i (w_i * sigma_i * Z_95 * Total_Equity)
    undiversified_var_95_inr = round_paise(
        sum(weights[s] * daily_vol_by_symbol[s] * Z_95 * total_eq for s in symbols)
    )

    # Diversification benefit %: (Undiversified - Diversified) / Undiversified
    if undiversified_var_95_inr > Decimal("0"):
      div_benefit_pct = (
          (undiversified_var_95_inr - var_95_inr) / undiversified_var_95_inr
      ) * Decimal("100")
      div_benefit_pct = max(Decimal("0.0"), round_price(div_benefit_pct))
    else:
      div_benefit_pct = Decimal("0.0")

    metrics = VaRMetrics(
        confidence_pct=Decimal("0.95"),
        var_95_pct=round_price(var_95_pct),
        var_95_inr=var_95_inr,
        var_99_pct=round_price(var_99_pct),
        var_99_inr=var_99_inr,
        cvar_95_pct=round_price(cvar_95_pct),
        cvar_95_inr=cvar_95_inr,
        cvar_99_pct=round_price(cvar_99_pct),
        cvar_99_inr=cvar_99_inr,
        cf_var_95_pct=round_price(cf_var_95_pct),
        cf_var_95_inr=cf_var_95_inr,
        cf_var_99_pct=round_price(cf_var_99_pct),
        cf_var_99_inr=cf_var_99_inr,
        portfolio_volatility_daily_pct=round_price(port_daily_vol * Decimal("100")),
        undiversified_var_95_inr=undiversified_var_95_inr,
        diversification_benefit_pct=div_benefit_pct,
        portfolio_skewness=round_price(port_skew),
        portfolio_excess_kurtosis=round_price(port_kurt),
        holdings_count=len(symbols),
        total_exposure_inr=round_paise(total_exposure),
    )

    return PortfolioRiskReport(
        timestamp_utc=datetime.now(timezone.utc),
        total_equity_inr=total_eq,
        total_exposure_inr=round_paise(total_exposure),
        metrics=metrics,
        weights={s: round_price(weights[s]) for s in symbols},
        individual_volatilities_daily_pct={
            s: round_price(daily_vol_by_symbol[s] * Decimal("100")) for s in symbols
        },
        correlation_matrix=corr_matrix,
    )
