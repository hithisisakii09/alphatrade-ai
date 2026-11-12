"""Trade execution analytics, MAE/MFE computation, sector attribution, and journal generation."""

import csv
from datetime import datetime, timezone
from decimal import Decimal
import io
from app.core.clock import to_ist
from app.core.database import MarketCandleRecord, PositionRecord, Session
from app.core.money import round_paise, round_price, to_decimal
from app.data.instruments import InstrumentRegistry


def compute_position_mae_mfe(pos: PositionRecord, db: Session | None = None) -> dict:
  """Computes Maximum Favorable Excursion (MFE) and Maximum Adverse Excursion (MAE) in points and R-multiples."""
  entry_p = to_decimal(pos.entry_price)
  stop_p = to_decimal(pos.initial_stop_price)
  risk_per_share = abs(entry_p - stop_p)
  if risk_per_share <= Decimal("0"):
    risk_per_share = entry_p * Decimal("0.01")  # 1% fallback

  highest_p = to_decimal(pos.highest_price_since_entry)
  mfe_points = max(Decimal("0.00"), highest_p - entry_p)
  mfe_r = round_price(mfe_points / risk_per_share)

  # Determine MAE (lowest price experienced during trade)
  lowest_p = entry_p
  if db and pos.opened_at_utc:
    end_time = pos.closed_at_utc or datetime.now(timezone.utc)
    bars = (
        db.query(MarketCandleRecord)
        .filter(MarketCandleRecord.symbol == pos.symbol)
        .all()
    )
    relevant_bars = [
        b for b in bars
        if b.timestamp_utc and pos.opened_at_utc <= b.timestamp_utc <= end_time
    ]
    if relevant_bars:
      lowest_p = min(to_decimal(b.low) for b in relevant_bars)
    elif pos.exit_price:
      lowest_p = min(entry_p, to_decimal(pos.exit_price))
  elif pos.exit_price:
    lowest_p = min(entry_p, to_decimal(pos.exit_price))

  mae_points = max(Decimal("0.00"), entry_p - lowest_p)
  mae_r = round_price(mae_points / risk_per_share)

  # Holding duration in minutes
  duration_mins = 0
  if pos.opened_at_utc:
    end_dt = pos.closed_at_utc or datetime.now(timezone.utc)
    duration_mins = max(0, int((end_dt - pos.opened_at_utc).total_seconds() // 60))

  # Net R-multiple realized
  qty = Decimal(str(pos.quantity))
  risk_capital = risk_per_share * qty
  realized_pnl = to_decimal(pos.realized_pnl_inr)
  realized_r = round_price(realized_pnl / risk_capital) if risk_capital > Decimal("0") else Decimal("0.00")

  return {
      "risk_per_share": str(risk_per_share),
      "mfe_points": str(mfe_points),
      "mfe_r": str(mfe_r),
      "mae_points": str(mae_points),
      "mae_r": str(mae_r),
      "realized_r": str(realized_r),
      "duration_minutes": duration_mins,
  }


def compute_trade_analytics(db: Session, session_date: str | None = None) -> dict:
  """Computes comprehensive trade performance analytics, MAE/MFE statistics, and sector breakdown."""
  query = db.query(PositionRecord)
  if session_date:
    query = query.filter(PositionRecord.session_date == session_date)
  positions = query.order_by(PositionRecord.id.asc()).all()

  registry = InstrumentRegistry()

  total_trades = len(positions)
  closed_positions = [p for p in positions if p.status == "CLOSED"]
  open_positions = [p for p in positions if p.status == "OPEN"]

  wins: list[Decimal] = []
  losses: list[Decimal] = []
  mfe_r_list: list[Decimal] = []
  mae_r_list: list[Decimal] = []
  realized_r_list: list[Decimal] = []
  sector_stats: dict[str, dict] = {}

  enriched_trades: list[dict] = []

  for p in positions:
    analytics = compute_position_mae_mfe(p, db=db)
    sec = registry.get_sector(p.symbol)

    mfe_r = to_decimal(analytics["mfe_r"])
    mae_r = to_decimal(analytics["mae_r"])
    mfe_r_list.append(mfe_r)
    mae_r_list.append(mae_r)

    pnl = to_decimal(p.realized_pnl_inr)
    is_closed = p.status == "CLOSED"

    if is_closed:
      r_mult = to_decimal(analytics["realized_r"])
      realized_r_list.append(r_mult)
      if pnl > Decimal("0.00"):
        wins.append(pnl)
      elif pnl < Decimal("0.00"):
        losses.append(pnl)

    # Sector aggregation
    if sec not in sector_stats:
      sector_stats[sec] = {
          "sector": sec,
          "trade_count": 0,
          "wins": 0,
          "losses": 0,
          "gross_pnl_inr": Decimal("0.00"),
          "fees_inr": Decimal("0.00"),
          "net_pnl_inr": Decimal("0.00"),
      }
    s_entry = sector_stats[sec]
    s_entry["trade_count"] += 1
    total_fees = to_decimal(p.entry_fees_inr) + to_decimal(p.exit_fees_inr)
    s_entry["fees_inr"] = round_paise(s_entry["fees_inr"] + total_fees)
    s_entry["net_pnl_inr"] = round_paise(s_entry["net_pnl_inr"] + pnl)
    if is_closed:
      if pnl > Decimal("0"):
        s_entry["wins"] += 1
      elif pnl < Decimal("0"):
        s_entry["losses"] += 1

    entry_ist_str = to_ist(p.opened_at_utc).strftime("%Y-%m-%d %H:%M:%S") if p.opened_at_utc else ""
    exit_ist_str = to_ist(p.closed_at_utc).strftime("%Y-%m-%d %H:%M:%S") if p.closed_at_utc else ""

    enriched_trades.append({
        "position_id": p.position_id,
        "symbol": p.symbol,
        "sector": sec,
        "session_date": p.session_date,
        "status": p.status,
        "quantity": p.quantity,
        "entry_time_ist": entry_ist_str,
        "entry_price": str(p.entry_price),
        "exit_time_ist": exit_ist_str,
        "exit_price": str(p.exit_price) if p.exit_price else None,
        "initial_stop_price": str(p.initial_stop_price),
        "current_stop_price": str(p.current_stop_price),
        "highest_price": str(p.highest_price_since_entry),
        "exit_reason": p.exit_reason,
        "realized_pnl_inr": str(p.realized_pnl_inr),
        "entry_fees_inr": str(p.entry_fees_inr),
        "exit_fees_inr": str(p.exit_fees_inr),
        "total_fees_inr": str(total_fees),
        "mfe_points": analytics["mfe_points"],
        "mfe_r": analytics["mfe_r"],
        "mae_points": analytics["mae_points"],
        "mae_r": analytics["mae_r"],
        "realized_r": analytics["realized_r"],
        "duration_minutes": analytics["duration_minutes"],
    })

  total_wins_cnt = len(wins)
  total_losses_cnt = len(losses)
  closed_cnt = len(closed_positions)

  win_rate_pct = round_price((Decimal(total_wins_cnt) / Decimal(closed_cnt)) * Decimal("100")) if closed_cnt > 0 else Decimal("0.00")
  gross_wins_sum = sum(wins, Decimal("0.00"))
  gross_losses_sum = abs(sum(losses, Decimal("0.00")))
  profit_factor = round_price(gross_wins_sum / gross_losses_sum) if gross_losses_sum > Decimal("0") else (Decimal("999.00") if gross_wins_sum > 0 else Decimal("0.00"))

  avg_win = round_paise(gross_wins_sum / Decimal(total_wins_cnt)) if total_wins_cnt > 0 else Decimal("0.00")
  avg_loss = round_paise(gross_losses_sum / Decimal(total_losses_cnt)) if total_losses_cnt > 0 else Decimal("0.00")
  payoff_ratio = round_price(avg_win / avg_loss) if avg_loss > Decimal("0") else Decimal("0.00")

  avg_mfe_r = round_price(sum(mfe_r_list, Decimal("0")) / Decimal(len(mfe_r_list))) if mfe_r_list else Decimal("0.00")
  avg_mae_r = round_price(sum(mae_r_list, Decimal("0")) / Decimal(len(mae_r_list))) if mae_r_list else Decimal("0.00")
  max_mfe_r = max(mfe_r_list) if mfe_r_list else Decimal("0.00")
  max_mae_r = max(mae_r_list) if mae_r_list else Decimal("0.00")
  avg_realized_r = round_price(sum(realized_r_list, Decimal("0")) / Decimal(len(realized_r_list))) if realized_r_list else Decimal("0.00")

  # Efficiency ratio (Avg Realized R / Avg MFE R)
  efficiency_pct = round_price((avg_realized_r / avg_mfe_r) * Decimal("100")) if avg_mfe_r > Decimal("0") else Decimal("0.00")

  # Format sector breakdown
  formatted_sectors = []
  for sec_name, s_val in sector_stats.items():
    s_closed = s_val["wins"] + s_val["losses"]
    s_wr = round_price((Decimal(s_val["wins"]) / Decimal(s_closed)) * Decimal("100")) if s_closed > 0 else Decimal("0.00")
    formatted_sectors.append({
        "sector": sec_name,
        "trade_count": s_val["trade_count"],
        "wins": s_val["wins"],
        "losses": s_val["losses"],
        "win_rate_pct": str(s_wr),
        "total_fees_inr": str(s_val["fees_inr"]),
        "net_pnl_inr": str(s_val["net_pnl_inr"]),
    })
  formatted_sectors.sort(key=lambda x: to_decimal(x["net_pnl_inr"]), reverse=True)

  return {
      "session_date_filter": session_date,
      "total_trades": total_trades,
      "open_trades": len(open_positions),
      "closed_trades": closed_cnt,
      "winning_trades": total_wins_cnt,
      "losing_trades": total_losses_cnt,
      "win_rate_pct": str(win_rate_pct),
      "profit_factor": str(profit_factor),
      "avg_win_inr": str(avg_win),
      "avg_loss_inr": str(avg_loss),
      "payoff_ratio": str(payoff_ratio),
      "avg_mfe_r": str(avg_mfe_r),
      "avg_mae_r": str(avg_mae_r),
      "max_mfe_r": str(max_mfe_r),
      "max_mae_r": str(max_mae_r),
      "avg_realized_r": str(avg_realized_r),
      "capture_efficiency_pct": str(efficiency_pct),
      "sector_breakdown": formatted_sectors,
      "trades": enriched_trades,
  }


def generate_trade_journal_csv(db: Session, session_date: str | None = None) -> str:
  """Generates an RFC 4180 compliant CSV string representing the complete trade execution journal."""
  analytics = compute_trade_analytics(db, session_date=session_date)
  trades = analytics["trades"]

  output = io.StringIO()
  fieldnames = [
      "position_id",
      "session_date",
      "symbol",
      "sector",
      "status",
      "quantity",
      "entry_time_ist",
      "entry_price",
      "exit_time_ist",
      "exit_price",
      "initial_stop_price",
      "current_stop_price",
      "highest_price",
      "exit_reason",
      "total_fees_inr",
      "realized_pnl_inr",
      "realized_r",
      "mfe_points",
      "mfe_r",
      "mae_points",
      "mae_r",
      "duration_minutes",
  ]
  writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\r\n")
  writer.writeheader()
  for t in trades:
    writer.writerow(t)

  return output.getvalue()
