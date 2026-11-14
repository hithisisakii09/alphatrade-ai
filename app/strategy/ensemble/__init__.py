"""Multi-Strategy Meta-Ensemble Package (Milestone M27)."""

from app.strategy.ensemble.base import (
    AbstractAlphaStrategy,
    EnsembleSignal,
    StrategyType,
)
from app.strategy.ensemble.orb_trend import ORBTrendStrategy
from app.strategy.ensemble.vwap_reversion import VWAPMeanReversionStrategy
from app.strategy.ensemble.stat_arb import PairsSpreadReversionStrategy
from app.strategy.ensemble.allocator import (
    MetaStrategyEnsemble,
    SynthesizedEnsembleOrder,
)

__all__ = [
    "AbstractAlphaStrategy",
    "EnsembleSignal",
    "StrategyType",
    "ORBTrendStrategy",
    "VWAPMeanReversionStrategy",
    "PairsSpreadReversionStrategy",
    "MetaStrategyEnsemble",
    "SynthesizedEnsembleOrder",
]
