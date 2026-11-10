# AlphaTrade AI v2.0 — Backtest Integrity & Research Methodology

## 1. Chronological 1-Minute Replay & Pessimistic Intrabar Rule
- **Base Resolution**: All simulations iterate chronologically over 1-minute UTC bars.
- **No Signal-Close Fills**: Signals evaluated on a 09:35 IST 5-minute close execute no earlier than the 09:36 IST 1-minute open plus half-spread and adverse slippage.
- **Pessimistic Intrabar Tie-Breaker**: If a 1-minute bar's `Low` breaches the active stop-loss and its `High` breaches trailing activation (or a higher trailing peak), the backtester pessimistically executes the stop-loss first.
- **Gap-Through-Stop Handling**: If a bar opens below the stop price, the exit fills at the adverse bar open minus slippage, never at the stop trigger price.

## 2. Walk-Forward & Statistical Classification
- **Chronological Split**: 60% In-Sample, 40% Out-of-Sample, plus rolling Walk-Forward windows.
- **Evidence Classification**:
  - **POSITIVE**: Out-of-Sample trade count `>= 100`, net expectancy `> 0.15R` after statutory costs, profit factor `>= 1.25`, and positive expectancy under `1.5x` slippage stress.
  - **NEGATIVE**: Net Out-of-Sample expectancy `<= 0` or failure under slippage stress.
  - **INCONCLUSIVE**: Out-of-Sample trade count `< 100` or insufficient historical depth.
