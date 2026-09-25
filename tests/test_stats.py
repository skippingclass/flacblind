"""Binomial statistics: exact values, edge cases and the verdict thresholds."""

from __future__ import annotations

import math
import unittest
from fractions import Fraction

from helpers import ROOT  # noqa: F401  (path bootstrap)

from flacblind.core.stats import (
    SIGNIFICANCE,
    analyze,
    binomial_pmf,
    chance_of_at_least,
    expected_score,
    fmt_p_value,
    min_rounds_for_significance,
    p_value_greater,
    p_value_two_sided,
    required_correct,
    score_histogram,
    verdict,
    wilson_interval,
    z_score,
)


def exact_tail(n: int, k: int) -> Fraction:
    """P(X >= k) with exact rational arithmetic, for cross-checking."""
    return sum(Fraction(math.comb(n, i), 2**n) for i in range(k, n + 1))


class TestPValues(unittest.TestCase):
    def test_matches_exact_rational_arithmetic(self) -> None:
        for n in (1, 2, 5, 10, 20, 50, 100, 500):
            for k in {0, n // 4, n // 2, (3 * n) // 4, n}:
                self.assertAlmostEqual(
                    p_value_greater(n, k), float(exact_tail(n, k)), places=12,
                    msg=f"n={n} k={k}",
                )

    def test_known_abx_values(self) -> None:
        # The classic ABX table: 9/10 is significant, 8/10 is not.
        self.assertAlmostEqual(p_value_greater(10, 9), 11 / 1024, places=12)
        self.assertAlmostEqual(p_value_greater(10, 10), 1 / 1024, places=12)
        self.assertAlmostEqual(p_value_greater(10, 8), 56 / 1024, places=12)
        self.assertAlmostEqual(p_value_greater(20, 15), 21700 / 2**20, places=9)

    def test_symmetry(self) -> None:
        """By symmetry of the fair coin, P(X>=k) + P(X>=n-k+1) == 1."""
        for n in (4, 9, 10, 21):
            for k in range(1, n + 1):
                self.assertAlmostEqual(
                    p_value_greater(n, k) + p_value_greater(n, n - k + 1), 1.0, places=12
                )
            self.assertAlmostEqual(p_value_greater(n, 0), 1.0, places=12)

    def test_monotonic_decreasing(self) -> None:
        values = [p_value_greater(20, k) for k in range(21)]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_degenerate_inputs(self) -> None:
        self.assertEqual(p_value_greater(0, 0), 1.0)
        self.assertEqual(chance_of_at_least(0, 0), 1.0)
        self.assertEqual(p_value_greater(5, 99), p_value_greater(5, 5))
        self.assertEqual(p_value_greater(5, -3), 1.0)

    def test_two_sided_is_symmetric_about_chance(self) -> None:
        for n in (6, 10, 15):
            for k in range(n + 1):
                self.assertAlmostEqual(
                    p_value_two_sided(n, k), p_value_two_sided(n, n - k), places=12
                )
        # A coin flipper's exact 50 % has a two-sided p of 1.
        self.assertAlmostEqual(p_value_two_sided(10, 5), 1.0, places=12)

    def test_binomial_pmf_sums_to_one(self) -> None:
        for n in (0, 1, 7, 30):
            self.assertAlmostEqual(sum(binomial_pmf(n, k) for k in range(n + 1)), 1.0, places=10)

    def test_large_n_does_not_overflow(self) -> None:
        # A naive implementation overflows a float well before this.
        value = p_value_greater(4000, 2200)
        self.assertTrue(0.0 <= value <= 1.0)
        self.assertLess(value, 0.5)


class TestThresholds(unittest.TestCase):
    def test_required_correct(self) -> None:
        self.assertEqual(required_correct(10), 9)
        self.assertEqual(required_correct(5), 5)
        self.assertEqual(required_correct(20), 15)
        self.assertEqual(required_correct(2), 3)  # 2/2 is p=0.25: impossible

    def test_required_correct_impossible_case(self) -> None:
        # Three rounds cannot reach 95 % confidence, not even with 3/3.
        self.assertEqual(required_correct(3), 4)
        self.assertGreater(p_value_greater(3, 3), SIGNIFICANCE)

    def test_min_rounds_for_significance(self) -> None:
        self.assertEqual(min_rounds_for_significance(), 5)
        self.assertLess(p_value_greater(5, 5), SIGNIFICANCE)
        self.assertGreater(p_value_greater(4, 4), SIGNIFICANCE)

    def test_expected_score_and_z(self) -> None:
        self.assertEqual(expected_score(10), 5.0)
        self.assertAlmostEqual(z_score(10, 10), 2.85, places=2)  # continuity corrected
        self.assertLess(z_score(10, 2), 0.0)

    def test_wilson_interval(self) -> None:
        low, high = wilson_interval(9, 10)
        self.assertLess(low, 0.9)
        self.assertGreater(high, 0.9)
        self.assertGreater(high, 0.95)
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))
        low0, high0 = wilson_interval(0, 10)
        self.assertEqual(low0, 0.0)
        self.assertLess(high0, 0.4)

    def test_histogram_shape(self) -> None:
        values = score_histogram(10)
        self.assertEqual(len(values), 11)
        self.assertAlmostEqual(max(values), 1.0)  # normalised to the tallest bar
        self.assertEqual(values.index(max(values)), 5)  # mode is 5/10
        total = sum(values)
        for k, value in enumerate(values):
            self.assertAlmostEqual(value / total, binomial_pmf(10, k), places=9)
        self.assertEqual(score_histogram(0), [])


class TestVerdicts(unittest.TestCase):
    def test_levels(self) -> None:
        self.assertEqual(verdict(10, 10).level, "strong")
        self.assertEqual(verdict(10, 9).level, "significant")
        self.assertEqual(verdict(10, 8).level, "weak")
        self.assertEqual(verdict(10, 5).level, "none")
        self.assertEqual(verdict(0, 0).level, "none")

    def test_eight_of_ten_is_not_significant(self) -> None:
        """The point of the whole tool: 8/10 must not read as a win."""
        reading = verdict(10, 8)
        self.assertFalse(reading.level in ("significant", "strong"))
        self.assertGreater(reading.p_value, SIGNIFICANCE)

    def test_detail_text_mentions_probability(self) -> None:
        self.assertIn("%", verdict(10, 9).detail)

    def test_analyze_bundle(self) -> None:
        result = analyze(10, 9)
        self.assertEqual((result.rounds, result.correct), (10, 9))
        self.assertTrue(result.is_significant)
        self.assertEqual(result.margin, 0)
        self.assertEqual(analyze(10, 8).margin, 1)
        self.assertEqual(analyze(10, 6).margin, 3)

    def test_fmt_p_value(self) -> None:
        self.assertEqual(fmt_p_value(0.0), "< 0.000001")
        self.assertEqual(fmt_p_value(0.0107), "0.011")
        self.assertEqual(fmt_p_value(0.5), "0.5")
        self.assertEqual(fmt_p_value(1.0), "1")


if __name__ == "__main__":
    unittest.main()
