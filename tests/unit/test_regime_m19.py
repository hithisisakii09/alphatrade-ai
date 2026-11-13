"""
M19 — HMM Volatility Regime Classifier Tests
=============================================
6 deterministic tests:
  1. Regime classifier trains and returns valid state on bull sequence
  2. Bear regime detected when prices consistently decline
  3. Neutral/untrained fallback when < min_bars
  4. Regime sequence length matches observations
  5. Strategy engine rejects signal when regime is BEAR and filter enabled
  6. API endpoint returns regime JSON for in-memory data
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from decimal import Decimal
from app.market.regime import VolatilityRegimeClassifier, MarketRegime, RegimeState


# ---------------------------------------------------------------------------
# Helper: generate synthetic price series
# ---------------------------------------------------------------------------

def _trending_prices(n: int, start: float = 1000.0, drift: float = 0.002,
                     noise: float = 0.001, seed: int = 42) -> tuple:
    """Generate (closes, highs, lows) with deterministic pseudo-random noise."""
    import random
    rng = random.Random(seed)
    closes = [start]
    for i in range(1, n):
        ret = drift + noise * (rng.random() * 2 - 1)
        closes.append(closes[-1] * (1 + ret))
    highs = [c * (1 + abs(noise)) for c in closes]
    lows  = [c * (1 - abs(noise)) for c in closes]
    return closes, highs, lows


# ---------------------------------------------------------------------------
# Test 1: Bull regime detection on ascending prices
# ---------------------------------------------------------------------------

def test_bull_regime_detected_on_rising_prices():
    """Classifier should return BULL or NEUTRAL (not BEAR) on a consistently rising series."""
    closes, highs, lows = _trending_prices(80, drift=0.003, noise=0.0005, seed=1)

    clf = VolatilityRegimeClassifier(min_bars=20, max_iter=40)
    clf.fit(closes, highs, lows)

    assert clf.is_trained, "Model should be trained with 80 bars"

    state = clf.predict_current()
    assert state.regime in (MarketRegime.BULL, MarketRegime.NEUTRAL), (
        f"Expected BULL or NEUTRAL on rising series, got {state.regime.value}"
    )
    assert 0.0 < state.confidence <= 1.0, "Confidence must be in (0, 1]"
    assert abs(state.bull_prob + state.neutral_prob + state.bear_prob - 1.0) < 1e-6, (
        "Posterior probabilities must sum to 1"
    )
    assert state.log_likelihood != float("-inf"), "Log-likelihood must be finite"
    assert state.n_bars_used == len(closes) - 1

    print(f"[PASS] Bull regime: {state.regime.value} @ {state.confidence:.1%} confidence")


# ---------------------------------------------------------------------------
# Test 2: Bear regime on declining prices
# ---------------------------------------------------------------------------

def test_bear_regime_detected_on_falling_prices():
    """Classifier should return BEAR or NEUTRAL on a consistently falling series."""
    closes, highs, lows = _trending_prices(80, drift=-0.004, noise=0.0008, seed=2)

    clf = VolatilityRegimeClassifier(min_bars=20, max_iter=40)
    clf.fit(closes, highs, lows)

    assert clf.is_trained

    state = clf.predict_current()
    assert state.regime in (MarketRegime.BEAR, MarketRegime.NEUTRAL), (
        f"Expected BEAR or NEUTRAL on falling series, got {state.regime.value}"
    )
    print(f"[PASS] Bear regime: {state.regime.value} @ {state.confidence:.1%} confidence")


# ---------------------------------------------------------------------------
# Test 3: Fallback to NEUTRAL when insufficient bars
# ---------------------------------------------------------------------------

def test_insufficient_bars_returns_neutral_fallback():
    """With < min_bars observations the classifier must return NEUTRAL and low confidence."""
    closes, highs, lows = _trending_prices(10, drift=0.001, seed=3)

    clf = VolatilityRegimeClassifier(min_bars=20)
    clf.fit(closes, highs, lows)

    assert not clf.is_trained, "Should NOT be trained with only 10 bars"

    state = clf.predict_current()
    assert state.regime == MarketRegime.NEUTRAL, (
        f"Expected NEUTRAL fallback, got {state.regime.value}"
    )
    assert state.confidence < 0.5, "Confidence should be low for untrained model"
    print(f"[PASS] Insufficient bars → NEUTRAL (confidence={state.confidence:.3f})")


# ---------------------------------------------------------------------------
# Test 4: Viterbi sequence length integrity
# ---------------------------------------------------------------------------

def test_regime_sequence_length_matches_observations():
    """Viterbi path length must equal number of valid observations (n_bars - 1)."""
    closes, highs, lows = _trending_prices(50, drift=0.001, noise=0.001, seed=4)

    clf = VolatilityRegimeClassifier(min_bars=20, max_iter=30)
    clf.fit(closes, highs, lows)

    assert clf.is_trained

    seq = clf.predict_sequence()
    expected_len = len(closes) - 1
    assert len(seq) == expected_len, (
        f"Sequence length {len(seq)} != expected {expected_len}"
    )
    for r in seq:
        assert isinstance(r, MarketRegime), f"Expected MarketRegime, got {type(r)}"

    dist = clf.regime_distribution()
    assert set(dist.keys()) == {"BULL", "NEUTRAL", "BEAR"}
    assert abs(sum(dist.values()) - 1.0) < 1e-6, "Regime fractions must sum to 1"

    print(f"[PASS] Sequence length {len(seq)}, distribution: {dist}")


# ---------------------------------------------------------------------------
# Test 5: ORB engine rejects signal when regime is BEAR and filter enabled
# ---------------------------------------------------------------------------

def test_orb_engine_rejects_on_adverse_regime():
    """ORB Check 13: when require_regime_filter=True and regime=BEAR → ADVERSE_REGIME rejection."""
    from app.core.clock import ist_datetime
    from app.core.config import load_app_config
    from app.data.instruments import InstrumentRegistry
    from app.data.models import CandleBar
    from app.market.indicators import OpeningRangeState
    from app.market.regime import RegimeState, MarketRegime
    from app.strategy.orb.engine import ORBStrategyEngine

    app_cfg = load_app_config()
    cfg = app_cfg.strategy

    # Enable regime filter — direct attribute mutation (matches M16 test pattern)
    cfg.require_regime_filter = True
    cfg.allowed_regimes = ["BULL", "NEUTRAL"]

    registry = InstrumentRegistry()
    engine = ORBStrategyEngine(config=cfg, registry=registry)

    # 04:30 UTC = 10:00 IST (well before 14:30 cutoff)
    bar = CandleBar(
        symbol="RELIANCE",
        interval_minutes=5,
        timestamp_utc=ist_datetime(2026, 11, 12, 10, 0),
        open=Decimal("1350"),
        high=Decimal("1360"),
        low=Decimal("1348"),
        close=Decimal("1355"),
        volume=100000,
        source="TEST",
    )
    orb_state = OpeningRangeState(
        symbol="RELIANCE",
        session_date="2026-11-12",
        orb_high=Decimal("1348"),
        orb_low=Decimal("1330"),
        orb_volume=500000,
        is_complete=True,
    )
    # Adverse BEAR regime with high confidence
    bear_regime = RegimeState(
        regime=MarketRegime.BEAR,
        state_index=2,
        confidence=0.85,
        bull_prob=0.07,
        neutral_prob=0.08,
        bear_prob=0.85,
        n_bars_used=50,
        log_likelihood=-120.5,
    )

    result = engine.evaluate_5m_bar(
        bar_5m=bar,
        orb_state=orb_state,
        vwap=Decimal("1340"),
        rvol=Decimal("2.1"),
        estimated_spread_pct=Decimal("0.0010"),
        prev_daily_close=Decimal("1320"),
        nifty_strongly_bearish=False,
        regime_state=bear_regime,
    )

    # Restore config defaults
    cfg.require_regime_filter = False

    assert not result.triggered, "Signal should be rejected in BEAR regime"
    assert result.rejection_reason == "ADVERSE_REGIME", (
        f"Expected ADVERSE_REGIME, got {result.rejection_reason}"
    )
    assert "BEAR" in result.explanation
    print(f"[PASS] ADVERSE_REGIME gate triggered: {result.explanation}")


# ---------------------------------------------------------------------------
# Test 6: API endpoint returns regime JSON for in-memory data
# ---------------------------------------------------------------------------

def test_api_regime_endpoint():
    """GET /market/regime should return valid JSON structure even with no DB data."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    # Use a symbol that won't be in DB — tests fallback / empty-data path
    resp = client.get("/market/regime?symbol=TESTNODATA_XYZ&lookback_bars=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "TESTNODATA_XYZ"
    assert body["regime"] in ("BULL", "NEUTRAL", "BEAR")
    assert "confidence" in body
    assert isinstance(body["confidence"], float)
    assert "bull_prob" in body
    assert "neutral_prob" in body
    assert "bear_prob" in body
    assert "regime_distribution" in body
    dist = body["regime_distribution"]
    assert isinstance(dist, dict)
    assert set(dist.keys()) == {"BULL", "NEUTRAL", "BEAR"}

    print(f"[PASS] API /market/regime → {body['regime']} (confidence={body['confidence']})")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_bull_regime_detected_on_rising_prices,
        test_bear_regime_detected_on_falling_prices,
        test_insufficient_bars_returns_neutral_fallback,
        test_regime_sequence_length_matches_observations,
        test_orb_engine_rejects_on_adverse_regime,
        test_api_regime_endpoint,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            import traceback; traceback.print_exc()
            sys.exit(1)
    print("\nAll M19 regime tests passed.")
