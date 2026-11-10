# M0 Data-Source Feasibility & Pre-Cleared Broker Matrix

**Project:** AlphaTrade AI v2.0 (NSE India Intraday Cash Equities)
**Date:** 10 November 2026
**M0 Status:** **PASSED (Pre-Cleared Zero-Cost Retail Broker APIs)**

## 1. Executive Summary
Milestone 0 requires evaluating permitted market-data sources for 1-minute and 5-minute NSE cash-equity intraday OHLCV candles without violating the ₹0 subscription budget or scraping NSE websites. Version 2.0 pre-clears three zero-cost retail broker APIs for single-user local research and paper trading.

## 2. Candidate Source Comparison Matrix

| Provider | Intraday & Historical Depth | Pricing | Automated Research / Paper Rights | v2.0 Status |
| :--- | :--- | :--- | :--- | :--- |
| **[FYERS API v3](https://fyers.in/api-docs/v3/api/market-data/historical-data/)** | 1m/5m intraday candles back to July 2017 (5+ years); 29 years EOD; live WebSocket up to 5,000 symbols ([FYERS API Docs](https://fyers.in/api-docs/introduction/)) | ₹0 for FYERS account holders ([FYERS Datafeed FAQ](https://support.fyers.in/portal/en/kb/articles/do-i-need-to-pay-for-datafeeds)) | Permitted for personal account research and paper trading; redistribution prohibited ([Work with Historical Data](https://fyers.in/api-docs/v3/getting-started/guides/work-with-historical-market-data/)) | **PRE-APPROVED (Primary)** |
| **[Upstox Developer API v3](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/)** | 1m intraday candles from 2022; daily candles back to 2000; live WebSocket feed ([Broker API Comparison](https://stocksdeveloper.in/documentation/supported-brokers/api-comparison/market-data/)) | ₹0 for Upstox account holders ([Upstox Trading API](https://upstox.com/trading-api/)) | Permitted for personal algo development and backtesting; redistribution prohibited | **PRE-APPROVED (Secondary)** |
| **[Angel One SmartAPI](https://smartapi.angelbroking.com/docs)** | Free historical intraday OHLCV and live WebSocket streaming across NSE equities and indices ([SmartAPI FAQ](https://smartapi.angelbroking.com/faq)) | ₹0 for Angel One clients | Permitted for retail client backtesting and paper execution | **PRE-APPROVED (Tertiary)** |
| **[Zerodha Kite Connect](https://zerodha.com/products/api/)** | Up to 10 years of 1m/5m historical data bundled with base Connect plan ([Kite Forum Announcement](https://kite.trade/forum/discussion/14806/historical-data-is-now-free-with-base-kite-connect-subscription)) | ₹500 / month | Permitted for personal use, but requires ₹500/month subscription | **DEFERRED (Paid)** |
| **[DhanHQ Data APIs](https://dhanhq.co/trading-apis/individuals-geeks)** | 5 years of 1m intraday candles and real-time feed ([Dhan Data Subscription](https://dhan.co/support/platforms/dhanhq-api/how-does-the-dhanhq-data-api-subscription-work/)) | ₹499 / month + taxes | Trading APIs are free, but Data APIs require monthly fee or F&O volume waiver | **DEFERRED (Paid)** |

## 3. Data-Quality & Corporate-Action Safeguards
- **1-Minute Base Resolution:** Ingest 1-minute OHLCV candles in UTC and aggregate 5-minute signal bars and 15-minute Opening Range bars deterministically.
- **Corporate-Action & Split Quarantine:** Because retail broker historical feeds can occasionally display unadjusted split or bonus gaps ([Kite Connect Historical Forum](https://kite.trade/forum/discussions/tagged/historical-api/p1)), the M2 validator quarantines any symbol exhibiting an overnight close-to-open jump exceeding `15%` until verified.
- **Universe Restriction:** Restrict the initial trading universe to liquid NIFTY 100 / NIFTY 200 constituents (`>= ₹25 Crore` 20-day median daily turnover) to minimize survivorship distortion.
