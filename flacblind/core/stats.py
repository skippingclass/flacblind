"""Exact binomial statistics for ABX results.

A blind ABX test is a coin flip: in every round the listener has a 50 %
chance of naming the lossless sample.  "9/10 correct" therefore means nothing
on its own — what matters is how likely that outcome is *by accident*.

Everything here is computed with :mod:`math` in log space, so it stays exact
for large round counts (endless mode) without SciPy.  The headline number is
the one-sided p-value

    P(X >= correct | n, p = 0.5)

i.e. the probability of scoring at least this well if the listener had no
ability to tell the files apart.  Below 0.05 the ABX community's 95 %
confidence standard is met.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "SIGNIFICANCE",
    "STRONG_SIGNIFICANCE",
    "log_binomial_pmf",
    "binomial_pmf",
    "p_value_greater",
    "p_value_two_sided",
    "chance_of_at_least",
    "expected_score",
    "z_score",
    "wilson_interval",
    "required_correct",
    "min_rounds_for_significance",
    "verdict",
    "Verdict",
    "analyze",
    "score_histogram",
    "fmt_p_value",
]

SIGNIFICANCE = 0.05
STRONG_SIGNIFICANCE = 0.01

#: Two-tailed z value for a 95 % confidence interval.
Z_95 = 1.959963984540054


def _log_factorial(n: int) -> float:
    return math.lgamma(n + 1.0)


def log_binomial_pmf(n: int, k: int, p: float = 0.5) -> float:
    """Natural log of ``P(X = k)`` for ``X ~ Binomial(n, p)``."""
    if n < 0:
        raise ValueError("n must be >= 0")
    if k < 0 or k > n:
        return -math.inf
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    log_choose = _log_factorial(n) - _log_factorial(k) - _log_factorial(n - k)
    return log_choose + k * math.log(p) + (n - k) * math.log1p(-p)


def binomial_pmf(n: int, k: int, p: float = 0.5) -> float:
    """``P(X = k)`` for ``X ~ Binomial(n, p)``."""
    return math.exp(log_binomial_pmf(n, k, p))


def p_value_greater(n: int, k: int, p: float = 0.5) -> float:
    """One-sided p-value: probability of scoring **at least** ``k``.

    This is the number the GUI headlines — it answers "can this listener beat
    chance?" and is also the exact probability of getting this score or a
    better one by accident.
    """
    if n <= 0:
        return 1.0
    k = max(0, min(n, k))
    # Sum from the top down: the tail is short and the terms are small, so this
    # converges in a handful of iterations even for n in the thousands.
    terms = [log_binomial_pmf(n, i, p) for i in range(n, k - 1, -1)]
    top = max(terms)
    total = math.fsum(math.exp(t - top) for t in terms) * math.exp(top)
    return min(1.0, max(0.0, total))


def chance_of_at_least(n: int, k: int) -> float:
    """Alias of :func:`p_value_greater` with p = 0.5, spelled for humans."""
    return p_value_greater(n, k, 0.5)


def p_value_two_sided(n: int, k: int, p: float = 0.5) -> float:
    """Two-sided p-value using the standard "sum of equally unlikely outcomes".

    Every ``k'`` whose point probability is no larger than the observed one is
    counted.  For a fair coin this mirrors the familiar
    ``min(1, 2 * P(X >= k))`` while staying correct for asymmetric tests.
    """
    if n <= 0:
        return 1.0
    k = max(0, min(n, k))
    observed = log_binomial_pmf(n, k, p)
    total = 0.0
    for kp in range(n + 1):
        log_p = log_binomial_pmf(n, kp, p)
        if log_p <= observed + 1e-12:
            total += math.exp(log_p)
    return min(1.0, max(0.0, total))


def expected_score(n: int) -> float:
    """Score a coin-flipping listener is expected to reach."""
    return n / 2.0


def z_score(n: int, k: int, p: float = 0.5) -> float:
    """Standardised z value of the observed score (with continuity correction)."""
    if n <= 0:
        return 0.0
    mu = n * p
    sigma = math.sqrt(n * p * (1.0 - p))
    if sigma <= 0:
        return 0.0
    corrected = k - 0.5 if k > mu else k + 0.5
    return (corrected - mu) / sigma


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval — a well behaved CI for small samples."""
    if n <= 0:
        return (0.0, 1.0)
    phat = k / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    spread = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def min_rounds_for_significance(alpha: float = SIGNIFICANCE) -> int:
    """Smallest round count at which *perfect* scoring is significant."""
    n = 1
    while n < 10_000 and p_value_greater(n, n) > alpha:
        n += 1
    return n


