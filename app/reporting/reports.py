"""Deterministic Daily and Research Report Generator."""

from datetime import datetime, timezone
from decimal import Decimal
from app.core.database import Session
from app.core.database import BacktestRunRecord, BacktestTradeRecord, OrderRecord, PositionRecord
from app.core.money import round_paise, to_decimal
from app.portfolio.ledger import PortfolioLedger


def generate_daily_report(db: Session, ledger: PortfolioLedger, session_date: str) -> dict:
  positions = db.query(PositionRecord).filter(PositionRecord.session_date == session_date).all()
  orders = db.query(OrderRecord).filter(OrderRecord.session_date == session_date).all()
  closed_pos = [p for p in positions if p.status == "OPEN" or p.status == "CLOSED"]
  realized_pnl = ledger.get_daily_realized_pnl(session_date)
  unrealized_pnl = ledger.get_daily_unrealized_pnl()
  total_fees = round_paise(
      sum((to_decimal(p.entry_fees_inr) + to_decimal(p.exit_fees_inr) for p in closed_pos), Decimal("0.00"))
  )
  reconciled, rec_msg = ledger.reconcile()

  return {
      "session_date": session_date,
      "generated_at_utc": datetime.now(timezone.utc).isoformat(),
      "execution_mode": "PAPER",
      "starting_capital_inr": str(ledger.starting_capital_inr),
      "cash_balance_inr": str(ledger.get_cash_balance()),
      "deployed_exposure_inr": str(ledger.get_deployed_exposure()),
      "realized_pnl_inr": str(realized_pnl),
      "unrealized_pnl_inr": str(unrealized_pnl),
      "total_fees_inr": str(total_fees),
      "orders_count": len(orders),
      "open_positions_count": len([p for p in positions if p.status == "OPEN"]),
      "closed_positions_count": len([p for p in positions if p.status == "CLOSED"]),
      "ledger_reconciled": reconciled,
      "reconciliation_status": rec_msg,
      "llm_commentary_disclaimer": "Deterministic system report; no LLM modification of figures.",
  }


def list_backtest_summaries(db: Session) -> list[dict]:
  runs = db.query(BacktestRunRecord).order_by(BacktestRunRecord.id.desc()).all()
  result: list[dict] = []
  for r in runs:
    trades = db.query(BacktestTradeRecord).filter(BacktestTradeRecord.run_id == r.run_id).all()
    result.append({
        "run_id": r.run_id,
        "strategy_version": r.strategy_version,
        "cost_version": r.cost_version,
        "risk_profile": r.risk_profile,
        "dataset_id": r.dataset_id,
        "total_trades": r.total_trades,
        "win_rate_pct": str(r.win_rate_pct),
        "profit_factor": str(r.profit_factor),
        "net_expectancy_inr": str(r.net_expectancy_inr),
        "max_drawdown_pct": str(r.max_drawdown_pct),
        "avg_cost_to_risk_pct": str(r.avg_cost_to_risk_pct),
        "evidence_classification": r.evidence_classification,
        "trades": [
            {
                "symbol": t.symbol,
                "session_date": t.session_date,
                "quantity": t.quantity,
                "entry_price": str(t.entry_price),
                "exit_price": str(t.exit_price),
                "net_pnl_inr": str(t.net_pnl_inr),
                "r_multiple_net": str(t.r_multiple_net),
                "exit_reason": t.exit_reason,
            }
            for t in trades
        ],
    })
  return result
