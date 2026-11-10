# AlphaTrade AI v2.0 — Engineering Progress & Gate Verification Log

**Current Status:** **MILESTONES M0 THROUGH M5 COMPLETE • Genuine SQLite Disk Persistence & Angel One SmartAPI Feeder Verified (19/19 Tests Passing)**  
**Last Updated:** 10 November 2026  
**Active Execution Mode:** Strictly `PAPER`  
**Active Strategy Profile:** `STANDARD_RESEARCH_100K` (`INTRADAY_V2` Exit Mode)  
**Active Data Provider:** `ANGEL_SMARTAPI` (NSE Cash Equities)  

---

## 1. Verified Completed Work (Runtime-Tested & Proven)

- **Milestone 0 Data Ingestion Pipeline Complete (Angel One SmartAPI):**
  - Implemented `AngelSmartAPIDataProvider` in `app/data/providers.py` with pure-Python RFC 6238 TOTP generation (`generate_totp`) from `ANGEL_TOTP_SECRET` with zero external binary dependencies.
  - Wired automated session authentication via Angel One's `loginByPassword` API (supporting both `ANGEL_PASSWORD` from `.env` and manual `ANGEL_JWT_TOKEN` injection).
  - Enforced client-side rate limiting at `<= 3 req/sec` with exponential backoff on HTTP 429.
  - Implemented liquid NIFTY 100 symbol-to-token resolver (`RELIANCE`: 2885, `TCS`: 11536, `INFY`: 1594, `HDFCBANK`: 1333, `ICICIBANK`: 4963, `SBIN`: 3045, etc.).
  - Implemented `parse_angel_response` converting Angel One raw candle arrays into validated `CandleBar` models with exact `Decimal` arithmetic.
  - Created automated standalone verification script `scripts/verify_angel_data.py`.
- **Phase 3 Persistence Remediation Complete (M1 & M5):**
  - Replaced the in-memory `shared_store` mock in `app/core/database.py` with genuine parameterized SQLite disk persistence matching `SCHEMA_DDL`.
  - Implemented unit-of-work `Session` tracking with automatic dirty-attribute checking and `UPDATE` execution on commit/flush.
  - Implemented type-safe serialization/deserialization: exact `Decimal` numeric representation (preventing binary float precision errors), ISO-8601 timestamps, booleans (0/1), and integers.
  - Cross-process restart verification test (`test_cross_process_restart_persistence`) verified across separate OS subprocesses.
- **Complete Test Suite Passing (19/19 tests, exit code 0):**
  - `pytest -v`: **19 passed, 0 failed** in 1.56s.
  - `run_tests.py`: **19 passed, 0 failed** in 1.65s.
  - Test suites verified:
    - `unit/test_angel_provider.py` (5 tests): RFC 6238 TOTP vectors, symbol token resolution, getCandleData response parsing, auth error handling, and factory instantiation.
    - `unit/test_core_and_indicators.py` (4 tests): Decimal float rejection, 2026 NSE calendar (Republic Day holiday & Diwali Muhurat special session), candle validation (duplicates, >90s staleness, >15% overnight corporate-action split quarantine), VWAP, Wilder ATR(14), TOD RVOL, and deterministic 1m->5m aggregation.
    - `unit/test_risk_and_costs.py` (3 tests): Legacy 8/20/10 worked example, `INTRADAY_V2` (1.25% stop / +2.50% activation / 0.75% drop), pessimistic intrabar stop priority, and gap-below-stop execution at adverse open price.
    - `integration/test_broker_and_recovery.py` (2 tests): Order state machine, idempotency keys, partial fill completion with VWAP adjustment, circuit-freeze handling, in-memory recovery, and multi-process OS restart recovery from disk.
    - `backtest/test_backtester_and_lookahead.py` (2 tests): Catching deliberate look-ahead leakage via `ChronologicalBarWindow`, reproducible multi-symbol benchmark simulation persisting to SQLite disk file.
    - `safety/test_live_guard_and_edge_cases.py` (3 tests): Fail-closed `LIVE` mode prohibition, rejected orders not locking daily symbol slots, 1.5% daily loss limit kill switch, 15:00 IST forced EOD liquidation during kill switch, and all 11 read-only FastAPI endpoints.