def required_correct(n: int, alpha: float = SIGNIFICANCE) -> int:
    """Score needed on ``n`` rounds to reach ``p < alpha`` (``n + 1`` if impossible).

    ``p`` decreases monotonically with the score for a fair coin, so the
    smallest qualifying score is simply the first one that passes the bar.
    """
    if n <= 0:
        return min_rounds_for_significance(alpha)
    for k in range(0, n + 1):
        if p_value_greater(n, k) < alpha:
            return k
    return n + 1


@dataclass(frozen=True, slots=True)
class Verdict:
    """A human readable reading of a p-value."""

    level: Literal["strong", "significant", "weak", "none"]
    p_value: float
    confidence: float
    label: str
    detail: str


def verdict(n: int, k: int) -> Verdict:
    """Classify a result into the usual ABX buckets."""
    p = p_value_greater(n, k) if n > 0 else 1.0
    if n <= 0:
        return Verdict("none", 1.0, 0.0, "No rounds played", "Cast a few votes first.")
    if p < STRONG_SIGNIFICANCE:
        level: Literal["strong", "significant", "weak", "none"] = "strong"
        label = "Clearly audible"
        detail = (
            f"Scoring at least this well by pure luck has a probability of only "
            f"{p * 100:.2f} % — the difference is real and you can hear it."
        )
        confidence = 1.0 - p
    elif p < SIGNIFICANCE:
        level = "significant"
        label = "Statistically significant"
        detail = (
            f"A chance listener would beat your score in less than 1 out of 20 "
            f"tests ({p * 100:.1f} %). Real, though not overwhelming."
        )
        confidence = 1.0 - p
    elif p < 0.2:
        level = "weak"
        label = "Inconclusive"
        detail = (
            f"This score still happens {p * 100:.1f} % of the time by accident. "
            f"Play more rounds to be sure."
        )
        confidence = 1.0 - p
    else:
        level = "none"
        label = "Indistinguishable"
        detail = (
            f"A coin flip would score this well {p * 100:.0f} % of the time — "
            f"the two files are effectively identical to your ears."
        )
        confidence = 1.0 - p
    return Verdict(level, p, confidence, label, detail)


def analyze(n: int, k: int, alpha: float = SIGNIFICANCE) -> "StatsResult":
    """Bundle every number the results panel needs."""
    k = max(0, min(n, k)) if n > 0 else 0
    ci_low, ci_high = wilson_interval(k, n)
    return StatsResult(
        rounds=max(0, n),
        correct=k,
        p_value=p_value_greater(n, k) if n > 0 else 1.0,
        p_value_two_sided=p_value_two_sided(n, k) if n > 0 else 1.0,
        ci_low=ci_low,
        ci_high=ci_high,
        needed_for_significance=required_correct(n, alpha),
        z=z_score(n, k),
    )


@dataclass(frozen=True, slots=True)
class StatsResult:
    """Immutable statistics bundle (mirrors ``SessionStats`` in the UI layer)."""

    rounds: int
    correct: int
    p_value: float
    p_value_two_sided: float
    ci_low: float
    ci_high: float
    needed_for_significance: int
    z: float = 0.0

    @property
    def rate(self) -> float:
        return self.correct / self.rounds if self.rounds else 0.0

    @property
    def is_significant(self) -> bool:
        return self.rounds > 0 and self.p_value < SIGNIFICANCE

    @property
    def margin(self) -> int:
        if self.rounds <= 0 or self.is_significant:
            return 0
        return max(0, self.needed_for_significance - self.correct)


def score_histogram(n: int, observed: int | None = None) -> list[float]:
    """Null distribution of scores for ``n`` rounds, tallest value scaled to 1."""
    if n <= 0:
        return []
    logs = [log_binomial_pmf(n, k) for k in range(n + 1)]
    top = max(logs)
    values = [math.exp(v - top) for v in logs]
    return values


def fmt_p_value(p: float) -> str:
    """Format a p-value the way a scientist wants to read it."""
    if p <= 0:
        return "< 0.000001"

    def _trim(digits: int) -> str:
        return f"{p:.{digits}f}".rstrip("0").rstrip(".")

    if p < 0.000001:
        return f"{p:.2e}"
    if p < 0.001:
        return _trim(5)
    if p < 0.01:
        return _trim(4)
    return _trim(3)
