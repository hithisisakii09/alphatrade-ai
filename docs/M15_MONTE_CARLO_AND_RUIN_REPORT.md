# AlphaTrade AI v2.0 — Milestone M15 Monte Carlo & Risk of Ruin Report

**Document ID:** `M15-MONTE-CARLO-AND-RUIN-REPORT`  
**Date:** 2026-11-12  
**Execution Mode:** STRICTLY PAPER ONLY (`LiveTradingProhibitedError` fail-closed enforced)  
**Primary Feeder:** Angel One SmartAPI (`getCandleData` with RFC 6238 TOTP)  
**Active Strategy:** `ORB_INTRADAY_V2` (15m Opening Range 09:15–09:30 IST &rarr; 5m Breakout)  
**Verification Status:** 62/62 Tests Passing (0 Regressions)  

---

## 1. Executive Summary

Milestone M15 establishes institutional statistical robustness for **AlphaTrade AI v2.0** by incorporating a non-parametric Monte Carlo bootstrap resampling engine and an automated Risk of Ruin (RoR) simulator.

Backtested performance metrics are inherently susceptible to **sequence risk**—the path-dependent order in which trades occur. A trading strategy with positive aggregate expectancy can still suffer unacceptable drawdowns or account failure if losses cluster prematurely before capital accumulates.

Milestone M15 introduces:
1. **Non-Parametric Bootstrap Resampling**:
   - Generates $N = 2,000$ to $10,000$ synthetic equity trajectories by sampling observed trade returns with replacement from the unified historical and live paper trade pool ($M = 559$ trades).
2. **Path-Independent Risk of Ruin (RoR) Analysis**:
   - Computes the empirical probability that equity drops below specified drawdown barriers:
     - $\mathbb{P}(\text{Drawdown} \ge 5\%)$
     - $\mathbb{P}(\text{Drawdown} \ge 10\%)$
     - $\mathbb{P}(\text{Drawdown} \ge 15\%)$
3. **Resampled Drawdown & Capital Distributions**:
   - Identifies the 50th percentile (median), 95th percentile (95% Value-at-Risk), and 99th percentile extreme drawdown thresholds.
4. **Interactive Dashboard & CLI**:
   - Standalone CLI [`scripts/run_monte_carlo.py`](file:///c:/Users/akhil/Desktop/Workspace/alphatrade-ai/scripts/run_monte_carlo.py).
   - Live API endpoint `GET /research/monte-carlo`.
   - Dedicated Monte Carlo interactive widget on Tab 6 (Backtest & Research) of the dashboard.

---

## 2. Mathematical Formulations

### 2.1 Non-Parametric Bootstrap

Given empirical trade outcomes $\{r_1, r_2, \dots, r_M\}$:

For each Monte Carlo simulation iteration $k \in \{1, \dots, K\}$:
1. Draw $M$ independent trades with replacement:
   $$\{r_{k,1}^*, r_{k,2}^*, \dots, r_{k,M}^*\} \sim \text{Uniform}(\{r_1, \dots, r_M\})$$
2. Construct simulated equity path $E_{k,t}$:
   $$E_{k,0} = \text{Starting Capital}$$
   $$E_{k,t} = E_{k,t-1} + r_{k,t}^* \quad \text{for } t = 1 \dots M$$

### 2.2 Drawdown Formulation

$$\text{Peak}_{k,t} = \max_{0 \le s \le t} (E_{k,s})$$

$$D_{k,t} = \frac{\text{Peak}_{k,t} - E_{k,t}}{\text{Peak}_{k,t}}$$

$$\text{Max Drawdown}_k = \max_{1 \le t \le M} (D_{k,t})$$

### 2.3 Probability of Ruin

Given a risk tolerance threshold $\tau_{\text{ruin}}$ (default: $10.0\%$):

$$\text{Ruin Event}_k = \begin{cases} 1 & \text{if } \text{Max Drawdown}_k \ge \tau_{\text{ruin}} \\ 0 & \text{otherwise} \end{cases}$$

$$\mathbb{P}(\text{Ruin}) = \frac{1}{K} \sum_{k=1}^K \text{Ruin Event}_k \times 100\%$$

---

## 3. Empirical Results (559 Trade Pool)

Simulation parameters: Starting Capital = ₹1,00,000.00, $K = 5,000$ iterations, Ruin Threshold = 10.0%:

| Metric | Empirical Simulation Value |
| :--- | :--- |
| **Sample Pool Size** | 559 trades (553 backtest + 6 live forward paper) |
| **Probability of Ruin (10% DD)** | **0.0000%** |
| **Probability of Drawdown $\ge 5\%$** | **0.0000%** |
| **Probability of Drawdown $\ge 15\%$** | **0.0000%** |
| **Median Max Drawdown** | **1.4479%** |
| **95th Percentile Max Drawdown** | **2.3169%** |
| **99th Percentile Max Drawdown** | **3.1748%** |
| **5th Percentile Ending Equity** | **₹2,38,820.11** |
| **Median Ending Equity** | **₹2,56,570.10** |
| **95th Percentile Ending Equity** | **₹2,75,393.06** |

**Quantitative Conclusion**: Under random sequence shuffling across 5,000 permutations, the probability of exceeding a 10% drawdown is 0.00%. The 95% worst-case drawdown remains well-contained at 2.32%, confirming the effectiveness of the 1.25% stop loss and daily loss cap (1.50%).

---

## 4. Verification Suite & Test Matrix

Milestone M15 was verified with automated unit and integration tests in `tests/unit/test_monte_carlo_m15.py`:

| Test Name | Verification Scope | Status |
| :--- | :--- | :--- |
| `test_monte_carlo_empty_trade_pool` | Safe handling of empty trade pool without division-by-zero errors. | **PASS** |
| `test_monte_carlo_deterministic_seed` | Byte-for-byte reproducibility with identical random seeds. | **PASS** |
| `test_monte_carlo_loss_pool_triggers_ruin` | Confirms that a synthetic loss pool correctly registers 100% ruin probability. | **PASS** |
| `test_monte_carlo_api_endpoint` | Verifies FastAPI route `GET /research/monte-carlo` schema and payload. | **PASS** |

### Unified Regression Summary
- **Total Tests:** 62
- **Passed:** 62
- **Failed:** 0
- **Regressions:** 0

