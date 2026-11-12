"""Independent Deterministic Risk Management Gate (Intraday v2.0 + Legacy 8/20/10 Exit Rules & Kill Switch)."""

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from app.core.clock import to_ist
from app.core.config import RiskProfileConfig
from app.core.money import round_paise, round_price, round_to_tick, to_decimal
from app.execution.costs import NSECostEngine


@dataclass(frozen=True)
class ExitLevels:
  entry_fill_price: Decimal
  initial_stop_price: Decimal
  trailing_activation_price: Decimal
  trailing_drop_pct: Decimal
  stop_distance_per_share: Decimal


@dataclass(frozen=True)
class TrailingEvaluation:
  trailing_activated: bool
  highest_price_since_entry: Decimal
  current_stop_price: Decimal
  should_exit: bool
  exit_reason: str | None
  trigger_price: Decimal | None


@dataclass(frozen=True)
class RiskApprovalDecision:
  approved: bool
  reason_code: str
  approved_quantity: int
  initial_stop_price: Decimal | None
  trailing_activation_price: Decimal | None
  risk_budget_inr: Decimal
  estimated_roundtrip_cost_inr: Decimal
  cost_to_risk_ratio: Decimal
  explanation: str


class RiskEngine:
  """Deterministic risk gate enforcing sizing, stops, daily loss limits, time windows, and kill switch."""

  def __init__(
      self,
      profile: RiskProfileConfig,
      cost_engine: NSECostEngine,
      entry_cutoff_ist: str = "14:30",
      liquidation_start_ist: str = "15:00",
      liquidation_deadline_ist: str = "15:15",
      registry: Any = None,
      var_engine: Any = None,
  ):
    self.profile = profile
    self.cost_engine = cost_engine
    self.registry = registry
    self.var_engine = var_engine
    self.entry_cutoff = time.fromisoformat(entry_cutoff_ist)
    self.liquidation_start = time.fromisoformat(liquidation_start_ist)
    self.liquidation_deadline = time.fromisoformat(liquidation_deadline_ist)
    self.kill_switch_active: bool = False
    self.kill_switch_reason: str | None = None
    self.accepted_symbols_by_date: dict[str, set[str]] = {}

  def trigger_kill_switch(self, reason: str) -> None:
    self.kill_switch_active = True
    self.kill_switch_reason = reason

  def reset_kill_switch(self) -> None:
    self.kill_switch_active = False
    self.kill_switch_reason = None

  def compute_exit_levels(
      self,
      entry_fill_price: Decimal,
      orb_low: Decimal | None = None,
      tick_size: Decimal = Decimal("0.05"),
  ) -> ExitLevels:
    """Computes initial stop-loss and trailing activation prices for either INTRADAY_V2 or LEGACY_8_20_10."""
    fill = to_decimal(entry_fill_price)
    if self.profile.exit_mode == "LEGACY_8_20_10":
      # Exact 8/20/10 specification math: stop = fill * 0.92, activation = fill * 1.20, drop = 10%
      init_stop = round_price(fill * (Decimal("1") - self.profile.initial_stop_pct))
      activation = round_price(fill * (Decimal("1") + self.profile.trailing_activation_pct))
      drop_pct = self.profile.trailing_drop_pct
      return ExitLevels(
          entry_fill_price=fill,
          initial_stop_price=init_stop,
          trailing_activation_price=activation,
          trailing_drop_pct=drop_pct,
          stop_distance_per_share=round_price(fill - init_stop),
      )

    # INTRADAY_V2 mode: default 1.25% stop (or ORB Low clamped between 0.50% and 1.50% below fill)
    pct_stop = fill * (Decimal("1") - self.profile.initial_stop_pct)
    min_stop_price = fill * (Decimal("1") - self.profile.max_stop_pct)  # max 1.50% distance
    max_stop_price = fill * (Decimal("1") - self.profile.min_stop_pct)  # min 0.50% distance

    candidate_stop = max(orb_low, pct_stop) if orb_low is not None else pct_stop
    clamped_stop = max(min_stop_price, min(max_stop_price, candidate_stop))
    init_stop = round_to_tick(clamped_stop, tick_size, direction="DOWN")
    stop_dist = round_price(fill - init_stop)

    # Trailing activation at +2R (or profile trailing_activation_pct = +2.50%)
    activation = round_to_tick(
        fill * (Decimal("1") + self.profile.trailing_activation_pct), tick_size, direction="UP"
    )
    return ExitLevels(
        entry_fill_price=fill,
        initial_stop_price=init_stop,
        trailing_activation_price=activation,
        trailing_drop_pct=self.profile.trailing_drop_pct,
        stop_distance_per_share=stop_dist,
    )

  def evaluate_position_bar_1m(
      self,
      bar_open: Decimal,
      bar_high: Decimal,
      bar_low: Decimal,
      bar_close: Decimal,
      bar_timestamp_utc: datetime,
      initial_stop_price: Decimal,
      trailing_activation_price: Decimal,
      trailing_drop_pct: Decimal,
      trailing_activated: bool,
      highest_price_since_entry: Decimal,
  ) -> TrailingEvaluation:
    """Evaluates a 1-minute bar with pessimistic intrabar stop priority (stop checked before new peak)."""
    dt_ist = to_ist(bar_timestamp_utc)
    curr_trail_stop = (
        round_price(highest_price_since_entry * (Decimal("1") - trailing_drop_pct))
        if trailing_activated
        else initial_stop_price
    )
    active_stop = max(initial_stop_price, curr_trail_stop) if trailing_activated else initial_stop_price

    # 1. Pessimistic intrabar check: check if bar_open or bar_low breached active stop FIRST
    if bar_open <= active_stop:
      return TrailingEvaluation(
          trailing_activated=trailing_activated,
          highest_price_since_entry=highest_price_since_entry,
          current_stop_price=active_stop,
          should_exit=True,
          exit_reason="GAP_BELOW_STOP_LOSS" if not trailing_activated else "GAP_BELOW_TRAILING_STOP",
          trigger_price=bar_open,
      )

    if bar_low <= active_stop:
      return TrailingEvaluation(
          trailing_activated=trailing_activated,
          highest_price_since_entry=highest_price_since_entry,
          current_stop_price=active_stop,
          should_exit=True,
          exit_reason="INITIAL_STOP_LOSS" if not trailing_activated else "TRAILING_STOP_EXIT",
          trigger_price=active_stop,
      )

    # 2. Update trailing activation & peak using bar_high
    new_activated = trailing_activated or (bar_high >= trailing_activation_price)
    new_peak = max(highest_price_since_entry, bar_high) if new_activated else highest_price_since_entry

    if new_activated:
      updated_trail_stop = round_price(new_peak * (Decimal("1") - trailing_drop_pct))
      active_stop = max(initial_stop_price, updated_trail_stop)
      # Check if price pulled back from new_peak to updated_trail_stop by bar_close
      if bar_close <= updated_trail_stop:
        return TrailingEvaluation(
            trailing_activated=True,
            highest_price_since_entry=new_peak,
            current_stop_price=active_stop,
            should_exit=True,
            exit_reason="TRAILING_STOP_EXIT",
            trigger_price=updated_trail_stop,
        )

    # 3. Check EOD forced liquidation window (15:00 - 15:15 IST)
    if dt_ist.time() >= self.liquidation_start:
      return TrailingEvaluation(
          trailing_activated=new_activated,
          highest_price_since_entry=new_peak,
          current_stop_price=active_stop,
          should_exit=True,
          exit_reason="EOD_FORCED_LIQUIDATION_1500",
          trigger_price=bar_close,
      )

    return TrailingEvaluation(
        trailing_activated=new_activated,
        highest_price_since_entry=new_peak,
        current_stop_price=active_stop,
        should_exit=False,
        exit_reason=None,
        trigger_price=None,
    )

  def evaluate_entry_signal(
      self,
      symbol: str,
      session_date: str,
      signal_timestamp_utc: datetime,
      candidate_entry_price: Decimal,
      orb_low: Decimal | None,
      current_capital_inr: Decimal,
      available_cash_inr: Decimal,
      deployed_exposure_inr: Decimal,
      open_positions_Symbols: set[str],
      daily_realized_pnl_inr: Decimal,
      daily_unrealized_pnl_inr: Decimal,
      data_is_fresh: bool = True,
      ledger_is_reconciled: bool = True,
      atr_value: Decimal | None = None,
      bar_volume: int | None = None,
  ) -> RiskApprovalDecision:
    """Evaluates a candidate strategy signal against all deterministic risk and portfolio limits."""
    cap = to_decimal(current_capital_inr)
    risk_budget = round_paise(cap * self.profile.max_risk_per_trade_pct)

    # 1. Safety & kill-switch checks
    if not data_is_fresh:
      self.trigger_kill_switch("STALE_OR_INVALID_MARKET_DATA")
    if not ledger_is_reconciled:
      self.trigger_kill_switch("LEDGER_RECONCILIATION_FAILURE")

    max_daily_loss = round_paise(cap * self.profile.max_daily_loss_pct)
    net_daily_pnl = to_decimal(daily_realized_pnl_inr) + to_decimal(daily_unrealized_pnl_inr)
    if net_daily_pnl <= -max_daily_loss:
      self.trigger_kill_switch("DAILY_LOSS_LIMIT_REACHED")

    if self.kill_switch_active:
      return RiskApprovalDecision(
          approved=False,
          reason_code=f"KILL_SWITCH_ACTIVE:{self.kill_switch_reason}",
          approved_quantity=0,
          initial_stop_price=None,
          trailing_activation_price=None,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=Decimal("0.00"),
          cost_to_risk_ratio=Decimal("0.0000"),
          explanation=f"Rejected: Kill switch active ({self.kill_switch_reason}).",
      )

    # 2. Time window check
    dt_ist = to_ist(signal_timestamp_utc)
    if dt_ist.time() >= self.entry_cutoff:
      return RiskApprovalDecision(
          approved=False,
          reason_code="ENTRY_AFTER_1430_CUTOFF",
          approved_quantity=0,
          initial_stop_price=None,
          trailing_activation_price=None,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=Decimal("0.00"),
          cost_to_risk_ratio=Decimal("0.0000"),
          explanation="Rejected: No new entries permitted at or after 14:30 IST.",
      )

    # 3. Duplicate position / averaging down check
    if symbol in open_positions_Symbols:
      return RiskApprovalDecision(
          approved=False,
          reason_code="DUPLICATE_POSITION_OR_AVERAGING_DOWN",
          approved_quantity=0,
          initial_stop_price=None,
          trailing_activation_price=None,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=Decimal("0.00"),
          cost_to_risk_ratio=Decimal("0.0000"),
          explanation=f"Rejected: Position already open in {symbol}; averaging down is prohibited.",
      )

    # 4. One accepted entry per symbol per day check
    accepted_today = self.accepted_symbols_by_date.setdefault(session_date, set())
    if symbol in accepted_today:
      return RiskApprovalDecision(
          approved=False,
          reason_code="MAX_ONE_ENTRY_PER_SYMBOL_PER_DAY",
          approved_quantity=0,
          initial_stop_price=None,
          trailing_activation_price=None,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=Decimal("0.00"),
          cost_to_risk_ratio=Decimal("0.0000"),
          explanation=f"Rejected: {symbol} already had an accepted entry on {session_date}.",
      )

    # 5. Maximum concurrent open positions check
    if len(open_positions_Symbols) >= self.profile.max_open_positions:
      return RiskApprovalDecision(
          approved=False,
          reason_code="MAX_OPEN_POSITIONS_REACHED",
          approved_quantity=0,
          initial_stop_price=None,
          trailing_activation_price=None,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=Decimal("0.00"),
          cost_to_risk_ratio=Decimal("0.0000"),
          explanation=f"Rejected: Maximum open positions ({self.profile.max_open_positions}) reached.",
      )

    # 5b. Maximum sector concentration check
    if self.registry and hasattr(self.registry, "get_sector"):
      cand_sector = self.registry.get_sector(symbol)
      if cand_sector and cand_sector != "GENERAL":
        max_sector_pos = getattr(self.profile, "max_positions_per_sector", 1)
        same_sector_count = sum(
            1 for s in open_positions_Symbols if self.registry.get_sector(s) == cand_sector
        )
        if same_sector_count >= max_sector_pos:
          return RiskApprovalDecision(
              approved=False,
              reason_code="SECTOR_CONCENTRATION_LIMIT_EXCEEDED",
              approved_quantity=0,
              initial_stop_price=None,
              trailing_activation_price=None,
              risk_budget_inr=risk_budget,
              estimated_roundtrip_cost_inr=Decimal("0.00"),
              cost_to_risk_ratio=Decimal("0.0000"),
              explanation=(
                  f"Rejected: Maximum open positions ({max_sector_pos}) for sector "
                  f"'{cand_sector}' already reached."
              ),
          )

    # 6. Position sizing arithmetic
    entry_p = to_decimal(candidate_entry_price)
    levels = self.compute_exit_levels(entry_p, orb_low=orb_low)
    effective_stop_dist = levels.stop_distance_per_share
    if atr_value is not None and atr_value > Decimal("0"):
      atr_mult = getattr(self.profile, "atr_volatility_multiplier", Decimal("1.5"))
      vol_stop_dist = round_price(atr_value * atr_mult)
      effective_stop_dist = max(levels.stop_distance_per_share, vol_stop_dist)

    trailing_activation_p = levels.trailing_activation_price
    if getattr(self.profile, "adaptive_atr_trailing", False) and atr_value is not None and atr_value > Decimal("0"):
      trail_mult = getattr(self.profile, "adaptive_atr_trail_multiplier", Decimal("2.0"))
      trailing_activation_p = round_price(entry_p + (atr_value * trail_mult))

    if effective_stop_dist <= Decimal("0"):
      return RiskApprovalDecision(
          approved=False,
          reason_code="INVALID_STOP_DISTANCE",
          approved_quantity=0,
          initial_stop_price=levels.initial_stop_price,
          trailing_activation_price=trailing_activation_p,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=Decimal("0.00"),
          cost_to_risk_ratio=Decimal("0.0000"),
          explanation="Rejected: Stop distance per share must be > 0.",
      )

    qty_by_risk = int((risk_budget / effective_stop_dist).to_integral_value(rounding=ROUND_DOWN))
    max_total_deployable = round_paise(cap * self.profile.max_capital_deployed_pct)
    remaining_exposure_cap = max(Decimal("0.00"), max_total_deployable - to_decimal(deployed_exposure_inr))
    max_single_pos_cap = round_paise(cap * self.profile.max_single_position_pct)
    usable_notional = min(remaining_exposure_cap, max_single_pos_cap, to_decimal(available_cash_inr))
    qty_by_capital = int((usable_notional / entry_p).to_integral_value(rounding=ROUND_DOWN))

    final_qty = min(qty_by_risk, qty_by_capital)
    if final_qty < 1:
      return RiskApprovalDecision(
          approved=False,
          reason_code="QUANTITY_BELOW_ONE_SHARE",
          approved_quantity=0,
          initial_stop_price=levels.initial_stop_price,
          trailing_activation_price=trailing_activation_p,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=Decimal("0.00"),
          cost_to_risk_ratio=Decimal("0.0000"),
          explanation=(
              f"Rejected: Whole-share quantity rounded to 0 (qty_by_risk={qty_by_risk}, "
              f"qty_by_capital={qty_by_capital} at price {entry_p})."
          ),
      )

    # 6b. Portfolio Value-at-Risk (VaR) Gate
    if getattr(self.profile, "require_portfolio_var_cap", False) and self.var_engine is not None:
      simulated_holdings: dict[str, Decimal] = {}
      for op in open_positions_Symbols:
        simulated_holdings[op] = round_paise(cap / Decimal(max(1, self.profile.max_open_positions)))
      simulated_holdings[symbol] = round_paise(Decimal(final_qty) * entry_p)

      var_report = self.var_engine.compute_portfolio_risk(
          holdings=simulated_holdings,
          total_equity_inr=cap,
      )
      max_allowed_var = round_paise(
          cap * getattr(self.profile, "max_portfolio_var_95_pct", Decimal("0.025"))
      )
      if var_report.metrics.var_95_inr > max_allowed_var:
        return RiskApprovalDecision(
            approved=False,
            reason_code="PORTFOLIO_VAR_LIMIT_EXCEEDED",
            approved_quantity=0,
            initial_stop_price=levels.initial_stop_price,
            trailing_activation_price=trailing_activation_p,
            risk_budget_inr=risk_budget,
            estimated_roundtrip_cost_inr=Decimal("0.00"),
            cost_to_risk_ratio=Decimal("0.0000"),
            explanation=(
                f"Rejected: Projected portfolio 95% VaR ₹{var_report.metrics.var_95_inr} "
                f"({var_report.metrics.var_95_pct * 100:.2f}%) exceeds limit ₹{max_allowed_var} "
                f"({getattr(self.profile, 'max_portfolio_var_95_pct', Decimal('0.025')) * 100:.2f}%)."
            ),
        )

    # 6c. Participation Rate & Liquidity Gate
    if getattr(self.profile, "require_participation_rate_cap", False) and bar_volume is not None and bar_volume > 0:
      max_part_pct = getattr(self.profile, "max_participation_rate_pct", Decimal("0.05"))
      max_allowed_qty = int((Decimal(bar_volume) * max_part_pct).to_integral_value(rounding=ROUND_DOWN))
      if final_qty > max_allowed_qty:
        return RiskApprovalDecision(
            approved=False,
            reason_code="PARTICIPATION_RATE_LIMIT_EXCEEDED",
            approved_quantity=0,
            initial_stop_price=levels.initial_stop_price,
            trailing_activation_price=trailing_activation_p,
            risk_budget_inr=risk_budget,
            estimated_roundtrip_cost_inr=Decimal("0.00"),
            cost_to_risk_ratio=Decimal("0.0000"),
            explanation=(
                f"Rejected: Requested quantity {final_qty} shares exceeds maximum participation "
                f"limit {max_allowed_qty} shares ({max_part_pct * 100:.1f}% of bar volume {bar_volume})."
            ),
        )

    # 7. Cost-to-risk ratio check
    actual_trade_risk_1r = round_paise(Decimal(final_qty) * levels.stop_distance_per_share)
    est_rt_cost = self.cost_engine.estimate_roundtrip_cost(final_qty, entry_p, bar_volume=bar_volume)
    cost_to_risk = round_price(est_rt_cost / actual_trade_risk_1r) if actual_trade_risk_1r > Decimal("0") else Decimal("1")

    if cost_to_risk > self.profile.max_cost_to_risk_ratio:
      return RiskApprovalDecision(
          approved=False,
          reason_code="COST_TO_RISK_RATIO_EXCEEDED",
          approved_quantity=0,
          initial_stop_price=levels.initial_stop_price,
          trailing_activation_price=trailing_activation_p,
          risk_budget_inr=risk_budget,
          estimated_roundtrip_cost_inr=est_rt_cost,
          cost_to_risk_ratio=cost_to_risk,
          explanation=(
              f"Rejected: Estimated round-trip cost {est_rt_cost} is {cost_to_risk * 100:.1f}% of 1R "
              f"({actual_trade_risk_1r}), exceeding max {self.profile.max_cost_to_risk_ratio * 100:.1f}%."
          ),
      )

    return RiskApprovalDecision(
        approved=True,
        reason_code="APPROVED",
        approved_quantity=final_qty,
        initial_stop_price=levels.initial_stop_price,
        trailing_activation_price=trailing_activation_p,
        risk_budget_inr=risk_budget,
        estimated_roundtrip_cost_inr=est_rt_cost,
        cost_to_risk_ratio=cost_to_risk,
        explanation=(
            f"Approved {final_qty} shares of {symbol} at ref {entry_p}; "
            f"stop={levels.initial_stop_price}, trail_act={trailing_activation_p}, "
            f"cost/risk={cost_to_risk * 100:.1f}%."
        ),
    )

  def record_order_accepted(self, session_date: str, symbol: str) -> None:
    """Marks a symbol as having an accepted entry on session_date (rejected orders do not call this)."""
    self.accepted_symbols_by_date.setdefault(session_date, set()).add(symbol)
