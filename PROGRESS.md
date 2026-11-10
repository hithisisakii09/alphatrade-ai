# AlphaTrade AI v2.0 — Engineering Progress & Gate Verification Log

**Current Status:** **RUNTIME VERIFIED (13/13 Tests Passing) • Awaiting M0 Provider Approval & SQLite Disk Persistence Fix**  
**Last Updated:** 10 November 2026  
**Active Execution Mode:** Strictly `PAPER`  
**Active Strategy Profile:** `STANDARD_RESEARCH_100K` (`INTRADAY_V2` Exit Mode)  

---

## 1. Verified Completed Work (Runtime-Tested & Proven)

- **Python Runtime Environment Resolved:** Installed Python 3.12.10 via `winget` in user scope and created `.venv` with `fastapi`, `pydantic`, `sqlalchemy`, `pytest`, `httpx`, and `tzdata`.
- **Complete Test Suite Passing (13/13 tests, exit code 0):**
  - `run_tests.py`: **13 passed, 0 failed** in standard execution.
  - `pytest -v`: **13 passed, 0 failed** in 1.06s.
  - Test suites verified:
    - `unit/test_core_and_indicators.py`: Decimal float rejection, 2026 NSE calendar (Republic Day holiday & Diwali Muhurat special session), candle validation (duplicates, >90s staleness, >15% overnight corporate-action split quarantine), VWAP, Wilder ATR(14), TOD RVOL, and deterministic 1m->5m aggregation.
    - `unit/test_risk_and_costs.py`: Legacy 8/20/10 worked example (100 -> 92 stop -> 120 activation -> 130 peak -> 117 exit), `INTRADAY_V2` (1.25% stop / +2.50% activation / 0.75% drop), pessimistic intrabar stop priority, and gap-below-stop execution at adverse open price.
    - `integration/test_broker_and_recovery.py`: Order state machine, idempotency keys, partial fill completion with VWAP adjustment, circuit-freeze handling, and position reload.
    - `backtest/test_backtester_and_lookahead.py`: Catching deliberate look-ahead leakage via `ChronologicalBarWindow`, reproducible multi-symbol benchmark simulation.
    - `safety/test_live_guard_and_edge_cases.py`: Fail-closed `LIVE` mode prohibition, rejected orders not locking daily symbol slots, 1.5% daily loss limit kill switch, 15:00 IST forced EOD liquidation during kill switch, and all 11 read-only FastAPI endpoints.
- **Daily Runner Simulation:** `python -m app.services.daily_runner` successfully executes end-to-end multi-symbol replay, generating 2 benchmark trades, double-entry ledger reconciliation, and correct metric summary.

---

## 2. Code-Inspection-Only Findings & Architectural Discrepancies

- **Exit Profile Realignment:**
  - Resolved historical mismatch: Legacy `8/20/10` (8% stop, 20% activation) is mathematically unviable for intraday equities bound by 5%–20% daily price bands.
  - Confirmed `INTRADAY_V2` (1.25% initial stop or ORB Low clamped 0.50%–1.50%, +2.50% activation, 0.75% drop) is the operational default. `LEGACY_SWING_8_20_10` is quarantined for backwards-compatibility tests.
- **Static Dashboard UI:**
  - `dashboard/index.html` displays static benchmark data; asynchronous client-side `fetch()` polling against the 11 FastAPI endpoints is not implemented.

---

## 3. Failed, Blocked & Incomplete Checks

- **Persistence Layer Volatility (Blocker for M5 Completion):**
  - **Status:** **SIMULATION ADAPTER ONLY**
  - In `app/core/database.py`, `init_db()` executes `SCHEMA_DDL` to create all 17 tables on SQLite disk (`alphatrade_v2.db`), but the `Session` class retains all state in Python memory via `shared_store = {}`.
  - No SQL `INSERT` or `UPDATE` statements are issued to the database file.
  - **Runtime Test Evidence:** Running `daily_runner` to completion resulted in `0 rows` in the `positions`, `orders`, and `ledger_entries` SQLite disk tables. State does not survive process termination.
- **Broker Network Adapters (Blocker for M0 Completion):**
  - **Status:** **RESPONSE PARSERS ONLY**
  - `app/data/providers.py` contains JSON response parsers for FYERS and Upstox, but `fetch_1m_candles()` is unimplemented.
  - No live HTTP calls, OAuth token refresh, or rate-limit retry mechanisms exist.
- **Historical Dataset Depth (Blocker for M7 100+ OOS Trades):**
  - Requires live broker fetching or local CSV historical ingestion to execute a 100+ trade Out-of-Sample evaluation.

---

## 4. Current Milestone Status Matrix

| Milestone | Scope | Previous Claim | Verified Status | Blocking Issue |
| :---: | :--- | :---: | :---: | :--- |
| **M0** | Data Feasibility & Adapters | `PASSED` | **PARTIAL** | Feasibility report complete. Awaiting user written approval on provider selection before implementing live HTTP fetching. |
| **M1** | Foundation & Execution Guard | `PASSED` | **PARTIAL** | Core math & calendar verified runtime. SQLite disk table writing not implemented. |
| **M2** | Ingestion & Data Validation | `PASSED` | **COMPLETE** | Invariants, staleness, and split quarantine verified runtime (100% test pass). |
| **M3** | Intraday Risk Engine | `PASSED` | **COMPLETE** | `INTRADAY_V2`, cost-to-risk gate, and kill switch verified runtime (100% test pass). |
| **M4** | Backtester & Look-Ahead Guard | `PASSED` | **COMPLETE** | Next-bar open fills and look-ahead guard verified runtime (100% test pass). |
| **M5** | Paper Broker & Costs & Recovery | `PASSED` | **PARTIAL** | Cost calculations and order lifecycle verified. True process restart recovery blocked by in-memory `shared_store`. |
| **M6** | Daily Runner, API & Dashboard | `PASSED` | **PARTIAL** | 11 FastAPI endpoints pass tests. Dashboard is static; live intraday cron runner missing. |
| **M7** | Research Validation (100+ OOS) | `PASSED` | **PARTIAL** | Statistical classification verified. Awaiting historical dataset loader for 100+ OOS trades. |

---

## 5. Exact Next Action & Gate

1. **User Approval Gate (M0):** Present provider comparison (FYERS v3 vs. Upstox v3 vs. Angel One) and wait for written provider approval.
2. **Persistence Remediation (M1/M5):** Implement genuine SQLite parameter-based persistence in `app/core/database.py` replacing `shared_store` so that orders, fills, positions, trailing peaks, and ledger entries persist across OS process restarts.
3. **Data Fetching Integration (M0):** Implement authenticated read-only historical 1m candle fetching for the approved broker API.
