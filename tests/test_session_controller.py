"""Session controller: preparation lifecycle, session flow and export."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import HAS_FFMPEG, ROOT, make_pair, require_ffmpeg  # noqa: F401

from flacblind.core.models import AudioKind, SampleKey, SegmentSpec
from flacblind.core.settings import AppSettings
from flacblind.ui.session_controller import (
    AppState,
    SessionController,
    export_report,
    format_summary,
)

_APP = None


def _ensure_app():  # noqa: ANN202
    """The player facade and the workers need a QApplication.

    The instance is kept in a module global on purpose: a QApplication that is
    garbage collected takes the whole Qt event loop with it.
    """
    global _APP
    from flacblind.ui.qtcompat import QApplication

    app = QApplication.instance()
    if app is None:
        _APP = app = QApplication(["flacblind-tests"])
    return app


def make_controller(tmp: Path) -> SessionController:
    _ensure_app()
    settings = AppSettings()
    settings.rounds = 5
    controller = SessionController(settings)
    controller.player.select_backend("auto")
    return controller


class TestControllerStates(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-ctrl-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.controller = make_controller(self.tmp)

    def tearDown(self) -> None:
        self.controller.shutdown()

    def test_initial_state(self) -> None:
        self.assertEqual(self.controller.state, AppState.IDLE)
        self.assertIsNone(self.controller.pair)
        self.assertIsNone(self.controller.session)
        self.assertFalse(self.controller.sources_ready())
        self.assertFalse(self.controller.preparing)

    def test_sources_and_swap(self) -> None:
        self.controller.set_source(SampleKey.A, str(self.tmp / "a.flac"))
        self.controller.set_source(SampleKey.B, str(self.tmp / "b.mp3"))
        self.assertTrue(self.controller.sources_ready())
        self.controller.swap_sources()
        self.assertEqual(self.controller.settings.lossless_path, str(self.tmp / "b.mp3"))
        self.assertEqual(self.controller.settings.lossy_path, str(self.tmp / "a.flac"))

    def test_same_file_is_not_ready(self) -> None:
        same = str(self.tmp / "same.flac")
        self.controller.set_source(SampleKey.A, same)
        self.controller.set_source(SampleKey.B, same)
        self.assertFalse(self.controller.sources_ready())

    def test_start_without_sources_is_refused(self) -> None:
        started, reason = self.controller.start_preparation()
        self.assertFalse(started)
        self.assertEqual(reason, "sources")
        self.assertEqual(self.controller.state, AppState.IDLE)

    def test_test_config_mirrors_settings(self) -> None:
        self.controller.settings.rounds = 20
        self.controller.settings.seed = 7
        self.controller.settings.require_both_heard = True
        config = self.controller.test_config()
        self.assertEqual(config.rounds, 20)
        self.assertEqual(config.seed, 7)
        self.assertTrue(config.require_both_heard)

    def test_new_session_without_pair(self) -> None:
        self.controller.new_session()
        self.assertEqual(self.controller.state, AppState.IDLE)

    def test_workspace_is_reused(self) -> None:
        first = self.controller.ensure_workspace()
        second = self.controller.ensure_workspace()
        self.assertIs(first, second)
        self.assertTrue(first.path.exists())


@require_ffmpeg
class TestPreparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="fb-test-ctrl-ffmpeg-"))
        cls.lossless, cls.lossy = make_pair(cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.controller = make_controller(self.tmp)
        self.controller.set_source(SampleKey.A, self.lossless)
        self.controller.set_source(SampleKey.B, self.lossy)

    def tearDown(self) -> None:
        self.controller.shutdown()

    def _run_worker(self) -> None:
        """Run the worker to completion, pumping the event loop meanwhile.

        Worker signals are delivered as queued events, so ``wait()`` alone
        would swallow them — exactly what the real application does while it
        keeps painting.
        """
        from flacblind.ui.qtcompat import QApplication

        app = _ensure_app()
        worker = self.controller.worker
        assert worker is not None
        worker.progress.connect(lambda *_: None)
        worker.prepared.connect(self.controller.on_prepared)
        worker.failed.connect(self.controller.on_prepare_failed)
        worker.cancelled.connect(self.controller.on_prepare_cancelled)
        worker.start()
        deadline = time.monotonic() + 120
        while worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        worker.wait(5000)
        for _ in range(5):
            app.processEvents()

    def test_preparation_flow(self) -> None:
        started, reason = self.controller.start_preparation()
        self.assertTrue(started, reason)
        self.assertTrue(self.controller.preparing)
        self.assertEqual(self.controller.state, AppState.PREPARING)
        self.assertIsNotNone(self.controller.workspace)
        self.assertTrue(self.controller.workspace.path.exists())  # type: ignore[union-attr]

        self._run_worker()
        self.assertFalse(self.controller.preparing)
        self.assertIsNotNone(self.controller.pair)
        assert self.controller.pair is not None
        self.assertIs(self.controller.pair.sample_a.kind, AudioKind.LOSSLESS)
        self.assertIsNotNone(self.controller.session)
        self.assertEqual(self.controller.state, AppState.ROUND)
        # the player received the pair
        self.assertEqual(self.controller.player.duration_ms, int(self.controller.pair.duration_sec * 1000))

    def test_second_preparation_is_refused_while_running(self) -> None:
        self.controller.start_preparation()
        started, reason = self.controller.start_preparation()
        self.assertFalse(started)
        self.assertEqual(reason, "already running")
        self._run_worker()

    def test_cancellation(self) -> None:
        self.controller.start_preparation()
        self.controller.cancel_preparation()
        self._run_worker()
        self.assertFalse(self.controller.preparing)
        self.assertIsNone(self.controller.pair)

    def test_session_vote_cycle(self) -> None:
        self.controller.start_preparation()
        self._run_worker()
        assert self.controller.session is not None

        # round 1: vote, then advance
        self.assertEqual(self.controller.state, AppState.ROUND)
        self.assertTrue(self.controller.can_vote()[0])
        truth = self.controller.session.current.lossless_key  # type: ignore[union-attr]
        self.controller.vote(truth)
        self.assertEqual(self.controller.state, AppState.VOTED)
        self.assertEqual(self.controller.can_vote()[1], "already-voted")

        self.controller.next_round()
        self.assertEqual(self.controller.state, AppState.ROUND)
        self.assertTrue(self.controller.can_vote()[0])

        # round 2: vote, then finish
        truth = self.controller.session.current.lossless_key  # type: ignore[union-attr]
        self.controller.vote(truth.other)
        self.assertEqual(len(self.controller.records()), 2)
        self.assertEqual(self.controller.stats().correct, 1)

        self.controller.finish()
        self.assertEqual(self.controller.state, AppState.FINISHED)
        self.assertIsNotNone(self.controller.last_report)
        self.assertEqual(len(self.controller.records()), 2)
        self.assertEqual(self.controller.stats().correct, 1)
        self.assertIn("1/2", format_summary(self.controller.last_report))

    def test_shutdown_cleans_the_workspace(self) -> None:
        self.controller.start_preparation()
        self._run_worker()
        workspace = self.controller.workspace
        self.assertIsNotNone(workspace)
        path = workspace.path  # type: ignore[union-attr]
        self.assertTrue(path.exists())
        self.controller.settings.delete_temp_on_exit = True
        self.controller.shutdown()
        self.assertFalse(path.exists())

    def test_shutdown_can_keep_the_files(self) -> None:
        self.controller.start_preparation()
        self._run_worker()
        workspace = self.controller.workspace
        path = workspace.path  # type: ignore[union-attr]
        self.controller.settings.delete_temp_on_exit = False
        self.controller.shutdown()
        self.assertTrue(path.exists())
        shutil.rmtree(path, ignore_errors=True)


class TestExport(unittest.TestCase):
    def _report(self, correct: int = 6, total: int = 6):  # noqa: ANN202
        from flacblind.core.abx import ABXSession, TestConfig

        session = ABXSession(TestConfig(rounds=total, seed=3))
        for index in range(total):
            truth = session.start().lossless_key
            session.vote(truth if index < correct else truth.other)
            if not session.finished:
                session.next_round()
        return session.report(segment="0:00 – 0:30", sample_rate=44100)

    def test_json_export(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="fb-test-export-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        report = self._report()
        target = export_report(tmp / "out.json", report)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["rounds"], 6)
        self.assertEqual(data["correct"], 6)
        self.assertEqual(data["verdict"], "significant")
        self.assertEqual(len(data["history"]), 6)
        self.assertEqual(data["history"][0]["round"], 1)
        self.assertEqual(data["sample_rate"], 44100)

    def test_csv_export(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="fb-test-export-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        report = self._report(correct=4, total=6)
        target = export_report(tmp / "out.csv", report, fmt="csv")
        text = target.read_text(encoding="utf-8")
        lines = text.strip().splitlines()
        self.assertTrue(lines[0].startswith("round,guess,truth,correct"))
        self.assertEqual(len(lines), 6 + 3)  # header + rounds + blank + summary

    def test_summary_line(self) -> None:
        report = self._report(correct=3, total=6)
        summary = format_summary(report)
        self.assertIn("3/6", summary)
        self.assertIn("p = ", summary)
        self.assertIn("95 % CI", summary)


if __name__ == "__main__":
    unittest.main()
