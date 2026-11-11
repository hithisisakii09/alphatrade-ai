# Milestone 8 — Prioritized Engineering Gap Analysis & Remediation Plan

**Date of Audit:** 2026-11-11 15:25 IST  
**Auditor:** Quantitative Systems Engineering Audit Team  
**Status:** AUDIT & PROPOSAL STAGE (Awaiting Explicit User Approval Before Implementation)  

---

## 1. Prioritized Defect & Gap Matrix

| ID | Severity | Category | Description | Status |
| :---: | :---: | :---: | :--- | :---: |
| **GAP-01** | **HIGH** | Research Validity | Synthetic Walk-Forward Data Conflated with Historical Alpha Claims | Identified & Audited |
| **GAP-02** | **HIGH** | Data Integrity | Insufficient Genuine Historical Dataset for Statistical Significance ($N < 100$) | Identified & Audited |
| **GAP-03** | **MEDIUM** | Simulation Accuracy | Static `prev_closes[sym]` Distorts Multi-Day Historical Backtests | Identified & Audited |
| **GAP-04** | **MEDIUM** | Data Coverage | Missing Session (2026-11-10) and Asymmetric Constituent Coverage | Identified & Audited |
| **GAP-05** | **LOW** | CI/CD Infrastructure | GitHub Actions Workflow Disabled / Not Triggering on Remote | Identified & Audited |

---

## 2. Detailed Finding Profiles

---

### GAP-01: Synthetic Walk-Forward Data Conflated with Historical Alpha Claims

- **Classification:** **HIGH (Research Validity & Methodological Integrity)**
- **Issue:**
  The Milestone 7 walk-forward results (102 OOS trades, 79.41% Win Rate, Profit Factor 8.35, Net Expectancy +0.9626 R) were generated using a synthetic Monte Carlo data generator (`generate_multi_day_dataset()`), which injects an artificial 78% breakout probability into simulated price bars. These metrics were documented in `PROGRESS.md` alongside real historical ingestion, creating the false impression that the strategy had demonstrated positive empirical alpha on genuine NSE market data.
- **Evidence:**
  `app/research/walk_forward.py` lines 47–88 and 106 (`all_bars_by_symbol, prev_closes = generate_multi_day_dataset(...)`). Persisted backtest trade dates in SQLite show simulated August–October 2026 dates (e.g. `2026-08-03`).
- **Why It Matters:**
  Conflating synthetic stress tests with genuine historical backtest results undermines quantitative research credibility and violates institutional zero-trust principles. When the backtester is run on the real 9,500 candles, only 1 trade occurred (`HDFCBANK`, -1.1233 R loss), classified as `INCONCLUSIVE_SAMPLE_SIZE_BELOW_100`.
- **Core Solution:**
  1. Formally decouple the synthetic benchmark generator into a clearly designated test module: `tests/stress/test_engine_scalability_stress.py`.
  2. Implement `run_real_historical_walk_forward()` in `app/research/walk_forward.py` that loads exclusively from `market_candles` in SQLite.
  3. Ensure that any run with fewer than 100 historical trades is strictly classified as `INCONCLUSIVE_SAMPLE_SIZE_BELOW_100` and labeled `[HISTORICAL_SAMPLE_INSUFFICIENT]`.
- **Exact Files Affected:**
  - `app/research/walk_forward.py`
  - `scripts/run_walk_forward_study.py`
  - `PROGRESS.md`
- **Proposed Implementation Scope:**
  Update `run_100_trade_out_of_sample_study` to accept a `data_source` parameter (`"REAL_SQLITE"` vs `"SYNTHETIC_STRESS_BENCHMARK"`). Default to `"REAL_SQLITE"`. Document the synthetic nature of the 102-trade run in research notes.
- **Regression Tests Required:**
  - `tests/backtest/test_walk_forward_oos.py`: Verify that historical runs draw strictly from SQLite and flag small sample sizes.

---

### GAP-02: Insufficient Genuine Historical Dataset for Statistical Significance ($N < 100$)

- **Classification:** **HIGH (Data Integrity & Statistical Viability)**
- **Issue:**
  The genuine historical candle database (`alphatrade_v2.db`) currently holds 9,500 1-minute bars spanning only TWO distinct trading sessions (`2026-11-09` and `2026-11-11`). Two sessions across 20 symbols are statistically insufficient to generate a minimum sample size of 100 trades for a selective intraday ORB strategy.
- **Evidence:**
  SQL query `SELECT DISTINCT date(...) FROM market_candles` yields exactly 2 dates. Total trades triggered across the entire universe on real data is 1.
- **Why It Matters:**
  A quantitative edge cannot be verified or disproven from 1 or 2 trades. Establishing statistical significance ($p < 0.05$, $N \ge 100$) for an intraday strategy requires 40 to 60 distinct sessions (~300,000+ 1-minute candles across 20 symbols).
- **Core Solution:**
  Enhance `scripts/download_historical_candles.py` with multi-month pagination, chunking requests into 5-day intervals with rate-limit backoff, to ingest a continuous 60-day historical window from Angel One SmartAPI as historical data becomes accessible.
