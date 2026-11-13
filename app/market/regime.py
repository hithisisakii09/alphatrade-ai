"""
M19 — Hidden Markov Model Volatility Regime Classifier
=======================================================
Pure Decimal / numpy-free implementation using a 3-state Gaussian HMM fitted
via the Baum-Welch EM algorithm on log-returns derived from OHLCV bars.

States
------
  0  → BULL   : low volatility, positive drift
  1  → NEUTRAL : moderate volatility, near-zero drift
  2  → BEAR   : high volatility, negative drift

The classifier uses a simplified (diagonal-covariance) Gaussian emission model
operating on two features per bar:
  • log_return  = log(close_t / close_{t-1})
  • norm_range  = (high - low) / close_{t-1}   (normalised intrabar range)

Baum-Welch runs for at most `max_iter` iterations or until log-likelihood
improvement falls below `tol`.  With fewer than `min_bars` observations the
classifier returns NEUTRAL with low confidence.

All intermediate computations use Python `float` (IEEE-754 double) which is
acceptable for statistical signal processing; monetary decisions downstream
still use Decimal via the existing cost/risk engines.
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public enum
# ---------------------------------------------------------------------------

class MarketRegime(str, Enum):
    BULL    = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR    = "BEAR"


# ---------------------------------------------------------------------------
# Internal HMM helpers (pure Python, no numpy/scipy)
# ---------------------------------------------------------------------------

_EPS = 1e-300          # floor to avoid log(0)
_LOG_EPS = math.log(_EPS)
_N_STATES = 3
_N_FEATURES = 2        # [log_return, norm_range]


def _log_gaussian(x: float, mu: float, sigma: float) -> float:
    """Log-probability of scalar x under N(mu, sigma^2)."""
    if sigma <= 0:
        sigma = 1e-8
    z = (x - mu) / sigma
    return -0.5 * z * z - math.log(sigma) - 0.9189385332046727  # 0.5*log(2π)


def _log_emission(obs: Tuple[float, float], means: List[List[float]],
                  stds: List[List[float]], state: int) -> float:
    """Log-emission probability for a 2-D observation given state."""
    return (_log_gaussian(obs[0], means[state][0], stds[state][0]) +
            _log_gaussian(obs[1], means[state][1], stds[state][1]))


def _log_sum_exp(log_vals: List[float]) -> float:
    """Numerically stable log-sum-exp."""
    max_v = max(log_vals)
    if max_v == float('-inf'):
        return float('-inf')
    return max_v + math.log(sum(math.exp(v - max_v) for v in log_vals) + _EPS)


def _forward_log(observations: List[Tuple[float, float]],
                 log_pi: List[float],
                 log_A: List[List[float]],
                 means: List[List[float]],
                 stds: List[List[float]]) -> Tuple[List[List[float]], float]:
    """
    Forward algorithm in log-space.
    Returns (alpha_log[T][S], log_likelihood).
    """
    T = len(observations)
    S = _N_STATES
    alpha = [[float('-inf')] * S for _ in range(T)]

    # Initialise
    for s in range(S):
        alpha[0][s] = log_pi[s] + _log_emission(observations[0], means, stds, s)

    # Recurse
    for t in range(1, T):
        em = [_log_emission(observations[t], means, stds, s) for s in range(S)]
        for s in range(S):
            alpha[t][s] = em[s] + _log_sum_exp(
                [alpha[t-1][i] + log_A[i][s] for i in range(S)]
            )

    ll = _log_sum_exp(alpha[T-1])
    return alpha, ll


def _backward_log(observations: List[Tuple[float, float]],
                  log_A: List[List[float]],
                  means: List[List[float]],
                  stds: List[List[float]]) -> List[List[float]]:
    """Backward algorithm in log-space. Returns beta_log[T][S]."""
    T = len(observations)
    S = _N_STATES
    beta = [[float('-inf')] * S for _ in range(T)]
    for s in range(S):
        beta[T-1][s] = 0.0  # log(1)

    for t in range(T-2, -1, -1):
        em_next = [_log_emission(observations[t+1], means, stds, s) for s in range(S)]
        for s in range(S):
            beta[t][s] = _log_sum_exp(
                [log_A[s][j] + em_next[j] + beta[t+1][j] for j in range(S)]
            )

    return beta


def _baum_welch_step(
    observations: List[Tuple[float, float]],
    log_pi: List[float],
    log_A: List[List[float]],
    means: List[List[float]],
    stds: List[List[float]],
) -> Tuple[List[float], List[List[float]], List[List[float]], List[List[float]], float]:
    """One E-step + M-step of Baum-Welch. Returns updated params + log-likelihood."""
    T = len(observations)
    S = _N_STATES

    alpha, ll = _forward_log(observations, log_pi, log_A, means, stds)
    beta = _backward_log(observations, log_A, means, stds)

    # --- Gamma (posterior state probabilities) ---
    gamma = [[0.0] * S for _ in range(T)]
    for t in range(T):
        log_g = [alpha[t][s] + beta[t][s] for s in range(S)]
        log_norm = _log_sum_exp(log_g)
        for s in range(S):
            gamma[t][s] = math.exp(log_g[s] - log_norm)

    # --- Xi (posterior transition probabilities) ---
    xi = [[[0.0] * S for _ in range(S)] for _ in range(T-1)]
    for t in range(T-1):
        em_next = [_log_emission(observations[t+1], means, stds, j) for j in range(S)]
        log_denom_vals = []
        for i in range(S):
            for j in range(S):
                log_denom_vals.append(alpha[t][i] + log_A[i][j] + em_next[j] + beta[t+1][j])
        log_denom = _log_sum_exp(log_denom_vals)
        for i in range(S):
            for j in range(S):
                log_xij = alpha[t][i] + log_A[i][j] + em_next[j] + beta[t+1][j]
                xi[t][i][j] = math.exp(log_xij - log_denom)

    # --- M-step ---
    # New initial distribution
    new_log_pi = [math.log(max(gamma[0][s], _EPS)) for s in range(S)]

    # New transition matrix
    new_log_A = [[float('-inf')] * S for _ in range(S)]
    for i in range(S):
        sum_gamma_i = sum(gamma[t][i] for t in range(T-1)) + _EPS
        for j in range(S):
            sum_xi_ij = sum(xi[t][i][j] for t in range(T-1)) + _EPS
            new_log_A[i][j] = math.log(sum_xi_ij / sum_gamma_i)

    # New emission parameters
    new_means = [[0.0, 0.0] for _ in range(S)]
    new_stds  = [[1.0, 1.0] for _ in range(S)]

    for s in range(S):
        wt = sum(gamma[t][s] for t in range(T)) + _EPS
        for f in range(_N_FEATURES):
            new_means[s][f] = sum(gamma[t][s] * observations[t][f] for t in range(T)) / wt
        for f in range(_N_FEATURES):
            var = (sum(gamma[t][s] * (observations[t][f] - new_means[s][f])**2
                       for t in range(T)) / wt)
            new_stds[s][f] = max(math.sqrt(var), 1e-6)

    return new_log_pi, new_log_A, new_means, new_stds, ll


def _viterbi(observations: List[Tuple[float, float]],
             log_pi: List[float],
             log_A: List[List[float]],
             means: List[List[float]],
             stds: List[List[float]]) -> List[int]:
    """Viterbi decoding. Returns most-likely state sequence."""
    T = len(observations)
    S = _N_STATES
    delta = [[float('-inf')] * S for _ in range(T)]
    psi   = [[0] * S for _ in range(T)]

    for s in range(S):
        delta[0][s] = log_pi[s] + _log_emission(observations[0], means, stds, s)

    for t in range(1, T):
        em = [_log_emission(observations[t], means, stds, s) for s in range(S)]
        for s in range(S):
            vals = [delta[t-1][i] + log_A[i][s] for i in range(S)]
            best_i = max(range(S), key=lambda i: vals[i])
            delta[t][s] = vals[best_i] + em[s]
            psi[t][s]   = best_i

    # Backtrack
    path = [0] * T
    path[T-1] = max(range(S), key=lambda s: delta[T-1][s])
    for t in range(T-2, -1, -1):
        path[t] = psi[t+1][path[t+1]]

    return path


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RegimeState:
    regime: MarketRegime
    state_index: int               # raw HMM state (0/1/2)
    confidence: float              # posterior probability [0, 1]
    bull_prob: float
    neutral_prob: float
    bear_prob: float
    n_bars_used: int
    log_likelihood: float


@dataclass
class HMMParams:
    """Fitted HMM parameters (can be serialised / cached)."""
    log_pi:  List[float]           = field(default_factory=list)
    log_A:   List[List[float]]     = field(default_factory=list)
    means:   List[List[float]]     = field(default_factory=list)
    stds:    List[List[float]]     = field(default_factory=list)
    state_map: List[MarketRegime]  = field(default_factory=list)
    trained: bool = False


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------

class VolatilityRegimeClassifier:
    """
    3-state Gaussian HMM regime classifier.

    Usage
    -----
    clf = VolatilityRegimeClassifier()
    clf.fit(closes, highs, lows)          # train on historical bars
    state = clf.predict_current()          # regime of the last bar
    """

    MIN_BARS = 20
    MAX_ITER = 60
    TOL = 1e-4

    def __init__(self, min_bars: int = MIN_BARS,
                 max_iter: int = MAX_ITER, tol: float = TOL):
        self.min_bars = min_bars
        self.max_iter = max_iter
        self.tol = tol
        self._params = HMMParams()
        self._observations: List[Tuple[float, float]] = []
        self._state_sequence: List[int] = []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_observations(closes: Sequence[float],
                            highs: Sequence[float],
                            lows: Sequence[float]) -> List[Tuple[float, float]]:
        obs = []
        for i in range(1, len(closes)):
            if closes[i-1] <= 0:
                continue
            lr = math.log(closes[i] / closes[i-1])
            nr = (highs[i] - lows[i]) / closes[i-1]
            obs.append((lr, nr))
        return obs

    def _init_params(self, observations: List[Tuple[float, float]]) -> None:
        """Initialise HMM params heuristically from data quartiles."""
        T = len(observations)
        # Sort observations by log_return to get rough regime split
        sorted_lr = sorted(obs[0] for obs in observations)
        q33 = sorted_lr[T // 3]
        q67 = sorted_lr[2 * T // 3]

        # State 0=BULL (high lr), 1=NEUTRAL, 2=BEAR (low lr)
        bull   = [o for o in observations if o[0] >= q67]
        neutral = [o for o in observations if q33 <= o[0] < q67]
        bear   = [o for o in observations if o[0] < q33]

        def _mean_std(group: List[Tuple[float, float]], feat: int
                      ) -> Tuple[float, float]:
            if not group:
                return 0.0, 0.01
            vals = [g[feat] for g in group]
            mu = sum(vals) / len(vals)
            var = sum((v - mu)**2 for v in vals) / max(len(vals), 1)
            return mu, max(math.sqrt(var), 1e-6)

        self._params.means = [
            [_mean_std(bull, 0)[0],    _mean_std(bull, 1)[0]],
            [_mean_std(neutral, 0)[0], _mean_std(neutral, 1)[0]],
            [_mean_std(bear, 0)[0],    _mean_std(bear, 1)[0]],
        ]
        self._params.stds = [
            [_mean_std(bull, 0)[1],    _mean_std(bull, 1)[1]],
            [_mean_std(neutral, 0)[1], _mean_std(neutral, 1)[1]],
            [_mean_std(bear, 0)[1],    _mean_std(bear, 1)[1]],
        ]

        # Uniform initial + slightly sticky transition
        log1_3 = math.log(1/3)
        self._params.log_pi = [log1_3, log1_3, log1_3]
        # 80% self-transition, 10% to each other state
        self._params.log_A = [
            [math.log(0.80), math.log(0.10), math.log(0.10)],
            [math.log(0.10), math.log(0.80), math.log(0.10)],
            [math.log(0.10), math.log(0.10), math.log(0.80)],
        ]

    def _map_states_to_regimes(self) -> List[MarketRegime]:
        """
        Assign HMM states → regime labels by sorting on mean log_return.
        State with highest mean lr → BULL, lowest → BEAR, middle → NEUTRAL.
        """
        means = self._params.means
        order = sorted(range(_N_STATES), key=lambda s: means[s][0])
        # order[0]=lowest lr=BEAR, order[1]=NEUTRAL, order[2]=highest=BULL
        mapping = [MarketRegime.NEUTRAL] * _N_STATES
        mapping[order[0]] = MarketRegime.BEAR
        mapping[order[1]] = MarketRegime.NEUTRAL
        mapping[order[2]] = MarketRegime.BULL
        return mapping

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit(self, closes: Sequence[float],
            highs: Sequence[float],
            lows: Sequence[float]) -> "VolatilityRegimeClassifier":
        """
        Fit the HMM via Baum-Welch EM.

        Parameters
        ----------
        closes, highs, lows : sequences of bar prices (chronological order)
        """
        obs = self._build_observations(closes, highs, lows)
        if len(obs) < self.min_bars:
            logger.warning(
                "RegimeClassifier: only %d observations (need %d); "
                "returning NEUTRAL by default.", len(obs), self.min_bars)
            self._observations = obs
            self._params.trained = False
            return self

        self._observations = obs
        self._init_params(obs)

        prev_ll = float('-inf')
        for iteration in range(self.max_iter):
            (self._params.log_pi,
             self._params.log_A,
             self._params.means,
             self._params.stds,
             ll) = _baum_welch_step(
                obs,
                self._params.log_pi,
                self._params.log_A,
                self._params.means,
                self._params.stds,
            )
            improvement = ll - prev_ll
            logger.debug("RegimeClassifier EM iter %d: ll=%.4f Δll=%.6f",
                         iteration+1, ll, improvement)
            if improvement < self.tol and iteration > 0:
                logger.info("RegimeClassifier converged after %d iterations (ll=%.4f)",
                            iteration+1, ll)
                break
            prev_ll = ll

        self._params.state_map = self._map_states_to_regimes()
        self._state_sequence = _viterbi(
            obs, self._params.log_pi, self._params.log_A,
            self._params.means, self._params.stds)
        self._params.trained = True
        return self

    def predict_current(self) -> RegimeState:
        """
        Return regime for the most recent bar.

        If the model is not yet trained (< min_bars), returns NEUTRAL
        with low confidence.
        """
        if not self._params.trained or not self._observations:
            return RegimeState(
                regime=MarketRegime.NEUTRAL,
                state_index=1,
                confidence=0.333,
                bull_prob=0.333,
                neutral_prob=0.334,
                bear_prob=0.333,
                n_bars_used=len(self._observations),
                log_likelihood=float('-inf'),
            )

        # Run forward algorithm to get posterior for the last bar
        obs = self._observations
        alpha, ll = _forward_log(
            obs, self._params.log_pi, self._params.log_A,
            self._params.means, self._params.stds)

        log_g_last = alpha[-1]
        log_norm = _log_sum_exp(log_g_last)
        posteriors = [math.exp(lg - log_norm) for lg in log_g_last]

        # Map to BULL/NEUTRAL/BEAR using state_map
        probs = {MarketRegime.BULL: 0.0, MarketRegime.NEUTRAL: 0.0, MarketRegime.BEAR: 0.0}
        for s in range(_N_STATES):
            probs[self._params.state_map[s]] += posteriors[s]

        current_regime = max(probs, key=lambda r: probs[r])
        current_state  = self._state_sequence[-1]

        return RegimeState(
            regime=current_regime,
            state_index=current_state,
            confidence=probs[current_regime],
            bull_prob=probs[MarketRegime.BULL],
            neutral_prob=probs[MarketRegime.NEUTRAL],
            bear_prob=probs[MarketRegime.BEAR],
            n_bars_used=len(obs),
            log_likelihood=ll,
        )

    def predict_sequence(self) -> List[MarketRegime]:
        """Return regime label for each fitted observation (Viterbi path)."""
        if not self._params.trained:
            return [MarketRegime.NEUTRAL] * len(self._observations)
        return [self._params.state_map[s] for s in self._state_sequence]

    def regime_distribution(self) -> dict:
        """Return fraction of bars in each regime."""
        seq = self.predict_sequence()
        n = len(seq)
        if n == 0:
            return {r.value: 0.0 for r in MarketRegime}
        counts = {r: sum(1 for s in seq if s == r) for r in MarketRegime}
        return {r.value: counts[r] / n for r in MarketRegime}

    @property
    def is_trained(self) -> bool:
        return self._params.trained