- **Daily Runner Simulation:** `python -m app.services.daily_runner` executed end-to-end multi-symbol replay, persisting 4 orders, 4 fills, 2 positions, 9 ledger entries, 1 backtest run, and 2 backtest trades directly to `alphatrade_v2.db`.
- **FastAPI Web Service & HTML Console:** Live server active on `http://127.0.0.1:8000`, verified all 11 endpoints return HTTP 200 reading directly from `alphatrade_v2.db`.

---

## 2. Code-Inspection-Only Findings & Architectural Discrepancies

- **Exit Profile Realignment:**
  - Confirmed `INTRADAY_V2` (1.25% initial stop or ORB Low clamped 0.50%–1.50%, +2.50% activation, 0.75% drop) is the operational default. `LEGACY_SWING_8_20_10` is quarantined for backwards-compatibility tests.
- **Dashboard UI:**
  - `dashboard/index.html` serves a responsive 4-view console. Dynamic client-side periodic polling against the 11 FastAPI endpoints is ready for M6 live integration.

---

## 3. Failed, Blocked & Incomplete Checks

- **Historical Dataset Depth (Blocker for M7 100+ OOS Trades):**
  - Requires running multi-day ingestion across NIFTY symbols via Angel One SmartAPI to compile a 100+ trade Out-of-Sample historical evaluation dataset.

---

## 4. Current Milestone Status Matrix

| Milestone | Scope | Previous Claim | Verified Status | Evidence & Resolution |
| :---: | :--- | :---: | :---: | :--- |
| **M0** | Data Feasibility & Adapters | `PASSED` | **COMPLETE** | Angel One SmartAPI fully implemented as primary feeder with RFC 6238 TOTP, rate-limiting guard, and token resolver. Verified via `verify_angel_data.py` and 5 unit tests. |
| **M1** | Foundation & Execution Guard | `PASSED` | **COMPLETE** | Core Decimal math, 2026 NSE calendar, fail-closed paper guard, and genuine SQLite disk persistence verified runtime (19/19 tests pass). |
| **M2** | Ingestion & Data Validation | `PASSED` | **COMPLETE** | Invariants, staleness, and split quarantine verified runtime (100% test pass). |
| **M3** | Intraday Risk Engine | `PASSED` | **COMPLETE** | `INTRADAY_V2`, cost-to-risk gate, and kill switch verified runtime (100% test pass). |
| **M4** | Backtester & Look-Ahead Guard | `PASSED` | **COMPLETE** | Next-bar open fills and look-ahead guard verified runtime (100% test pass). |
| **M5** | Paper Broker & Costs & Recovery | `PASSED` | **COMPLETE** | Cost calculations, order lifecycle, idempotency, and true multi-process restart recovery verified from SQLite disk (`test_cross_process_restart_persistence` passes). |
| **M6** | Daily Runner, API & Dashboard | `PASSED` | **PARTIAL** | All 11 FastAPI endpoints verified against disk data. Live cron scheduler pending. |
| **M7** | Research Validation (100+ OOS) | `PASSED` | **PARTIAL** | Statistical classification verified. Requires historical 1m data loader for 100+ OOS trades. |

---

## 5. Exact Next Action & Gate

1. **Ingest Historical Dataset via Angel One SmartAPI (M7):** Run multi-day historical data pull for NIFTY 100 basket to assemble 100+ trade Out-of-Sample backtest dataset.
2. **Automate Intraday Runner (M6):** Implement APScheduler cron for automated session execution (09:15–15:30 IST) in `app/services/daily_runner.py`.