- **Exact Files Affected:**
  - `scripts/download_historical_candles.py`
- **Proposed Implementation Scope:**
  Add a `--start-date` and `--end-date` CLI parameter with automatic 5-day chunking, retry mechanisms, and rate-limiting compliant with Angel One's 3 req/s threshold.
- **Regression Tests Required:**
  - Unit test verifying multi-chunk date partitioning and idempotent re-fetching.

---

### GAP-03: Static `prev_closes[sym]` Distorts Multi-Day Historical Backtests

- **Classification:** **MEDIUM (Simulation Accuracy)**
- **Issue:**
  In `scripts/run_live_backtest.py` line 81:
  ```python
  prev_closes[sym] = candle_bars[0].open
  ```
  In a multi-day historical backtest spanning several sessions, the previous day's close remains fixed to the very first bar's open of day 1. It is never updated on session date rollover (`s_date != current_session_date`).
- **Evidence:**
  `run_live_backtest.py` passes a single static dictionary `prev_closes_by_symbol` to `EventDrivenBacktester.run_backtest`.
- **Why It Matters:**
  The ORB strategy uses `prev_close` to compute daily price circuit bands (lower circuit, upper circuit) and gap filters. Using Day 1's open as Day 10's previous close causes circuit bands to drift away from reality on subsequent days.
- **Core Solution:**
  Update `EventDrivenBacktester` to track the last bar's close of session $D-1$ and dynamically update `prev_close` for session $D$ when `s_date != current_session_date`.
- **Exact Files Affected:**
  - `app/backtest/engine.py`
  - `scripts/run_live_backtest.py`
- **Proposed Implementation Scope:**
  In `app/backtest/engine.py`, maintain `session_prev_close_by_symbol[sym]` and update it from the final bar of the previous session before processing the first bar of a new session.
- **Regression Tests Required:**
  - Backtester unit test simulating a 2-day sequence verifying that Day 2 circuit bands use Day 1's close.

---

### GAP-04: Missing Session (2026-11-10) and Asymmetric Constituent Coverage

- **Classification:** **MEDIUM (Data Coverage & Operational Reliability)**
- **Issue:**
  Angel One SmartAPI returned 0 bars for `2026-11-10` during historical download. Additionally, BHARTIARTL and KOTAKBANK have no data for 2026-11-09, while 5 symbols lack data for 2026-11-11, creating asymmetric universe coverage.
- **Evidence:**
  `download_historical_candles.py` log for `2026-11-10`: `0 bars returned`. SQLite inventory query confirms varying bar counts across symbols (197 to 571).
- **Why It Matters:**
  Incomplete data across symbols prevents fair cross-sectional universe screening and portfolio ranking during multi-symbol backtesting.
- **Core Solution:**
  Implement a gap-audit and backfill routine in `download_historical_candles.py` that identifies missing sessions per symbol and initiates targeted re-fetch attempts with diagnostic logging.
- **Exact Files Affected:**
  - `scripts/download_historical_candles.py`
- **Proposed Implementation Scope:**
  Add `--audit-gaps` flag that inspects SQLite `market_candles` and highlights missing session dates across universe constituents.
- **Regression Tests Required:**
  - Test verifying gap detection logic.

---

### GAP-05: GitHub Actions Workflow Inactive on Remote

- **Classification:** **LOW (CI/CD Operations)**
- **Issue:**
  The GitHub repository `https://github.com/hithisisakii09/alphatrade-ai.git` currently shows 0 workflow runs via GitHub's public API. The workflow file `.github/workflows/ci.yml` is pushed, but GitHub Actions is either disabled in repository settings or awaiting repository owner enablement.
- **Evidence:**
  `https://api.github.com/repos/hithisisakii09/alphatrade-ai/actions/runs` returns `total_count: 0`.
- **Why It Matters:**
  Automated testing and Docker build verification on push/PR cannot run until GitHub Actions is enabled in repository settings.
- **Core Solution:**
  Repository owner should navigate to `Settings -> Actions -> General -> Workflow permissions` on GitHub and ensure "Read and write permissions" and "Allow GitHub Actions" are selected.
- **Exact Files Affected:**
  - None (Repository settings configuration).
- **Proposed Implementation Scope:**
  Operational guidance only. No code modification required.
- **Regression Tests Required:**
  - N/A.

---

## 3. Recommended Implementation Roadmap

1. **Step 1 (Immediate - User Approval Required):**
   - Correctly reclassify Milestone 7 in `PROGRESS.md` to distinguish synthetic stress test results from genuine historical backtest results.
   - Refactor `scripts/run_live_backtest.py` and `app/backtest/engine.py` to support dynamic session rollover for `prev_close` (GAP-03).
2. **Step 2 (Next Milestone - M9):**
   - Implement multi-month historical ingestion with Angel One pagination (GAP-02 & GAP-04) to expand the real candle database to $\ge 40$ sessions.
   - Execute an authentic, empirical 100+ trade walk-forward study on genuine historical data.

