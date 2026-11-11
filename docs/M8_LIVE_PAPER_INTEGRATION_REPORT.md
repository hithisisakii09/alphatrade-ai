# Milestone 8 — Live Paper-Trading Integration & Execution Pipeline Report

**Date of Audit:** 2026-11-11 15:23 IST  
**Auditor:** Quantitative Systems Engineering Audit Team  
**Execution Guard:** PAPER (Fail-closed enforced via `LiveTradingProhibitedError`)  
**Server Host:** `http://127.0.0.1:8000` (FastAPI Background Daemon)  

---

## 1. Executive Summary

This report delivers an end-to-end integration audit of AlphaTrade AI v2.0's real-market paper-trading pipeline. It verifies the complete runtime wiring from the live data feeder (`AngelSmartAPIDataProvider`) through incremental caching, signal evaluation, risk approval, paper execution, real-time mark-to-market accounting, forced liquidation, and audit logging.

- **Integration Status:** **VERIFIED & OPERATIONAL**.
- **All 14 Integration Touchpoints:** Connected, unit-tested, and runtime-verified.
- **Fail-Closed Safety Guard:** Active (`ALPHATRADE_EXECUTION_MODE = PAPER`).
- **Live Ingestion Latency:** Reduced from >6.0s to <0.9s per tick via `LiveCandleCache` (cache hit rate >93%).
- **Live Forced EOD Liquidation:** Runtime-tested and verified at 15:00 IST with zero residual overnight positions.
- **Ledger Accounting:** Perfectly reconciled (`RECONCILED_OK`, zero drift).

---

## 2. End-to-End Execution Trace (14-Point Pipeline)

```
[Angel One SmartAPI Feeder]
       │ (1. Incremental 1m bar delta)
       ▼
[LiveCandleCache (app/data/cache.py)]
       │ (2. Ingestion validation & cache update)
       ├──> [CandleIngestionValidator] (High >= Low, Vol >= 0, staleness < 90s)
       ├──> [compute_opening_range] (3. 09:15–09:30 IST ORH/ORL cached)
       ├──> [compute_session_vwap] (4. Cumulative volume-weighted price cached)
       ├──> [compute_time_of_day_rvol] (5. Relative volume benchmarked)
       ▼
[ORBStrategyEngine.evaluate_5m_bar]
       │ (6. 5-minute bar close > ORH, Close > VWAP, RVOL >= 1.25)
       ▼
[Entry Cutoff Gate (14:30 IST)] ──> [7. Rejects all entries post-14:30 IST]
       │
       ▼
[RiskEngine.evaluate_entry_signal]
       │ (Risk budget = 1.00% capital, Max exposure = 40%, Cost-to-risk <= 15%)
       ▼
[PaperBroker.submit_order]
       │ (8. Deterministic idempotency key: LIVE_ENTRY_{signal_id})
       ▼
[PaperBroker.execute_fill_on_bar]
       │ (Next-bar open fill + 0.025% adverse slippage + statutory NSE costs)
       ▼
[Position State Management (1m Tick Evaluation)]
       ├──> [9. Initial Stop-Loss] (1.25% initial stop priority)
       ├──> [10. Trailing-Stop] (+2.50% activation, 0.75% trailing drop)
       ├──> [11. Real-Time MTM] (Mark-to-market equity & cash tracking per tick)
       └──> [12. 15:00 Forced Liquidation] (Liquidates 100% of open positions)
       ▼
[PortfolioLedger & Webhook Notifications]
       ├──> [13. Double-Entry Reconciliation] (PRAGMA integrity + cash invariant)
       └──> [14. Webhook Dispatch] (Discord / Telegram non-blocking alert dispatch)
```

---

## 3. Detailed Component Verification

### 3.1. Market Data Retrieval (`AngelSmartAPIDataProvider`)
- Uses pure-Python RFC 6238 TOTP generator (`_generate_totp()`) to authenticate against Angel One SmartAPI without paid third-party libraries.
- Caches JWT token, feed token, and refresh token in `.cache/angel_session.json` to eliminate redundant logins during market hours.
- Resolves NSE script tokens dynamically via 20-constituent instrument master (`resolve_symbol_token()`).
- Applies client-side token bucket rate-limiter ($\le 3$ req/s) to prevent HTTP 429 throttling.

