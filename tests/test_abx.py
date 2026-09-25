"""The ABX session state machine."""

from __future__ import annotations

import unittest

from helpers import ROOT  # noqa: F401

from flacblind.core.abx import ABXSession, RoundState, TestConfig
from flacblind.core.errors import SessionError
from flacblind.core.models import SampleKey
from flacblind.core.stats import p_value_greater


class TestSession(unittest.TestCase):
    def test_finite_session_flow(self) -> None:
        session = ABXSession(TestConfig(rounds=3, seed=1))
        first = session.start()
        self.assertEqual(first.index, 1)
        self.assertEqual(session.round_number, 1)
        self.assertFalse(session.finished)

        session.mark_played(SampleKey.A)
        session.mark_played(SampleKey.B)
        self.assertEqual(first.state, RoundState.LISTENING)
        allowed, reason = session.can_vote()
        self.assertTrue(allowed, reason)

        record = session.vote(first.lossless_key)
        self.assertTrue(record.correct)
        self.assertEqual(record.index, 1)
        self.assertEqual(first.state, RoundState.REVEALED)
        self.assertFalse(session.finished)

        session.next_round()
        self.assertEqual(session.current.index, 2)
        session.vote(session.current.lossless_key.other)
        self.assertEqual(len(session.records), 2)
        self.assertTrue(session.records[1].correct is False)

        session.next_round()
        session.vote(session.current.lossless_key)
        self.assertTrue(session.finished)
        self.assertEqual(len(session), 3)
        with self.assertRaises(SessionError):
            session.next_round()

    def test_double_vote_rejected(self) -> None:
        session = ABXSession(TestConfig(rounds=5))
        session.start()
        session.vote(SampleKey.A)
        with self.assertRaises(SessionError):
            session.vote(SampleKey.B)

    def test_vote_without_round(self) -> None:
        session = ABXSession(TestConfig(rounds=5))
        with self.assertRaises(SessionError):
            session.vote(SampleKey.A)

    def test_next_round_requires_finished_round(self) -> None:
        session = ABXSession(TestConfig(rounds=5))
        session.start()
        with self.assertRaises(SessionError):
            session.next_round()
        session.vote(SampleKey.A)
        session.next_round()  # now allowed

    def test_endless_mode(self) -> None:
        session = ABXSession(TestConfig(rounds=0, seed=7))
        self.assertTrue(session.config.endless)
        for i in range(25):
            truth = session.start().lossless_key if session.current is None else session.current.lossless_key
            record = session.vote(truth)
            self.assertTrue(record.correct)
            if not session.finished:
                session.next_round()
            self.assertEqual(session.round_number, i + 2 if not session.finished else i + 1)
        self.assertFalse(session.finished)
        self.assertEqual(len(session), 25)
        session.end_early()
        self.assertTrue(session.finished)

    def test_endless_label(self) -> None:
        self.assertEqual(TestConfig(rounds=0).rounds_text, "∞")
        self.assertEqual(TestConfig(rounds=20).rounds_text, "20")

    def test_seed_is_reproducible(self) -> None:
        def sequence(seed: int) -> list[SampleKey]:
            session = ABXSession(TestConfig(rounds=8, seed=seed, reveal_after_vote=True))
            keys = []
            for _ in range(8):
                keys.append(session.start().lossless_key)
                session.vote(session.current.lossless_key)
                if not session.finished:
                    session.next_round()
            return keys

        self.assertEqual(sequence(42), sequence(42))
        self.assertNotEqual(sequence(42), sequence(43))

    def test_no_long_runs_of_the_same_answer(self) -> None:
        session = ABXSession(TestConfig(rounds=40, seed=3))
        keys = []
        for _ in range(40):
            keys.append(session.start().lossless_key)
            session.vote(SampleKey.A)  # always wrong, so the layout is what varies
            if not session.finished:
                session.next_round()
        longest = 1
        run = 1
        for previous, current in zip(keys, keys[1:]):
            run = run + 1 if current is previous else 1
            longest = max(longest, run)
        self.assertLessEqual(longest, 2, "the hidden answer must not stay in one slot")

    def test_require_both_heard(self) -> None:
        session = ABXSession(TestConfig(rounds=5, require_both_heard=True))
        session.start()
        self.assertEqual(session.can_vote()[1], "need-both")
        session.mark_played(SampleKey.A)
        self.assertEqual(session.can_vote()[1], "need-both")
        session.mark_played(SampleKey.B)
        self.assertTrue(session.can_vote()[0])

    def test_min_plays(self) -> None:
        session = ABXSession(TestConfig(rounds=5, min_plays=2))
        session.start()
        self.assertEqual(session.can_vote()[1], "need-plays")
        session.mark_played(SampleKey.A)
        self.assertEqual(session.can_vote()[1], "need-plays")
        session.mark_played(SampleKey.B)  # second play
        self.assertTrue(session.can_vote()[0])

    def test_statistics_update(self) -> None:
        session = ABXSession(TestConfig(rounds=10, seed=5))
        truths: list[SampleKey] = []
        for _ in range(10):
            truths.append(session.start().lossless_key)
            session.vote(truths[-1])
            if not session.finished:
                session.next_round()
        stats = session.stats()
        self.assertEqual((stats.rounds, stats.correct), (10, 10))
        self.assertAlmostEqual(stats.p_value, p_value_greater(10, 10))
        self.assertTrue(stats.is_significant)
        self.assertEqual(stats.margin, 0)
        self.assertAlmostEqual(stats.rate, 1.0)
        self.assertEqual(session.verdict().level, "strong")
        self.assertIn("10/10", session.summary_line())

    def test_report_round_trip(self) -> None:
        session = ABXSession(TestConfig(rounds=6, seed=2))
        for _ in range(6):
            session.start()
            session.vote(session.current.lossless_key)
            if not session.finished:
                session.next_round()
        report = session.report(segment="0:00 – 0:30", sample_rate=44100)
        payload = report.to_dict()
        self.assertEqual(payload["rounds"], 6)
        self.assertEqual(payload["correct"], 6)
        self.assertEqual(len(payload["history"]), 6)
        self.assertTrue(payload["verdict"] == "significant")
        self.assertEqual(report.accuracy, "6/6")
        self.assertEqual(payload["sample_rate"], 44100)

    def test_blind_mode_hides_the_answer(self) -> None:
        session = ABXSession(TestConfig(rounds=3, reveal_after_vote=False))
        session.start()
        session.vote(SampleKey.A)
        self.assertEqual(session.current.state, RoundState.VOTED)

    def test_skip_round_does_not_score(self) -> None:
        session = ABXSession(TestConfig(rounds=3, seed=9))
        session.start()
        first = session.current
        session.skip_round()
        self.assertEqual(len(session), 0)
        self.assertIsNot(session.current, first)
        self.assertEqual(session.round_number, 1)  # skipped rounds are not counted

    def test_restart_resets(self) -> None:
        session = ABXSession(TestConfig(rounds=3, seed=4))
        session.start()
        session.vote(session.current.lossless_key)
        session.restart()
        self.assertEqual(len(session), 0)
        self.assertEqual(session.round_number, 1)
        self.assertEqual(session.current.index, 1)

    def test_sample_key_helpers(self) -> None:
        self.assertIs(SampleKey.A.other, SampleKey.B)
        self.assertIs(SampleKey.B.other, SampleKey.A)
        self.assertEqual(SampleKey("A"), SampleKey.A)


if __name__ == "__main__":
    unittest.main()
