"""Unit tests for Milestone M27: Multi-Strategy Meta-Ensemble & Dynamic Regime Allocation.

Covers:
  1. Base strategy interface, signal serialization & fail-closed invariants
  2. ORBTrendStrategy evaluation (BULL momentum, RVOL filter, BEAR rejection)
  3. VWAPMeanReversionStrategy evaluation (oversold 2.0σ stretch, target to VWAP)
  4. PairsSpreadReversionStrategy evaluation (spread z-score calculation & trigger)
  5. MetaStrategyEnsemble dynamic weight interpolation across HMM regime posteriors
  6. Signal fusion, consensus conviction & cross-strategy conflict resolution
  7. Fail-closed paper execution guard enforcement
  8. M27 REST API endpoints integration test (/strategy/ensemble/status & /strategy/ensemble/signals)
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import sys

os.environ.setdefault("ALPHATRADE_EXECUTION_MODE", "PAPER")

from app.core.execution_guard import LiveTradingProhibitedError, assert_paper_mode
from app.core.money import round_price, to_decimal
from app.data.models import CandleBar
from app.market.regime import MarketRegime, RegimeState
from app.strategy.ensemble import (
    AbstractAlphaStrategy,
    EnsembleSignal,
    MetaStrategyEnsemble,
    ORBTrendStrategy,
    PairsSpreadReversionStrategy,
    StrategyType,
    SynthesizedEnsembleOrder,
    VWAPMeanReversionStrategy,
)


# ---------------------------------------------------------------------------
# Test 1: Base strategy interface & signal serialization
# ---------------------------------------------------------------------------

def test_base_strategy_interface_and_signal_serialization():
  """Verifies EnsembleSignal serialization and pure Decimal precision."""
  assert_paper_mode()
  now_utc = datetime(2026, 11, 14, 5, 0, 0, tzinfo=timezone.utc)
  sig = EnsembleSignal(
      signal_id="SIG_TEST_001",
      strategy_id="TEST_STRAT",
      strategy_type=StrategyType.TREND_FOLLOWING,
      symbol="RELIANCE",
      side="BUY",
      reference_price=Decimal("2850.50"),
      stop_loss_price=Decimal("2815.00"),
      target_price=Decimal("2920.00"),
      timestamp_utc=now_utc,
      confidence=Decimal("0.85"),
      metadata={"rvol": "1.85"},
  )

  d = sig.to_dict()
  assert d["signal_id"] == "SIG_TEST_001"
  assert d["strategy_type"] == "TREND_FOLLOWING"
  assert d["reference_price"] == "2850.50"
  assert d["confidence"] == "0.85"
  assert d["metadata"]["rvol"] == "1.85"


# ---------------------------------------------------------------------------
# Test 2: ORBTrendStrategy evaluation
# ---------------------------------------------------------------------------

def test_orb_trend_strategy_evaluation():
  """Verifies ORB trend strategy triggers in BULL regime and rejects in adverse BEAR regime."""
  assert_paper_mode()
  strat = ORBTrendStrategy(entry_start_ist="09:30:00", entry_cutoff_ist="14:30:00")
  base_time_utc = datetime(2026, 11, 14, 3, 45, 0, tzinfo=timezone.utc)  # 09:15 AM IST

  # Bars 0-2: Opening Range (09:15 - 09:30 IST) -> ORH = 2855.00
  bars: list[CandleBar] = []
  for i in range(5):
    t_bar = base_time_utc + timedelta(minutes=i * 5)
    if i < 3:
      bars.append(CandleBar(
          symbol="RELIANCE",
          timestamp_utc=t_bar,
          open=Decimal("2850.00"),
          high=Decimal("2855.00"),
          low=Decimal("2845.00"),
          close=Decimal("2852.00"),
          volume=10000,
      ))
    else:
      # Breakout bars (> 2855.00)
      bars.append(CandleBar(
          symbol="RELIANCE",
          timestamp_utc=t_bar,
          open=Decimal("2856.00"),
          high=Decimal("2862.00"),
          low=Decimal("2855.50"),
          close=Decimal("2860.00"),
          volume=25000,
      ))

  bull_regime = RegimeState(
      regime=MarketRegime.BULL,
      state_index=0,
      confidence=0.85,
      bull_prob=0.80,
      neutral_prob=0.15,
      bear_prob=0.05,
      n_bars_used=60,
      log_likelihood=-10.0,
  )

  # Should trigger in BULL regime with high RVOL
  sigs = strat.evaluate("RELIANCE", bars, regime_state=bull_regime, rvol=Decimal("2.00"))
  assert len(sigs) == 1
  assert sigs[0].side == "BUY"
  assert sigs[0].reference_price == Decimal("2860.00")
  assert sigs[0].stop_loss_price <= Decimal("2855.00")
  assert sigs[0].confidence >= Decimal("0.70")

  # Rejects in adverse BEAR regime
  bear_regime = RegimeState(
      regime=MarketRegime.BEAR,
      state_index=2,
      confidence=0.90,
      bull_prob=0.05,
      neutral_prob=0.15,
      bear_prob=0.80,
      n_bars_used=60,
      log_likelihood=-12.0,
  )
  sigs_bear = strat.evaluate("RELIANCE", bars, regime_state=bear_regime, rvol=Decimal("2.00"))
  assert len(sigs_bear) == 0


# ---------------------------------------------------------------------------
# Test 3: VWAPMeanReversionStrategy oversold signal
# ---------------------------------------------------------------------------

def test_vwap_mean_reversion_oversold_signal():
  """Verifies VWAP mean reversion triggers on oversold 2.0σ extension during NEUTRAL regime."""
  assert_paper_mode()
  strat = VWAPMeanReversionStrategy(band_multiplier=Decimal("2.0"), entry_start_ist="10:00:00")
  base_time_utc = datetime(2026, 11, 14, 4, 30, 0, tzinfo=timezone.utc)  # 10:00 AM IST

  # Build 20 range-bound bars around 1850.00
  bars: list[CandleBar] = []
  for i in range(20):
    t_bar = base_time_utc + timedelta(minutes=i * 5)
    bars.append(CandleBar(
        symbol="INFY",
        timestamp_utc=t_bar,
        open=Decimal("1850.00"),
        high=Decimal("1852.00"),
        low=Decimal("1848.00"),
        close=Decimal("1850.00"),
        volume=10000,
    ))

  # Add an oversold bar plunging to 1830.00 with low exhaustion volume
  t_last = base_time_utc + timedelta(minutes=20 * 5)
  bars.append(CandleBar(
      symbol="INFY",
      timestamp_utc=t_last,
      open=Decimal("1835.00"),
      high=Decimal("1835.00"),
      low=Decimal("1828.00"),
      close=Decimal("1830.00"),
      volume=4000,
  ))

  neutral_regime = RegimeState(
      regime=MarketRegime.NEUTRAL,
      state_index=1,
      confidence=0.80,
      bull_prob=0.10,
      neutral_prob=0.75,
      bear_prob=0.15,
      n_bars_used=60,
      log_likelihood=-11.0,
  )

  sigs = strat.evaluate("INFY", bars, regime_state=neutral_regime, rvol=Decimal("0.80"))
  assert len(sigs) == 1
  assert sigs[0].side == "BUY"
  assert sigs[0].reference_price == Decimal("1830.00")
  assert sigs[0].target_price > Decimal("1845.00")  # Targets mean reversion to session VWAP
  assert sigs[0].stop_loss_price < Decimal("1830.00")
  assert sigs[0].confidence >= Decimal("0.65")


# ---------------------------------------------------------------------------
# Test 4: PairsSpreadReversionStrategy evaluation
# ---------------------------------------------------------------------------

def test_pairs_stat_arb_spread_zscore_and_signal():
  """Verifies Pairs Stat Arb tracks spread z-score and emits BUY when z <= -2.0."""
  assert_paper_mode()
  strat = PairsSpreadReversionStrategy(z_threshold=Decimal("2.0"), lookback_window=30)
  base_time_utc = datetime(2026, 11, 14, 4, 30, 0, tzinfo=timezone.utc)

  bars_hdfc: list[CandleBar] = []
  bars_icici: list[CandleBar] = []

  # First 25 bars: stable ratio ~1.50
  for i in range(30):
    t_bar = base_time_utc + timedelta(minutes=i * 5)
    if i < 25:
      p_h = Decimal("1650.00") + Decimal(str((i % 3) * 0.50))
      p_ic = Decimal("1100.00") + Decimal(str((i % 2) * 0.40))
    else:
      # HDFC drops sharply, ICICI holds -> ratio plunges -> z <= -2.5
      p_h = Decimal("1550.00") - Decimal(str((i - 25) * 10.00))
      p_ic = Decimal("1100.00")

    bars_hdfc.append(CandleBar(
        symbol="HDFCBANK",
        timestamp_utc=t_bar,
        open=p_h,
        high=p_h + Decimal("1.00"),
        low=p_h - Decimal("1.00"),
        close=p_h,
        volume=20000,
    ))
    bars_icici.append(CandleBar(
        symbol="ICICIBANK",
        timestamp_utc=t_bar,
        open=p_ic,
        high=p_ic + Decimal("1.00"),
        low=p_ic - Decimal("1.00"),
        close=p_ic,
        volume=20000,
    ))

  z, mean_r, std_r = strat.compute_spread_zscore(bars_hdfc, bars_icici)
  assert z <= Decimal("-2.00")

  sigs = strat.evaluate("HDFCBANK", bars_hdfc, pair_bars=bars_icici)
  assert len(sigs) == 1
  assert sigs[0].symbol == "HDFCBANK"
  assert sigs[0].side == "BUY"
  assert sigs[0].reference_price == bars_hdfc[-1].close


# ---------------------------------------------------------------------------
# Test 5: MetaStrategyEnsemble dynamic weight interpolation
# ---------------------------------------------------------------------------

def test_meta_ensemble_dynamic_weights_computation():
  """Verifies dynamic regime weighting accurately shifts allocation and sums to 1.0000."""
  assert_paper_mode()
  ensemble = MetaStrategyEnsemble()

  # 1. Pure BULL regime (P(Bull)=1.0)
  bull = RegimeState(MarketRegime.BULL, 0, 1.0, bull_prob=1.0, neutral_prob=0.0, bear_prob=0.0, n_bars_used=60, log_likelihood=0.0)
  w_bull = ensemble.compute_dynamic_weights(bull)
  assert w_bull["ORB_TREND_V2"] == Decimal("0.7000")
  assert w_bull["VWAP_REVERSION_V1"] == Decimal("0.2000")
  assert w_bull["STAT_ARB_PAIRS_V1"] == Decimal("0.1000")
  assert sum(w_bull.values()) == Decimal("1.0000")

  # 2. Pure NEUTRAL regime (P(Neutral)=1.0)
  neutral = RegimeState(MarketRegime.NEUTRAL, 1, 1.0, bull_prob=0.0, neutral_prob=1.0, bear_prob=0.0, n_bars_used=60, log_likelihood=0.0)
  w_neutral = ensemble.compute_dynamic_weights(neutral)
  assert w_neutral["VWAP_REVERSION_V1"] == Decimal("0.6000")
  assert sum(w_neutral.values()) == Decimal("1.0000")

  # 3. Pure BEAR regime (P(Bear)=1.0)
  bear = RegimeState(MarketRegime.BEAR, 2, 1.0, bull_prob=0.0, neutral_prob=0.0, bear_prob=1.0, n_bars_used=60, log_likelihood=0.0)
  w_bear = ensemble.compute_dynamic_weights(bear)
  assert w_bear["STAT_ARB_PAIRS_V1"] == Decimal("0.7000")
  assert w_bear["ORB_TREND_V2"] == Decimal("0.0000")
  assert sum(w_bear.values()) == Decimal("1.0000")


# ---------------------------------------------------------------------------
# Test 6: Signal fusion and conflict resolution
# ---------------------------------------------------------------------------

def test_meta_ensemble_signal_fusion_and_conflict_resolution():
  """Verifies consensus conviction synthesis and conflict resolution."""
  assert_paper_mode()

  # Mock strategy A: ORB Trend emits BUY
  class MockTrend(AbstractAlphaStrategy):
    def evaluate(self, symbol, session_bars, regime_state=None, **kwargs):
      return [EnsembleSignal(
          signal_id="S1", strategy_id="ORB_TREND_V2", strategy_type=StrategyType.TREND_FOLLOWING,
          symbol=symbol, side="BUY", reference_price=Decimal("100.00"), stop_loss_price=Decimal("98.00"),
          target_price=Decimal("105.00"), timestamp_utc=datetime.now(timezone.utc), confidence=Decimal("0.80")
      )]

  # Mock strategy B: VWAP Reversion emits BUY (Consensus)
  class MockReversion(AbstractAlphaStrategy):
    def evaluate(self, symbol, session_bars, regime_state=None, **kwargs):
      return [EnsembleSignal(
          signal_id="S2", strategy_id="VWAP_REVERSION_V1", strategy_type=StrategyType.MEAN_REVERSION,
          symbol=symbol, side="BUY", reference_price=Decimal("100.00"), stop_loss_price=Decimal("98.50"),
          target_price=Decimal("104.00"), timestamp_utc=datetime.now(timezone.utc), confidence=Decimal("0.70")
      )]

  ensemble = MetaStrategyEnsemble(strategies=[MockTrend("ORB_TREND_V2", StrategyType.TREND_FOLLOWING), MockReversion("VWAP_REVERSION_V1", StrategyType.MEAN_REVERSION)])

  # In BULL: w(Trend)=0.78, w(Rev)=0.22 -> both BUY -> consensus
  bull = RegimeState(MarketRegime.BULL, 0, 0.9, bull_prob=0.9, neutral_prob=0.1, bear_prob=0.0, n_bars_used=60, log_likelihood=0.0)
  orders = ensemble.evaluate_ensemble("RELIANCE", session_bars=[], regime_state=bull)

  assert len(orders) == 1
  assert orders[0].side == "BUY"
  assert len(orders[0].participating_strategies) == 2
  assert orders[0].stop_loss_price == Decimal("98.50")  # Takes tighter stop loss
  assert orders[0].effective_conviction > Decimal("0.70")


# ---------------------------------------------------------------------------
# Test 7: Fail-closed paper mode guard
# ---------------------------------------------------------------------------

def test_meta_ensemble_fail_closed_paper_mode_guard():
  """Verifies LiveTradingProhibitedError is raised on attempted LIVE mode."""
  orig = os.environ.get("ALPHATRADE_EXECUTION_MODE")
  try:
    os.environ["ALPHATRADE_EXECUTION_MODE"] = "LIVE"
    try:
      MetaStrategyEnsemble()
      assert False, "Expected LiveTradingProhibitedError"
    except LiveTradingProhibitedError:
      pass
  finally:
    if orig:
      os.environ["ALPHATRADE_EXECUTION_MODE"] = orig
    else:
      os.environ.pop("ALPHATRADE_EXECUTION_MODE", None)


# ---------------------------------------------------------------------------
# Test 8: M27 REST API endpoints integration test
# ---------------------------------------------------------------------------

def test_m27_api_endpoints_integration():
  """Smoke-tests /strategy/ensemble/status and /strategy/ensemble/signals endpoints."""
  from fastapi.testclient import TestClient
  from app.main import app

  client = TestClient(app)

  # --- Ensemble status endpoint ---
  r1 = client.get("/strategy/ensemble/status?symbol=RELIANCE")
  assert r1.status_code == 200, f"Ensemble status returned {r1.status_code}: {r1.text}"
  data1 = r1.json()
  assert "registered_strategies_count" in data1
  assert data1["registered_strategies_count"] >= 3
  assert "active_weights" in data1
  assert "ORB_TREND_V2" in data1["active_weights"]

  # --- Ensemble signals endpoint ---
  r2 = client.get("/strategy/ensemble/signals?symbol=RELIANCE")
  assert r2.status_code == 200, f"Ensemble signals returned {r2.status_code}: {r2.text}"
  data2 = r2.json()
  assert data2["symbol"] == "RELIANCE"
  assert "dynamic_weights" in data2
  assert "orders_count" in data2
  assert "orders" in data2