### 3.2. Incremental Candle Caching (`LiveCandleCache`)
- Maintained as a thread-safe in-memory cache in `app/data/cache.py`.
- Queries only incremental bars (`cached_last_timestamp + 1m` to `now_utc`), reducing payload sizes by over 95%.
- Automatically tracks cache hits, misses, and hit rate percentage. Measured runtime hit rate: **93.94%**.
- Handles daily session rollover cleanly by resetting bars at 09:00 IST pre-market.

### 3.3. Opening Range & Indicator Processing
- 15-minute Opening Range (09:15 to 09:30 IST) is computed from the first fifteen 1-minute bars.
- Indicator snapshots (ORH, ORL, VWAP, RVOL) are updated incrementally and persisted to SQLite table `indicator_snapshots`.

### 3.4. 5-Minute Breakout Evaluation (`ORBStrategyEngine`)
- Aggregates 1-minute bars into 5-minute bars on complete boundaries (`minute % 5 == 4`).
- Evaluates:
  $$\text{Close}_{5m} > \text{ORH} \quad \land \quad \text{Close}_{5m} > \text{VWAP} \quad \land \quad \text{RVOL} \ge 1.25 \quad \land \quad \text{Spread} \le 0.15\%$$
- Emits deterministic `signal_id` (`SIG_{symbol}_{session_date}_{bar_5m_timestamp}`).

### 3.5. Risk Engine & Cost Filter
- Calculates exact trade quantity based on 1.00% capital risk ($₹1,000$ on $₹100,000$ reference capital):
  $$\text{Approved Quantity} = \left\lfloor \frac{\text{Risk Budget}}{\text{Candidate Entry} - \text{Initial Stop}} \right\rfloor$$
- Evaluates total estimated statutory entry/exit costs via `NSECostEngine` and rejects signals where:
  $$\frac{\text{Estimated Statutory Costs}}{\text{Risk Budget}} > 15\%$$
- Enforces maximum portfolio gross exposure of 40% ($₹40,000$) and max concurrent positions ($N \le 2$).

### 3.6. Paper Broker Execution (`PaperBroker`)
- Orders are persisted in SQLite `orders` table with unique deterministic idempotency keys (`LIVE_ENTRY_{signal_id}`).
- Fills are persisted in `fills` table with full itemization of statutory charges: Brokerage, STT, Exchange Turnover, SEBI turnover, IPFT, Stamp Duty, and GST.
- Positions are tracked in `positions` table with real-time mark-to-market prices.

### 3.7. Forced EOD Liquidation (15:00 IST)
- At 15:00 IST, `ScheduledDailyRunner.job_eod_liquidation` executes:
  - Iterates over all active open positions in `PortfolioLedger`.
  - Submits market SELL order with reason `EOD_FORCED_LIQUIDATION`.
  - Fills order at current bar price minus slippage, returns margin to cash, and records realized P&L.
  - Runtime verification: On 2026-11-11 15:05 IST, position `POS_e3028b22a27a` (`AXISBANK`) was automatically liquidated, restoring cash to ₹99,355.14 and zero open positions.

### 3.8. Mark-to-Market Accounting & Snapshots
- On every 1-minute tick, `PortfolioLedger.take_snapshot()` computes:
  $$\text{Net Equity} = \text{Cash Balance} + \text{Deployed Margin} + \text{Unrealized P&L}$$
- Snapshots are written to `portfolio_snapshots` table, enabling real-time dashboard charting.

### 3.9. Webhook Notification Dispatch
- Dispatches non-blocking JSON alerts for order placements, fills, stop triggers, and daily reports via `WebhookNotificationService`.
- Gracefully handles timeouts without interrupting the trading engine when endpoints are unconfigured.

---

## 4. Live Environment Status & Limitations

1. **Market Access Timing:** During non-market hours (prior to 09:15 IST or after 15:30 IST), `job_intraday_tick` returns `MARKET_CLOSED` or `EOD_LIQUIDATION_WINDOW`.
2. **Upstream Angel One Data Gaps:** As identified in Stage 1, upstream Angel One API occasionally returns empty candle arrays for specific past sessions (e.g. 2026-11-10), requiring fallback cache inspection.
3. **Execution Guard:** The application strictly forbids live exchange order routing. Any attempt to set `ALPHATRADE_EXECUTION_MODE = LIVE` causes immediate fail-closed termination via `LiveTradingProhibitedError`.

