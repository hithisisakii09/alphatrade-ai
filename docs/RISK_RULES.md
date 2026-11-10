# AlphaTrade AI v2.0 — Deterministic Risk Rules

## 1. Intraday Exit Rules (`1.25% / +2.50% / 0.75%` & Dynamic ORB/ATR)
Individual NSE stocks operate under daily price bands of `2%`, `5%`, `10%`, or `20%` ([NSE Price Bands](https://www.nseindia.com/static/products-services/equity-market-price-bands)) and a `10%` dynamic operating range for F&O stocks ([Zerodha Circuit Limits](https://support.zerodha.com/category/trading-and-markets/trading-faqs/general/articles/what-does-circuit-limits-i-e-price-bands-mean)). Version 2.0 enforces realistic intraday parameters:
- **Initial Stop-Loss (`1R`)**: `Entry_Fill * (1 - 0.0125)` (or Opening Range Low, clamped between `0.50%` and `1.50%` below fill).
- **Trailing Activation (`+2R`)**: Activates when price reaches `Entry_Fill * (1 + 0.0250)` (`+2.50%`).
- **Trailing Peak (`H_peak`)**: Highest price recorded at or after activation, persisted in the `positions` table.
- **Trailing Stop Exit**: Triggers when price `<= H_peak * (1 - 0.0075)` (`0.75%` drop from peak).
- **Legacy `8/20/10` Profile**: Available only via explicit configuration (`LEGACY_SWING_8_20_10`) for unit testing or multi-day swing comparisons.

## 2. Capital, Sizing & Cost-to-Risk Gate
- **Standard Research Ledger**: `₹1,00,000` (`0.5%` risk per trade = `₹500`; `1.5%` max daily loss = `₹1,500`).
- **Micro-Capital Stress Ledger**: `₹10,000` (`0.5%` risk per trade = `₹50`; `1.5%` max daily loss = `₹150`).
- **Whole-Share Position Sizing**: `Qty = min(floor(Risk_Budget / Stop_Per_Share), floor(Max_Position_Capital / Entry_Price), floor(Available_Cash / Entry_Price))`.
- **Cost-to-Risk Gate**: Rejects any trade where estimated round-trip brokerage and statutory taxes ([Zerodha Charges](https://zerodha.com/charges/), [Flattrade Calculator](https://flattrade.in/brokerage-calculator/)) exceed `15%` of `1R` (`Qty * Stop_Per_Share`).
