"""Unit tests for the NANO_5K ₹5,000 autonomous trading profile.

Verifies:
- Profile loads correctly from orb_v2.json with the right capital and stop-loss parameters.
- 5% hard stop-loss is enforced (initial_stop_pct = min_stop_pct = max_stop_pct = 0.0500).
- RiskEngine computes correct exit levels for a low-price stock entry at ₹150.
- RiskEngine approves/rejects trades based on ₹5,000 capital constraints.
- ScheduledDailyRunner selects the NANO_5K low-price symbols universe automatically.
"""
import os
from decimal import Decimal

import pytest

from app.core.clock import ist_datetime
from app.core.config import load_app_config
from app.execution.costs import NSECostEngine
from app.risk.engine import RiskEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_nano_profile():
    cfg = load_app_config(risk_profile_name="NANO_5K")
    return cfg, cfg.strategy.risk_profiles["NANO_5K"]


# ---------------------------------------------------------------------------
# Test 1: Profile parameters load correctly
# ---------------------------------------------------------------------------

def test_nano_5k_profile_loads():
    """NANO_5K profile must exist and carry correct capital and stop values."""
    cfg, prof = _get_nano_profile()
    assert prof.reference_capital == Decimal("5000.00"), (
        f"Expected ₹5,000 reference capital, got {prof.reference_capital}"
    )
    assert prof.initial_stop_pct == Decimal("0.0500"), (
        f"Expected 5% initial stop, got {prof.initial_stop_pct}"
    )
    assert prof.min_stop_pct == Decimal("0.0500"), (
        f"Expected min_stop_pct=5%, got {prof.min_stop_pct}"
    )
    assert prof.max_stop_pct == Decimal("0.0500"), (
        f"Expected max_stop_pct=5%, got {prof.max_stop_pct}"
    )
    assert prof.max_open_positions == 1, (
        f"Expected max_open_positions=1 for ₹5K capital, got {prof.max_open_positions}"
    )
    assert prof.max_capital_deployed_pct == Decimal("0.80"), (
        f"Expected 80% deployment cap, got {prof.max_capital_deployed_pct}"
    )


# ---------------------------------------------------------------------------
# Test 2: 5% stop-loss exit level on a low-price stock (₹150 entry)
# ---------------------------------------------------------------------------

def test_nano_5k_stop_loss_exit_levels_tatasteel():
    """Exit level must be exactly 5% below entry for ₹150 stock (TATASTEEL proxy)."""
    cfg, prof = _get_nano_profile()
    cost_eng = NSECostEngine(cfg.costs[cfg.active_cost_profile])
    risk_eng = RiskEngine(prof, cost_eng)

    entry = Decimal("150.00")
    levels = risk_eng.compute_exit_levels(entry)

    # 5% stop → ₹150 × 0.95 = ₹142.50
    expected_stop = Decimal("142.50")
    assert levels.initial_stop_price == expected_stop, (
        f"Expected initial stop ₹{expected_stop}, got ₹{levels.initial_stop_price}"
    )

    # Trailing activates at +3% (₹150 × 1.03 = ₹154.50)
    expected_activation = Decimal("154.50")
    assert levels.trailing_activation_price == expected_activation, (
        f"Expected trailing activation ₹{expected_activation}, got ₹{levels.trailing_activation_price}"
    )


# ---------------------------------------------------------------------------
# Test 3: Stop triggered when price falls 5% below entry
# ---------------------------------------------------------------------------

def test_nano_5k_stop_triggers_on_5pct_drop():
    """RiskEngine must signal exit when bar_low breaches the 5% stop price."""
    cfg, prof = _get_nano_profile()
    cost_eng = NSECostEngine(cfg.costs[cfg.active_cost_profile])
    risk_eng = RiskEngine(prof, cost_eng)

    entry = Decimal("150.00")
    levels = risk_eng.compute_exit_levels(entry)

    # Bar drops below ₹142.50 → should exit
    result = risk_eng.evaluate_position_bar_1m(
        bar_open=Decimal("148.00"),
        bar_high=Decimal("149.00"),
        bar_low=Decimal("141.00"),   # below ₹142.50 stop
        bar_close=Decimal("143.00"),
        bar_timestamp_utc=ist_datetime(2026, 11, 14, 10, 30),
        initial_stop_price=levels.initial_stop_price,
        trailing_activation_price=levels.trailing_activation_price,
        trailing_drop_pct=levels.trailing_drop_pct,
        trailing_activated=False,
        highest_price_since_entry=entry,
    )
    assert result.should_exit, "Stop-loss must trigger when bar_low < 5% stop price"
    assert result.exit_reason == "INITIAL_STOP_LOSS"
    assert result.trigger_price == levels.initial_stop_price


# ---------------------------------------------------------------------------
# Test 4: No stop-loss when price only dips 4% (within 5% threshold)
# ---------------------------------------------------------------------------

def test_nano_5k_no_exit_on_4pct_dip():
    """No exit when price only dips 4% — must stay below the 5% hard stop."""
    cfg, prof = _get_nano_profile()
    cost_eng = NSECostEngine(cfg.costs[cfg.active_cost_profile])
    risk_eng = RiskEngine(prof, cost_eng)

    entry = Decimal("150.00")
    levels = risk_eng.compute_exit_levels(entry)

    # Bar low = ₹144.00 (4% drop) — above ₹142.50 stop → should NOT exit
    result = risk_eng.evaluate_position_bar_1m(
        bar_open=Decimal("149.00"),
        bar_high=Decimal("150.00"),
        bar_low=Decimal("144.00"),   # 4% drop, above stop
        bar_close=Decimal("145.00"),
        bar_timestamp_utc=ist_datetime(2026, 11, 14, 10, 15),
        initial_stop_price=levels.initial_stop_price,
        trailing_activation_price=levels.trailing_activation_price,
        trailing_drop_pct=levels.trailing_drop_pct,
        trailing_activated=False,
        highest_price_since_entry=entry,
    )
    assert not result.should_exit, (
        "Must NOT exit on a 4% dip — only exit when drop exceeds 5% stop"
    )


