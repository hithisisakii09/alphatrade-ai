"""Event-Driven 1-Minute Chronological Backtester with Pessimistic Intrabar Priority & Look-Ahead Guard."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import uuid
from app.core.database import Session
from app.core.clock import to_ist
from app.core.config import AppConfig
from app.core.database import BacktestRunRecord, BacktestTradeRecord
from app.core.money import round_paise, round_price, to_decimal
from app.data.instruments import InstrumentRegistry
from app.data.models import CandleBar
from app.data.validator import CandleIngestionValidator, aggregate_1m_to_nm
from app.execution.costs import NSECostEngine
from app.execution.paper_broker import PaperBroker
from app.market.indicators import (
    compute_opening_range,
    compute_session_vwap,
    compute_time_of_day_rvol,
)
from app.portfolio.ledger import PortfolioLedger
from app.risk.engine import RiskEngine
from app.strategy.orb.engine import ORBStrategyEngine


class LookAheadLeakageError(RuntimeError):
  """Raised when any component attempts to access a bar with timestamp > current_bar.timestamp_utc."""


@dataclass(frozen=True)
class BacktestMetricsSummary:
  run_id: str
  dataset_id: str
  risk_profile: str
  total_trades: int
  wins: int
  losses: int
  win_rate_pct: Decimal
  profit_factor: Decimal
  net_expectancy_inr: Decimal
  net_expectancy_r: Decimal
  max_drawdown_pct: Decimal
  avg_holding_minutes: Decimal
  max_consecutive_losses: int
  rejection_rate_pct: Decimal
  avg_cost_to_risk_pct: Decimal
  evidence_classification: str


class ChronologicalBarWindow:
  """Wraps a bar list and raises LookAheadLeakageError if code inspects beyond current index."""

  def __init__(self, all_bars: list[CandleBar], current_idx: int):
    self._all_bars = all_bars
    self._current_idx = current_idx
    self.current_timestamp_utc = all_bars[current_idx].timestamp_utc

  def visible_bars(self) -> list[CandleBar]:
    return self._all_bars[: self._current_idx + 1]

  def access_bar_at(self, idx: int) -> CandleBar:
    bar = self._all_bars[idx]
    if bar.timestamp_utc > self.current_timestamp_utc:
      raise LookAheadLeakageError(
          f"Look-ahead violation: attempted to read future bar at {bar.timestamp_utc.isoformat()} "
          f"while current clock is {self.current_timestamp_utc.isoformat()}."
      )
    return bar


class EventDrivenBacktester:
  """Replays 1-minute bars chronologically, enforcing next-bar fills and pessimistic intrabar stops."""

  def __init__(
      self,
      db: Session,
      app_config: AppConfig,
      registry: InstrumentRegistry | None = None,
  ):
    self.db = db
    self.config = app_config
    self.registry = registry or InstrumentRegistry()
    self.risk_profile = app_config.strategy.risk_profiles[app_config.active_risk_profile]
    self.cost_profile = app_config.costs[app_config.active_cost_profile]
    self.cost_engine = NSECostEngine(self.cost_profile)
    self.risk_engine = RiskEngine(
        self.risk_profile,
        self.cost_engine,
        entry_cutoff_ist=app_config.strategy.entry_cutoff_ist,
        liquidation_start_ist=app_config.strategy.liquidation_start_ist,
        liquidation_deadline_ist=app_config.strategy.liquidation_deadline_ist,
    )
    self.strategy_engine = ORBStrategyEngine(app_config.strategy, self.registry)
    self.ledger = PortfolioLedger(db, self.risk_profile.reference_capital)
    self.broker = PaperBroker(db, self.ledger, self.cost_engine, self.risk_engine)
    self.validator = CandleIngestionValidator()

  def run_backtest(
      self,
      bars_1m_by_symbol: dict[str, list[CandleBar]],
      prev_closes_by_symbol: dict[str, Decimal],
      historical_minute_volumes: dict[str, list[int]] | None = None,
      dataset_id: str = "SYNTHETIC_VERIFY_V2",
      custom_slippage_pct: Decimal | None = None,
  ) -> BacktestMetricsSummary:
    run_id = f"BTR_{uuid.uuid4().hex[:10]}"
    pending_orders_by_symbol: dict[str, tuple] = {}
    trade_records: list[BacktestTradeRecord] = []
    candidate_breakouts = 0
    rejected_breakouts = 0
    cost_to_risk_samples: list[Decimal] = []

    peak_equity = self.risk_profile.reference_capital
    max_dd_pct = Decimal("0.0000")

    for sym, bars in bars_1m_by_symbol.items():
      sorted_bars = sorted(bars, key=lambda b: b.timestamp_utc)
      session_bars: list[CandleBar] = []
      current_session_date: str | None = None
      prev_close = prev_closes_by_symbol.get(sym, Decimal("1000.00"))
      lower_circuit, upper_circuit = self.registry.compute_price_bands(sym, prev_close)

      for idx in range(len(sorted_bars)):
        window = ChronologicalBarWindow(sorted_bars, idx)
        bar = window.access_bar_at(idx)
        valid, _ = self.validator.validate_bar(bar)
        if not valid or sym in self.validator.quarantined_symbols:
          continue

        dt_ist = to_ist(bar.timestamp_utc)
        s_date = dt_ist.date().isoformat()
        if s_date != current_session_date:
          current_session_date = s_date
          session_bars = []
          self.risk_engine.reset_kill_switch()

        session_bars.append(bar)
        orb_state = compute_opening_range(session_bars, self.config.strategy.opening_range_minutes)

        # 1. Execute any pending order at THIS bar's open (next-bar open execution)
        if sym in pending_orders_by_symbol:
          pend_order, pend_orb_low, pend_1r, pend_c2r = pending_orders_by_symbol.pop(sym)
          fill = self.broker.execute_fill_on_bar(
              pend_order,
              bar_open_price=bar.open,
              timestamp_utc=bar.timestamp_utc,
              orb_low=pend_orb_low,
              upper_circuit=upper_circuit,
              lower_circuit=lower_circuit,
              custom_slippage_pct=custom_slippage_pct,
          )
          if fill and pend_order.side == "BUY":
            cost_to_risk_samples.append(pend_c2r)

        # 2. Manage open position on THIS 1-minute bar (pessimistic intrabar stop priority)
        open_pos = self.ledger.get_open_position_by_symbol(sym)
        if open_pos is not None:
          eval_res = self.risk_engine.evaluate_position_bar_1m(
              bar_open=bar.open,
              bar_high=bar.high,
              bar_low=bar.low,
              bar_close=bar.close,
              bar_timestamp_utc=bar.timestamp_utc,
              initial_stop_price=to_decimal(open_pos.initial_stop_price),
              trailing_activation_price=to_decimal(open_pos.trailing_activation_price),
              trailing_drop_pct=to_decimal(open_pos.trailing_drop_pct),
              trailing_activated=bool(open_pos.trailing_activated),
              highest_price_since_entry=to_decimal(open_pos.highest_price_since_entry),
          )
          open_pos.trailing_activated = eval_res.trailing_activated
          open_pos.highest_price_since_entry = eval_res.highest_price_since_entry
          open_pos.current_stop_price = eval_res.current_stop_price
          self.db.commit()

          if eval_res.should_exit and eval_res.trigger_price is not None:
            exit_order, _ = self.broker.submit_order(
                idempotency_key=f"EXIT_{open_pos.position_id}_{bar.timestamp_utc.isoformat()}",
                symbol=sym,
                session_date=s_date,
                side="SELL",
                quantity=open_pos.quantity,
                reference_price=eval_res.trigger_price,
                timestamp_utc=bar.timestamp_utc,
            )
            exit_order.reason = eval_res.exit_reason
            sell_fill = self.broker.execute_fill_on_bar(
                exit_order,
                bar_open_price=eval_res.trigger_price,
                timestamp_utc=bar.timestamp_utc,
                upper_circuit=upper_circuit,
                lower_circuit=lower_circuit,
                custom_slippage_pct=custom_slippage_pct,
            )
            if sell_fill is not None:
              closed_pos = (
                  self.db.query(type(open_pos))
                  .filter_by(position_id=open_pos.position_id)
                  .first()
              )
              init_risk_per_sh = max(
                  Decimal("0.01"),
                  to_decimal(closed_pos.entry_price) - to_decimal(closed_pos.initial_stop_price),
              )
              total_1r = init_risk_per_sh * Decimal(closed_pos.quantity)
              r_mult = round_price(to_decimal(closed_pos.realized_pnl_inr) / total_1r)
              hold_mins = max(
                  1,
                  int((closed_pos.closed_at_utc - closed_pos.opened_at_utc).total_seconds() // 60),
              )
              gross_pnl = round_paise(
                  (to_decimal(closed_pos.exit_price) - to_decimal(closed_pos.entry_price))
                  * Decimal(closed_pos.quantity)
              )
              tot_fees = round_paise(
                  to_decimal(closed_pos.entry_fees_inr) + to_decimal(closed_pos.exit_fees_inr)
              )
              bt_trade = BacktestTradeRecord(
                  run_id=run_id,
                  symbol=sym,
                  session_date=s_date,
                  quantity=closed_pos.quantity,
                  entry_price=closed_pos.entry_price,
                  exit_price=closed_pos.exit_price,
                  gross_pnl_inr=gross_pnl,
                  total_fees_inr=tot_fees,
                  net_pnl_inr=closed_pos.realized_pnl_inr,
                  r_multiple_net=r_mult,
                  holding_minutes=hold_mins,
                  exit_reason=closed_pos.exit_reason or "EXIT",
              )
              self.db.add(bt_trade)
              self.db.commit()
              trade_records.append(bt_trade)

              snap = self.ledger.take_snapshot(bar.timestamp_utc)
              if snap.net_equity_inr > peak_equity:
                peak_equity = to_decimal(snap.net_equity_inr)
              dd = (peak_equity - to_decimal(snap.net_equity_inr)) / peak_equity * Decimal("100")
              if dd > max_dd_pct:
                max_dd_pct = round_price(dd)

        # 3. Evaluate 5-minute signal only on completed 5-minute boundaries (e.g. minute % 5 == 4)
        if dt_ist.minute % 5 == 4 and self.ledger.get_open_position_by_symbol(sym) is None and sym not in pending_orders_by_symbol:
          bars_5m = aggregate_1m_to_nm(session_bars, 5)
          if not bars_5m:
            continue
          latest_5m = bars_5m[-1]
          vwap = compute_session_vwap(session_bars)
          cum_vol = sum(b.volume for b in session_bars)
          hist_vols = (historical_minute_volumes or {}).get(sym, [max(1, cum_vol // 2)])
          rvol = compute_time_of_day_rvol(cum_vol, hist_vols)

          sig_eval = self.strategy_engine.evaluate_5m_bar(
              bar_5m=latest_5m,
              orb_state=orb_state,
              vwap=vwap,
              rvol=rvol,
              estimated_spread_pct=Decimal("0.0005"),
              prev_daily_close=prev_close,
              nifty_strongly_bearish=False,
          )
          if sig_eval.triggered and sig_eval.signal_id:
            candidate_breakouts += 1
            open_syms = {p.symbol for p in self.ledger.get_open_positions()}
            risk_dec = self.risk_engine.evaluate_entry_signal(
                symbol=sym,
                session_date=s_date,
                signal_timestamp_utc=bar.timestamp_utc,
                candidate_entry_price=bar.close,
                orb_low=orb_state.orb_low if orb_state else None,
                current_capital_inr=self.risk_profile.reference_capital,
                available_cash_inr=self.ledger.get_cash_balance(),
                deployed_exposure_inr=self.ledger.get_deployed_exposure(),
                open_positions_Symbols=open_syms,
                daily_realized_pnl_inr=self.ledger.get_daily_realized_pnl(s_date),
                daily_unrealized_pnl_inr=self.ledger.get_daily_unrealized_pnl(),
            )
            if risk_dec.approved and risk_dec.approved_quantity >= 1:
              order, _ = self.broker.submit_order(
                  idempotency_key=f"ENTRY_{sig_eval.signal_id}",
                  symbol=sym,
                  session_date=s_date,
                  side="BUY",
                  quantity=risk_dec.approved_quantity,
                  reference_price=bar.close,
                  timestamp_utc=bar.timestamp_utc,
                  signal_id=sig_eval.signal_id,
              )
              pending_orders_by_symbol[sym] = (
                  order,
                  orb_state.orb_low if orb_state else None,
                  risk_dec.risk_budget_inr,
                  risk_dec.cost_to_risk_ratio,
              )
            else:
              rejected_breakouts += 1

    # Compute summary metrics
    total_trades = len(trade_records)
    wins = sum(1 for t in trade_records if to_decimal(t.net_pnl_inr) > Decimal("0"))
    losses = total_trades - wins
    win_rate = round_price(Decimal(wins * 100) / Decimal(total_trades)) if total_trades > 0 else Decimal("0")
    gross_wins = sum((to_decimal(t.net_pnl_inr) for t in trade_records if to_decimal(t.net_pnl_inr) > Decimal("0")), Decimal("0.00"))
    gross_losses = abs(sum((to_decimal(t.net_pnl_inr) for t in trade_records if to_decimal(t.net_pnl_inr) <= Decimal("0")), Decimal("0.00")))
    profit_factor = round_price(gross_wins / gross_losses) if gross_losses > Decimal("0") else (Decimal("9.99") if gross_wins > Decimal("0") else Decimal("0"))
    net_exp_inr = round_paise(sum((to_decimal(t.net_pnl_inr) for t in trade_records), Decimal("0.00")) / Decimal(total_trades)) if total_trades > 0 else Decimal("0.00")
    net_exp_r = round_price(sum((to_decimal(t.r_multiple_net) for t in trade_records), Decimal("0.0000")) / Decimal(total_trades)) if total_trades > 0 else Decimal("0.0000")
    avg_hold = round_price(Decimal(sum(t.holding_minutes for t in trade_records)) / Decimal(total_trades)) if total_trades > 0 else Decimal("0")

    max_streak = 0
    curr_streak = 0
    for t in trade_records:
      if to_decimal(t.net_pnl_inr) <= Decimal("0"):
        curr_streak += 1
        max_streak = max(max_streak, curr_streak)
      else:
        curr_streak = 0

    rej_rate = round_price(Decimal(rejected_breakouts * 100) / Decimal(candidate_breakouts)) if candidate_breakouts > 0 else Decimal("0")
    avg_c2r = round_price(sum(cost_to_risk_samples, Decimal("0")) * Decimal("100") / Decimal(len(cost_to_risk_samples))) if cost_to_risk_samples else Decimal("0")

    if total_trades < 100:
      classification = "INCONCLUSIVE_SAMPLE_SIZE_BELOW_100"
    elif net_exp_r > Decimal("0.15") and profit_factor >= Decimal("1.25"):
      classification = "POSITIVE_EVIDENCE"
    else:
      classification = "NEGATIVE_EVIDENCE"

    run_rec = BacktestRunRecord(
        run_id=run_id,
        strategy_version=self.config.strategy.version,
        cost_version=self.config.cost_version,
        risk_profile=self.config.active_risk_profile,
        dataset_id=dataset_id,
        total_trades=total_trades,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        net_expectancy_inr=net_exp_inr,
        max_drawdown_pct=max_dd_pct,
        avg_cost_to_risk_pct=avg_c2r,
        evidence_classification=classification,
    )
    self.db.add(run_rec)
    self.db.commit()

    return BacktestMetricsSummary(
        run_id=run_id,
        dataset_id=dataset_id,
        risk_profile=self.config.active_risk_profile,
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        net_expectancy_inr=net_exp_inr,
        net_expectancy_r=net_exp_r,
        max_drawdown_pct=max_dd_pct,
        avg_holding_minutes=avg_hold,
        max_consecutive_losses=max_streak,
        rejection_rate_pct=rej_rate,
        avg_cost_to_risk_pct=avg_c2r,
        evidence_classification=classification,
    )
