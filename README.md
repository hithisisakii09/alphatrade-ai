# AlphaTrade AI v2.0

**NSE India • Intraday Cash Equities • Long-Only 15m Opening Range Breakout (ORB) • Deterministic Paper-Trading & Research Engine**

## Quick Start

```bash
# 1. Run the complete unit, integration, backtest, and safety test suite
pytest

# 2. Run the deterministic end-to-end benchmark simulation & populate SQLite DB
python3 -m app.services.daily_runner

# 3. Launch the read-only FastAPI server & 4-view HTML dashboard
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Version 2.0 Architectural Highlights
- **Intraday-Calibrated Exit Engine (`app/risk/engine.py`)**: Enforces `1.25%` initial stop (or ORB Low clamped `0.50%–1.50%`), `+2.50%` (`+2R`) trailing activation, and `0.75%` trailing drop aligned with [NSE Price Bands](https://www.nseindia.com/static/products-services/equity-market-price-bands), plus legacy `8/20/10` mode for backwards-compatibility testing.
- **Dual Capital Profiles (`config/strategy/orb_v2.json`)**: `STANDARD_RESEARCH_100K` (`₹1,00,000` virtual research ledger) and `MICRO_CAPITAL_10K` (`₹10,000` stress-test profile).
- **Verified Indian Statutory Cost Model (`app/execution/costs.py`)**: Models percentage-capped brokerage (`min(₹20, 0.03%)` per [Zerodha Charges](https://zerodha.com/charges/)) and zero-brokerage tiers ([Flattrade Calculator](https://flattrade.in/brokerage-calculator/)), `0.025%` sell-side STT, `0.003%` buy-side stamp duty, NSE transaction charges, SEBI/IPFT turnover fees, and `18%` GST ([Zerodha Brokerage Calculator](https://zerodha.com/brokerage-calculator/)).
- **Pre-Cleared M0 Broker Adapters (`app/data/providers.py`)**: Read-only adapters for [FYERS API v3](https://fyers.in/api-docs/v3/api/market-data/historical-data/), [Upstox Developer API v3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/), and [Angel One SmartAPI](https://smartapi.angelbroking.com/docs).
