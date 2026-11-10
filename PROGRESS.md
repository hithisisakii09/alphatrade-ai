# AlphaTrade AI v2.0 — Engineering Progress & Gate Verification Log

**Current Status:** **PHASE 3 COMPLETE • Genuine SQLite Disk Persistence Verified Across OS Processes (14/14 Tests Passing) • Awaiting M0 Provider Approval**  
**Last Updated:** 10 November 2026  
**Active Execution Mode:** Strictly `PAPER`  
**Active Strategy Profile:** `STANDARD_RESEARCH_100K` (`INTRADAY_V2` Exit Mode)  

---

## 1. Verified Completed Work (Runtime-Tested & Proven)

- **Phase 3 Persistence Remediation Complete (M1 & M5):**
  - Replaced the in-memory `shared_store` mock in `app/core/database.py` with genuine parameterized SQLite disk persistence matching `SCHEMA_DDL`.
  - Implemented unit-of-work `Session` tracking with automatic dirty-attribute checking and `UPDATE` execution on commit/flush.
  - Implemented type-safe serialization/deserialization: exact `Decimal` numeric representation (preventing binary float precision errors), ISO-8601 timestamps, booleans (0/1), and integers.
  - Implemented cross-process restart verification test (`test_cross_process_restart_persistence`):
    - Process 1 submits orders, executes partial fills, updates trailing peaks/stops, commits to disk, and terminates.
    - Process 2 inspects `alphatrade_v2.db` directly on disk (orders=1, fills=2, positions=1, ledger_entries=5), reconstructs domain state, asserts trailing peak (`1295.0000`) and stop (`1285.2900`), confirms double-entry ledger reconciliation (`RECONCILED_OK`), and enforces single-entry-per-day restriction.
- **Complete Test Suite Passing (14/14 tests, exit code 0):**
  - `pytest -v`: **14 passed, 0 failed** in 1.51s.
  - `run_tests.py`: **14 passed, 0 failed** in 1.62s.
  - Test suites verified:
    - `unit/test_core_and_indicators.py`: Decimal float rejection, 2026 NSE calendar (Republic Day holiday & Diwali Muhurat special session), candle validation (duplicates, >90s staleness, >15% overnight corporate-action split quarantine), VWAP, Wilder ATR(14), TOD RVOL, and deterministic 1m->5m aggregation.
    - `unit/test_risk_and_costs.py`: Legacy 8/20/10 worked example (100 -> 92 stop -> 120 activation -> 130 peak -> 117 exit), `INTRADAY_V2` (1.25% stop / +2.50% activation / 0.75% drop), pessimistic intrabar stop priority, and gap-below-stop execution at adverse open price.
    - `integration/test_broker_and_recovery.py`: Order state machine, idempotency keys, partial fill completion with VWAP adjustment, circuit-freeze handling, in-memory recovery, and multi-process OS restart recovery from disk.
    - `backtest/test_backtester_and_lookahead.py`: Catching deliberate look-ahead leakage via `ChronologicalBarWindow`, reproducible multi-symbol benchmark simulation persisting to SQLite disk file.
    - `safety/test_live_guard_and_edge_cases.py`: Fail-closed `LIVE` mode prohibition, rejected orders not locking daily symbol slots, 1.5% daily loss limit kill switch, 15:00 IST forced EOD liquidation during kill switch, and all 11 read-only FastAPI endpoints.
- **Daily Runner Simulation:** `python -m app.services.daily_runner` executed end-to-end multi-symbol replay, persisting 4 orders, 4 fills, 2 positions, 9 ledger entries, 1 backtest run, and 2 backtest trades directly to `alphatrade_v2.db`.
- **FastAPI Web Service & HTML Console:** Live server active on `http://127.0.0.1:8000`, verified all 11 endpoints (`/health`, `/market/status`, `/strategy/status`, `/risk/status`, `/portfolio`, `/positions`, `/orders`, etc.) return HTTP 200 reading directly from `alphatrade_v2.db`.

---

## 2. Code-Inspection-Only Findings & Architectural Discrepancies

- **Exit Profile Realignment:**
  - Resolved historical mismatch: Legacy `8/20/10` (8% stop, 20% activation) is mathematically unviable for intraday equities bound by 5%–20% daily price bands.
  - Confirmed `INTRADAY_V2` (1.25% initial stop or ORB Low clamped 0.50%–1.50%, +2.50% activation, 0.75% drop) is the operational default. `LEGACY_SWING_8_20_10` is quarantined for backwards-compatibility tests.
- **Dashboard UI:**
  - `dashboard/index.html` serves a responsive 4-view console. Direct client-side dynamic polling against the 11 FastAPI endpoints can be wired up during M6 live enhancements.

---

## 3. Failed, Blocked & Incomplete Checks

- **Broker Network Adapters (Blocker for M0 Completion):**
  - **Status:** **RESPONSE PARSERS ONLY**
  - `app/data/providers.py` contains JSON response parsers for FYERS and Upstox, but `fetch_1m_candles()` network calls are not yet wired to live HTTP endpoints.
  - Awaiting written user approval on primary provider (FYERS v3 recommended for ₹0 cost and depth back to 2017).
- **Historical Dataset Depth (Blocker for M7 100+ OOS Trades):**
  - Requires live broker fetching or local CSV historical ingestion to execute a 100+ trade Out-of-Sample evaluation.

---

## 4. Current Milestone Status Matrix

| Milestone | Scope | Previous Claim | Verified Status | Evidence & Resolution |
| :---: | :--- | :---: | :---: | :--- |
| **M0** | Data Feasibility & Adapters | `PASSED` | **PARTIAL** | Feasibility report complete. Awaiting user written approval on provider selection before implementing live HTTP fetching. |
| **M1** | Foundation & Execution Guard | `PASSED` | **COMPLETE** | Core Decimal math, 2026 NSE calendar, fail-closed paper guard, and genuine SQLite disk persistence verified runtime (14/14 tests pass). |
| **M2** | Ingestion & Data Validation | `PASSED` | **COMPLETE** | Invariants, staleness, and split quarantine verified runtime (100% test pass). |
| **M3** | Intraday Risk Engine | `PASSED` | **COMPLETE** | `INTRADAY_V2`, cost-to-risk gate, and kill switch verified runtime (100% test pass). |
| **M4** | Backtester & Look-Ahead Guard | `PASSED` | **COMPLETE** | Next-bar open fills and look-ahead guard verified runtime (100% test pass). |
| **M5** | Paper Broker & Costs & Recovery | `PASSED` | **COMPLETE** | Cost calculations, order lifecycle, idempotency, and true multi-process restart recovery verified from SQLite disk (`test_cross_process_restart_persistence` passes). |
| **M6** | Daily Runner, API & Dashboard | `PASSED` | **PARTIAL** | All 11 FastAPI endpoints verified against disk data. Dashboard is read-only console; live intraday cron runner missing. |
| **M7** | Research Validation (100+ OOS) | `PASSED` | **PARTIAL** | Statistical classification verified. Awaiting historical dataset loader for 100+ OOS trades. |

---

## 5. Exact Next Action & Gate

1. **User Approval Gate (M0):** Awaiting user written approval on provider selection:
   - **Recommended Primary:** **FYERS API v3** (Free historical depth back to 2017, ₹0 API fees, clean 1m historical endpoint).
   - **Secondary Fallback:** **Upstox API v3** (Free historical depth back to 2022, 50 req/sec limit).
2. **Phase 4 Implementation:** Implement authenticated historical 1m candle fetching in `app/data/providers.py` for the approved provider with rate-limit retries and `.env` credentials.
