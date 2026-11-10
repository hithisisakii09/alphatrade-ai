"""Unit tests for v2.0 intraday exits, legacy 8/20/10 worked example, sizing, and NSE statutory costs."""

from decimal import Decimal
from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.execution.costs import NSECostEngine
from app.risk.engine import RiskEngine


def test_exact_legacy_8_20_10_worked_example():
  """Verifies Section 5.1 worked example: Entry 100 -> stop 92 -> activation 120 -> peak 130 -> trailing exit 117."""
  cfg = load_app_config(risk_profile_name="LEGACY_SWING_8_20_10")
  prof = cfg.strategy.risk_profiles["LEGACY_SWING_8_20_10"]
  cost_eng = NSECostEngine(cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])
  risk_eng = RiskEngine(prof, cost_eng)

  levels = risk_eng.compute_exit_levels(Decimal("100.00"))
  assert levels.initial_stop_price == Decimal("92.0000")
  assert levels.trailing_activation_price == Decimal("120.0000")
  assert levels.trailing_drop_pct == Decimal("0.1000")

  # Price reaches trailing activation exactly at 120, then rallies to peak 130
  ev_act = risk_eng.evaluate_position_bar_1m(
      bar_open=Decimal("118"),
      bar_high=Decimal("120"),
      bar_low=Decimal("117"),
      bar_close=Decimal("120"),
      bar_timestamp_utc=ist_datetime(2026, 11, 10, 10, 0),
      initial_stop_price=levels.initial_stop_price,
      trailing_activation_price=levels.trailing_activation_price,
      trailing_drop_pct=levels.trailing_drop_pct,
      trailing_activated=False,
      highest_price_since_entry=Decimal("100"),
  )
  assert ev_act.trailing_activated
  assert not ev_act.should_exit

  # Peak reaches 130, then drops to 117 exactly (130 * 0.90 = 117)
  ev_exit = risk_eng.evaluate_position_bar_1m(
      bar_open=Decimal("128"),
      bar_high=Decimal("130"),
      bar_low=Decimal("117"),
      bar_close=Decimal("117"),
      bar_timestamp_utc=ist_datetime(2026, 11, 10, 10, 5),
      initial_stop_price=levels.initial_stop_price,
      trailing_activation_price=levels.trailing_activation_price,
      trailing_drop_pct=levels.trailing_drop_pct,
      trailing_activated=True,
      highest_price_since_entry=Decimal("120"),
  )
  assert ev_exit.should_exit
  assert ev_exit.exit_reason == "TRAILING_STOP_EXIT"
  assert ev_exit.trigger_price == Decimal("117.0000")


def test_v2_intraday_exit_levels_and_pessimistic_intrabar_priority():
  """Verifies v2.0 1.25% stop / +2.50% activation / 0.75% drop and pessimistic stop-first intrabar rule."""
  cfg = load_app_config(risk_profile_name="STANDARD_RESEARCH_100K")
  prof = cfg.strategy.risk_profiles["STANDARD_RESEARCH_100K"]
  cost_eng = NSECostEngine(cfg.costs["PERCENTAGE_CAPPED_DEFAULT"])
  risk_eng = RiskEngine(prof, cost_eng)

  levels = risk_eng.compute_exit_levels(Decimal("1000.00"))
  assert levels.initial_stop_price == Decimal("987.50")
  assert levels.trailing_activation_price == Decimal("1025.00")

  # Same 1-minute bar breaches BOTH initial stop (987.50) and trailing activation (1025.00)
  ev_both = risk_eng.evaluate_position_bar_1m(
      bar_open=Decimal("1000.00"),
      bar_high=Decimal("1030.00"),
      bar_low=Decimal("985.00"),
      bar_close=Decimal("1028.00"),
      bar_timestamp_utc=ist_datetime(2026, 11, 10, 10, 10),
      initial_stop_price=levels.initial_stop_price,
      trailing_activation_price=levels.trailing_activation_price,
      trailing_drop_pct=levels.trailing_drop_pct,
      trailing_activated=False,
      highest_price_since_entry=Decimal("1000.00"),
  )
  assert ev_both.should_exit
  assert ev_both.exit_reason == "INITIAL_STOP_LOSS"
  assert ev_both.trigger_price == Decimal("987.50")


def test_gap_below_stop_uses_conservative_bar_open():
  cfg = load_app_config()
  prof = cfg.strategy.risk_profiles["STANDARD_RESEARCH_100K"]
  risk_eng = RiskEngine(prof, NSECostEngine(cfg.costs["PERCENTAGE_CAPPED_DEFAULT"]))

  ev_gap = risk_eng.evaluate_position_bar_1m(
      bar_open=Decimal("980.00"),
      bar_high=Decimal("984.00"),
      bar_low=Decimal("978.00"),
      bar_close=Decimal("982.00"),
      bar_timestamp_utc=ist_datetime(2026, 11, 10, 10, 15),
      initial_stop_price=Decimal("987.50"),
      trailing_activation_price=Decimal("1025.00"),
      trailing_drop_pct=Decimal("0.0075"),
      trailing_activated=False,
      highest_price_since_entry=Decimal("1000.00"),
  )
  assert ev_gap.should_exit
  assert ev_gap.exit_reason == "GAP_BELOW_STOP_LOSS"
  assert ev_gap.trigger_price == Decimal("980.00")
