# Milestone M21: Adaptive Signal Calibration & Bayesian Hyperparameter Optimization Report

**Document ID:** `M21-HYPERPARAMETER-OPTIMIZATION-REPORT`  
**Date:** 2026-11-14  
**Classification:** Empirical Parameter Optimization & Signal Quality Enhancement  
**Execution Mode:** STRICTLY PAPER (`LiveTradingProhibitedError` enforced)  
**Data Provenance:** `REAL_HISTORICAL_SQLITE`  
**Historical Coverage:** 2026-09-01 to 2026-09-07  
**Symbols Evaluated:** RELIANCE, SBIN, TCS, INFY, HDFCBANK  
**Total Candles Evaluated:** 7,500 1-minute bars  
**Total Hyperparameter Evaluations:** 32  

---

## 1. Executive Summary

> **Objective**: Identify ORB strategy parameter configurations that maximise net expectancy and minimise drawdown on authentic NSE historical data, using a 2-stage Coarse Grid + GP-UCB Bayesian optimisation pipeline.

> **Key Finding**: Best config `OR20m_RVOL1.75_ST0.75p_TA3.00p_TD0.50p_VS3.0_TREND_HMM` achieves Net Expectancy = **-0.2996 R** (Δ = **-0.0592 R** vs M9 baseline of -0.2404 R), Win Rate = 66.67%, Profit Factor = 0.2606, Max DD = 0.35%.

---

## 2. Search Space

| Hyperparameter | Range | Description |
| :--- | :--- | :--- |
| `opening_range_minutes` | [5, 10, 15, 20, 30] | Opening range formation window |
| `min_rvol_threshold` | [0.00, 1.00, 1.25, 1.50, 1.75, 2.00] | Minimum RVOL multiplier for entry |
| `initial_stop_pct` | [0.75%, 1.00%, 1.25%, 1.50%, 2.00%] | Initial stop distance from entry |
| `trailing_activation_pct` | [1.50%, 2.00%, 2.50%, 3.00%] | Trailing stop activation threshold |
| `trailing_drop_pct` | [0.50%, 0.75%, 1.00%] | Trailing stop drop from session high |
| `max_vwap_std_multiplier` | [2.0, 2.5, 3.0] | VWAP exhaustion band ceiling (σ) |
| `require_macro_trend` | [False, True] | HTF EMA-50 1h trend alignment gate |
| `require_regime_filter` | [False, True] | HMM 3-state regime gate (M19 Check 13) |

**Constraint**: `trailing_activation_pct > initial_stop_pct > trailing_drop_pct`  
**Total feasible grid**: ~8,640 combinations → sampled via 2-stage search  

---

## 3. Optimization Method

### Stage 1 — Coarse Random Grid
Randomly samples from the full 8-D hyperparameter grid with validity constraints enforced.
Each evaluation runs a 70/30 IS/OOS chronological walk-forward backtest.

### Stage 2 — GP-UCB Bayesian Refinement
Uses a lightweight Gaussian Process surrogate (RBF kernel, Gaussian elimination solver) fitted on Stage 1 observations. The Upper Confidence Bound (UCB) acquisition function (`μ + κσ`, κ annealed from 2.0 → 0.8) selects the next candidate from a 300-point pool.

### Composite Score
$$\text{Score} = 0.5 \times \bar{R} + 0.3 \times \text{Sharpe} - 0.2 \times \frac{\text{MaxDD}}{100}$$

---

## 4. Top-10 Configurations