# ---------------------------------------------------------------------------
# Test 5: Risk engine approves a viable NANO_5K entry for TATASTEEL
# ---------------------------------------------------------------------------

def test_nano_5k_risk_engine_approves_tatasteel_entry():
    """RiskEngine must approve a ₹150 entry for TATASTEEL with ₹5,000 capital."""
    cfg, prof = _get_nano_profile()
    cost_eng = NSECostEngine(cfg.costs[cfg.active_cost_profile])
    risk_eng = RiskEngine(prof, cost_eng)

    decision = risk_eng.evaluate_entry_signal(
        symbol="TATASTEEL",
        session_date="2026-11-14",
        signal_timestamp_utc=ist_datetime(2026, 11, 14, 9, 35),
        candidate_entry_price=Decimal("150.00"),
        orb_low=Decimal("148.00"),
        current_capital_inr=prof.reference_capital,
        available_cash_inr=Decimal("5000.00"),   # full ₹5,000 available
        deployed_exposure_inr=Decimal("0.00"),
        open_positions_Symbols=set(),
        daily_realized_pnl_inr=Decimal("0.00"),
        daily_unrealized_pnl_inr=Decimal("0.00"),
    )

    assert decision.approved, (
        f"Expected trade approval for TATASTEEL@₹150 with ₹5,000 capital. "
        f"Reason: {decision.reason_code} — {decision.explanation}"
    )
    assert decision.approved_quantity >= 1, "Must approve at least 1 share"
    # With ₹4,000 max deployment at ₹150/share → up to 26 shares
    assert decision.approved_quantity <= 27, (
        f"Should not approve more than 26-27 shares at ₹150 within ₹4,000 budget, "
        f"got {decision.approved_quantity}"
    )


# ---------------------------------------------------------------------------
# Test 6: Risk engine rejects when daily loss limit reached (5% = ₹250)
# ---------------------------------------------------------------------------

def test_nano_5k_risk_engine_rejects_on_daily_loss_limit():
    """Trade must be rejected when daily realized PnL has already hit -₹250 (-5%)."""
    cfg, prof = _get_nano_profile()
    cost_eng = NSECostEngine(cfg.costs[cfg.active_cost_profile])
    risk_eng = RiskEngine(prof, cost_eng)

    # Simulate a day where ₹250 (5% of ₹5,000) has already been lost
    decision = risk_eng.evaluate_entry_signal(
        symbol="TATASTEEL",
        session_date="2026-11-14",
        signal_timestamp_utc=ist_datetime(2026, 11, 14, 11, 00),
        candidate_entry_price=Decimal("150.00"),
        orb_low=Decimal("148.00"),
        current_capital_inr=prof.reference_capital,
        available_cash_inr=Decimal("4750.00"),   # ₹250 spent/lost
        deployed_exposure_inr=Decimal("0.00"),
        open_positions_Symbols=set(),
        daily_realized_pnl_inr=Decimal("-250.00"),  # max daily loss hit
        daily_unrealized_pnl_inr=Decimal("0.00"),
    )

    assert not decision.approved, (
        "Must reject new entries when daily loss limit of ₹250 (5%) has been reached"
    )


# ---------------------------------------------------------------------------
# Test 7: ScheduledDailyRunner selects low-price symbols for NANO_5K
# ---------------------------------------------------------------------------

def test_nano_5k_runner_uses_low_price_universe(monkeypatch=None):
    """ScheduledDailyRunner must auto-select NANO_5K low-price symbols universe."""
    # Set the profile env var so load_app_config picks NANO_5K.
    # monkeypatch is passed by _run_with_monkeypatch but we manage env manually
    # here to ensure cleanup works with either pytest or the custom runner.
    orig = os.environ.get("ALPHATRADE_CAPITAL_PROFILE")
    os.environ["ALPHATRADE_CAPITAL_PROFILE"] = "NANO_5K"
    try:
        # Import inline to ensure env is applied during runner init
        from app.services.daily_runner import ScheduledDailyRunner
        runner = ScheduledDailyRunner()
        universe = runner.symbols_universe
        # Must contain affordable low-price stocks
        assert "TATASTEEL" in universe, "TATASTEEL must be in NANO_5K universe"
        assert "NTPC" in universe, "NTPC must be in NANO_5K universe"
        assert "POWERGRID" in universe, "POWERGRID must be in NANO_5K universe"
        # Must NOT contain high-priced large-cap stocks (default universe)
        assert "RELIANCE" not in universe, "RELIANCE (~₹1,250) must NOT be in NANO_5K universe"
        assert "TCS" not in universe, "TCS (~₹4,100) must NOT be in NANO_5K universe"
        runner.stop()
    finally:
        if orig is None:
            os.environ.pop("ALPHATRADE_CAPITAL_PROFILE", None)
        else:
            os.environ["ALPHATRADE_CAPITAL_PROFILE"] = orig


# ---------------------------------------------------------------------------
# Test 8: Profile key name exists in strategy config (regression guard)
# ---------------------------------------------------------------------------

def test_nano_5k_profile_key_in_strategy_config():
    """'NANO_5K' key must be present in strategy.risk_profiles dict."""
    cfg, _ = _get_nano_profile()
    assert "NANO_5K" in cfg.strategy.risk_profiles, (
        "NANO_5K profile missing from orb_v2.json risk_profiles"
    )
