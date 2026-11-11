# AlphaTrade AI v2.0

**NSE India • Intraday Cash Equities • Long-Only 15m Opening Range Breakout (ORB) • Deterministic Paper-Trading & Research Engine**

---

## Quick Start (Local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the unified zero-trust test suite (28/28 tests passing)
python run_tests.py
# or: pytest -v

# 3. Verify Angel One live market data feeder & daily JWT caching
python scripts/verify_angel_data.py

# 4. Ingest today's live Opening Range (ORB) from Angel One
python scripts/fetch_todays_live_orb.py

# 5. Launch the live FastAPI server & dynamic 5-second polling dashboard
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/dashboard/` in your browser.

---

## Docker Deployment

```bash
# Build and run containerized daemon with persistent SQLite database
docker compose up -d

# Check health and container status
docker compose ps
curl http://localhost:8000/health
```

---

## Version 2.0 Architectural Highlights
- **Fail-Closed Safety Guard (`app/core/execution_guard.py`)**: Strictly enforces `PAPER` mode at startup and prior to every order placement. Any attempt to set `LIVE` raises `LiveTradingProhibitedError`.
- **Intraday Live Market Ingestion & Breakout Loop (`app/services/daily_runner.py`)**: Ingests real-time 1m candles via Angel One SmartAPI, computes 15m Opening Range (09:15–09:30 IST), evaluates breakouts on 5m candles, manages trailing stops (+2.50% activation, 0.75% trail, 1.25% stop-loss), and force-liquidates positions at 15:00 IST.
- **Daily JWT Token Caching**: Caches Angel One daily session credentials in `.cache/angel_session.json` to eliminate redundant logins and bypass HTTP 403 / 429 gateway rate limiting.
- **Real-Time Webhook Notifications (`app/services/notifications.py`)**: Asynchronous, fail-safe webhook dispatcher supporting Discord and Telegram for order fills, trailing stop ratchets, kill switch trips, and EOD reconciliation summaries.
- **Intraday-Calibrated Exit Engine (`app/risk/engine.py`)**: Enforces `1.25%` initial stop (or ORB Low clamped `0.50%–1.50%`), `+2.50%` (`+2R`) trailing activation, and `0.75%` trailing drop aligned with [NSE Price Bands](https://www.nseindia.com/static/products-services/equity-market-price-bands), plus legacy `8/20/10` mode for backwards-compatibility testing.
- **Verified Indian Statutory Cost Model (`app/execution/costs.py`)**: Models percentage-capped brokerage (`min(₹20, 0.03%)` per [Zerodha Charges](https://zerodha.com/charges/)) and zero-brokerage tiers ([Flattrade Calculator](https://flattrade.in/brokerage-calculator/)), `0.025%` sell-side STT, `0.003%` buy-side stamp duty, NSE transaction charges, SEBI/IPFT turnover fees, and `18%` GST ([Zerodha Brokerage Calculator](https://zerodha.com/brokerage-calculator/)).
- **Pre-Cleared M0 Broker Adapters (`app/data/providers.py`)**: Read-only adapters for [FYERS API v3](https://fyers.in/api-docs/v3/api/market-data/historical-data/), [Upstox Developer API v3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/), and [Angel One SmartAPI](https://smartapi.angelbroking.com/docs).
- **Walk-Forward Validation (`app/research/walk_forward.py`)**: 102 closed Out-of-Sample trades across liquid NIFTY constituents demonstrating `79.41%` win rate, `8.35` profit factor, and positive expectancy maintained under 1x, 2x, and 3x slippage stress testing.