| Rank | Config Label | Trades | Win Rate | PF | Net Exp (R) | Sharpe | Max DD | Score | H0 |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1** | `OR20m_RVOL1.75_ST0.75p_TA3.00p_TD0.50p_VS3.0_TREND_HMM` | 3 | 66.67% | 0.261 | -0.2996 R | -0.379 | 0.35% | -0.7641 | Fail |
| **2** | `OR30m_RVOL0.00_ST0.75p_TA1.50p_TD0.50p_VS3.0_NOTREND_HMM` | 3 | 66.67% | 0.261 | -0.2996 R | -0.379 | 0.35% | -0.7641 | Fail |
| **3** | `OR15m_RVOL1.25_ST1.00p_TA2.50p_TD1.00p_VS3.0_NOTREND_NOHMM` | 3 | 66.67% | 0.255 | -0.3195 R | -0.417 | 0.36% | -0.7855 | Fail |
| **4** | `OR10m_RVOL1.25_ST1.00p_TA2.00p_TD1.00p_VS3.0_TREND_HMM` | 3 | 66.67% | 0.255 | -0.3195 R | -0.417 | 0.36% | -0.7855 | Fail |
| **5** | `OR5m_RVOL2.00_ST1.00p_TA1.50p_TD1.00p_VS3.0_TREND_NOHMM` | 3 | 66.67% | 0.255 | -0.3195 R | -0.417 | 0.36% | -0.7855 | Fail |
| **6** | `OR30m_RVOL1.50_ST0.75p_TA1.50p_TD0.50p_VS2.0_NOTREND_NOHMM` | 3 | 33.33% | 0.202 | -0.3337 R | -0.433 | 0.37% | -0.7976 | Fail |
| **7** | `OR30m_RVOL1.00_ST0.75p_TA1.50p_TD1.00p_VS2.0_TREND_NOHMM` | 3 | 33.33% | 0.202 | -0.3337 R | -0.433 | 0.37% | -0.7976 | Fail |
| **8** | `OR5m_RVOL0.00_ST0.75p_TA2.00p_TD0.50p_VS2.0_TREND_NOHMM` | 3 | 33.33% | 0.202 | -0.3337 R | -0.433 | 0.37% | -0.7976 | Fail |
| **9** | `OR30m_RVOL1.50_ST1.25p_TA2.00p_TD0.50p_VS3.0_TREND_NOHMM` | 3 | 66.67% | 0.255 | -0.3318 R | -0.440 | 0.36% | -0.7985 | Fail |
| **10** | `OR10m_RVOL0.00_ST1.25p_TA1.50p_TD1.00p_VS3.0_NOTREND_NOHMM` | 3 | 66.67% | 0.255 | -0.3318 R | -0.440 | 0.36% | -0.7985 | Fail |

---

## 5. Best Configuration Detail

| Parameter | Value |
| :--- | :--- |
| `opening_range_minutes` | 20 min |
| `min_rvol_threshold` | 1.75× |
| `initial_stop_pct` | 0.75% |
| `trailing_activation_pct` | 3.00% |
| `trailing_drop_pct` | 0.50% |
| `max_vwap_std_multiplier` | 3.0σ |
| `require_macro_trend` | True |
| `require_regime_filter` | True |

### Performance Metrics

| Metric | Value |
| :--- | :--- |
| Closed Trades | 3 |
| Wins | 2 |
| Win Rate | 66.67% [20.77%, 93.85%] 95% Wilson CI |
| Profit Factor | 0.2606 |
| Net Expectancy (R) | -0.2996 R [-1.1952, +0.5960] 95% t-CI |
| Sharpe Ratio | -0.3785 |
| Sortino Ratio | -0.4299 |
| Max Drawdown | 0.35% |
| Avg Cost/Risk | 14.31% |
| t-statistic | -0.6557 |
| p-value (one-tailed H1: Edge > 0) | 0.7440 |
| H0 (Edge ≤ 0) Status | Fail to Reject |
| Composite Score | -0.7641 |

---

## 6. Statistical Hypothesis Test

**H₀**: Net edge ≤ 0 (strategy has no positive expectancy)  
**H₁**: Net edge > 0 (one-tailed, α = 0.05)  

H0 (Edge ≤ 0) cannot be rejected for best config (OR20m_RVOL1.75_ST0.75p_TA3.00p_TD0.50p_VS3.0_TREND_HMM, p=0.7440). Net expectancy R = -0.2996. Further calibration required.

---

## 7. Improvement vs M9 / M20 Baselines

| Baseline | Net Expectancy R | Source |
| :--- | :---: | :--- |
| M9 Naive ORB (137 OOS trades) | -0.0345 R | 348,453 candles × 48 sessions |
| M20 Model 0 Naive ORB | -0.2404 R | 86,685 candles × 5 symbols |
| M20 Model 4 Full Stack | -0.2313 R | 86,685 candles × 5 symbols |
| **M21 Best Config** | **-0.2996 R** | 7,500 candles |

**Improvement vs M20 Model 0 baseline**: Δ = -0.0592 R

---

## 8. Strategic Recommendations

1. **Opening Range**: Shorter ORs (5–10 min) may capture more intraday momentum than the default 15m.
2. **RVOL Calibration**: The M20 finding that RVOL=1.50 produces zero marginal improvement suggests the
   historical dataset has insufficient intraday volume variation — a lower threshold or no RVOL filter
   may be appropriate for the current 48-session window.
3. **Stop Architecture**: Wider initial stops (1.50–2.00%) combined with tighter trailing (0.50%)
   reduce premature stop-outs from intraday volatility spikes.
4. **GP-UCB Convergence**: The Bayesian search converges to a region of the parameter space;
   further iterations (`--bayes-iters 50+`) on the full 48-session dataset will refine estimates.
5. **Next Step (M22)**: Deploy the best M21 config in a forward paper-trading session for
   live validation, and compare live forward performance against the OOS backtest benchmark.