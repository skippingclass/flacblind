"""GUI-level tests: window construction, state transitions, shortcuts, i18n.

Runs on Qt's ``offscreen`` platform, so it works in CI without a display.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import ROOT  # noqa: F401

from flacblind.core.models import SampleKey
from flacblind.core.settings import AppSettings
from flacblind.ui.i18n import Translator
from flacblind.ui.main_window import MainWindow, safe_slot
from flacblind.ui.qtcompat import QApplication, QtCore, QtGui, QtWidgets

_app = QApplication.instance() or QApplication(["flacblind-tests"])


def pump(ms: int = 30) -> None:
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec()


class TestWindowConstruction(unittest.TestCase):
    def setUp(self) -> None:
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-window-"))
        settings = AppSettings()
        settings.language = "en"
        self.window = MainWindow(settings)
        self.window.show()
        pump(20)
        self.drops = DropHelper()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(_forget_settings)


    def test_every_panel_exists(self) -> None:
        for attribute in (
            "_card_a", "_card_b", "_prepare_button", "_button_a", "_button_b",
            "_vote_a", "_vote_b", "_waveform", "_results", "_segment_editor",
            "_progress", "_notice", "_backend_combo", "_volume", "_position",
        ):
            self.assertTrue(hasattr(self.window, attribute), attribute)

    def test_initial_state_is_idle(self) -> None:
        self.assertEqual(self.window.controller.state, "idle")
        self.assertFalse(self.window._vote_a.isEnabled())
        self.assertFalse(self.window._button_a.isEnabled())
        self.assertFalse(self.window._prepare_button.isEnabled())

    def test_shortcuts_registered(self) -> None:
        self.assertEqual(
            set(self.window._shortcuts),
            {"space", "a", "b", "vote_a", "vote_b", "left", "right", "esc", "next", "mute"},
        )

    def test_backend_combo_lists_choices(self) -> None:
        labels = [self.window._backend_combo.itemText(i) for i in range(self.window._backend_combo.count())]
        self.assertGreaterEqual(len(labels), 4)  # auto + 3 backends

    def test_drop_targets_accept_drops(self) -> None:
        self.assertTrue(self.window._card_a.acceptDrops())
        self.assertTrue(self.window._card_b.acceptDrops())
        self.assertTrue(self.window.acceptDrops())

    def test_window_drop_assigns_a_source(self) -> None:
        flac = _touch(self.tmp / "song.flac")
        self.window.dropEvent(self.drops.event(flac))
        self.assertEqual(self.window.settings.lossless_path, str(flac))

    def test_second_drop_fills_the_lossy_slot(self) -> None:
        flac = _touch(self.tmp / "song.flac")
        mp3 = _touch(self.tmp / "song.mp3")
        self.window.dropEvent(self.drops.event(flac))
        self.window.dropEvent(self.drops.event(mp3))
        self.assertEqual(self.window.settings.lossless_path, str(flac))
        self.assertEqual(self.window.settings.lossy_path, str(mp3))
        self.assertTrue(self.window._prepare_button.isEnabled())

    def test_dropping_a_directory_is_ignored(self) -> None:
        self.window.dropEvent(self.drops.event(self.tmp))
        self.assertEqual(self.window.settings.lossless_path, "")

    def test_duplicate_file_is_refused(self) -> None:
        same = _touch(self.tmp / "same.flac")
        self.window._on_drop(str(same), SampleKey.A)
        self.window._on_drop(str(same), SampleKey.B)
        self.assertEqual(self.window.settings.lossy_path, "")
        self.assertTrue(self.window._notice.isVisible())

    def test_waveform_view_paints_without_data(self) -> None:
        self.window._waveform.set_peaks(None)
        self.window._waveform.repaint()
        self.window._waveform.grab()  # must not raise

    def test_language_switch_updates_texts(self) -> None:
        Translator.instance().set_language("ru")
        pump(10)
        self.assertEqual(self.window._vote_question.text(), "Какой образец без потерь?")
        self.assertEqual(self.window._results._title.text(), "Результаты")
        Translator.instance().set_language("en")
        pump(10)
        self.assertEqual(self.window._vote_question.text(), "Which one is lossless?")

    def test_decorated_slot_drops_qt_surplus_arguments(self) -> None:
        """`clicked` sends `checked`; variadic wrappers must not forward it.

        Regression: a wrapper declared as (*args) made PyQt6 stop discarding the
        surplus, so every zero-argument slot raised TypeError on click.
        """
        calls: list[object] = []

        class Dummy:
            @safe_slot
            def zero(self) -> None:
                calls.append("zero")

            @safe_slot
            def one(self, value: object) -> None:
                calls.append(("one", value))

            @safe_slot
            def two(self, a: object, b: object) -> None:
                calls.append(("two", a, b))

            @safe_slot
            def star(self, *args: object) -> None:
                calls.append(("star", args))

            @safe_slot
            def star_kw(self, *args: object, **kwargs: object) -> None:
                calls.append(("star_kw", args, kwargs))

            @safe_slot
            def kw(self, value: object, extra: object = None) -> None:
                calls.append(("kw", value, extra))

        dummy = Dummy()
        dummy.zero(True)                       # clicked(bool)
        dummy.one(True, 42)                    # currentIndexChanged + clicked
        dummy.two(1, 2, 3)
        dummy.star(1, 2, 3)
        dummy.star_kw(1, surplus=2)
        dummy.kw(7, extra=9, unknown=1)
        self.assertEqual(
            calls,
            ["zero", ("one", True), ("two", 1, 2), ("star", (1, 2, 3)),
             ("star_kw", (1,), {"surplus": 2}), ("kw", 7, 9)],
        )

    def test_every_button_click_reaches_its_slot(self) -> None:
        """Click the real buttons: no slot may raise, no error notice appears."""
        self.window.show()
        pump(20)
        self.window._notice.hide()
        for button in (
            self.window._prepare_button,      # no sources -> warning notice
            self.window._cancel_button,
            self.window._new_session,
            self.window._next_button,
            self.window._skip_button,
            self.window._button_a,
            self.window._button_b,
            self.window._vote_a,
            self.window._vote_b,
            self.window._play_button,
            self.window._stop_button,
            self.window._loop_button,
            self.window._mute_button,
            self.window._results._export,
        ):
            with self.subTest(button=button.objectName() or button.text()):
                before = self.window._notice._label.text()
                button.click()
                pump(10)
                self.assertNotIn("action failed", self.window._notice._label.text().lower())
                del before

    def test_action_menu_entries_are_slot_safe(self) -> None:
        """QAction.triggered also passes a bool."""
        self.window.show()
        pump(20)
        for action in (
            self.window._action_new,
            self.window._action_finish,
            self.window._action_difference,
        ):
            with self.subTest(action=action.text()):
                action.trigger()
                pump(10)
                self.assertNotIn("action failed", self.window._notice._label.text().lower())

    def test_slot_exceptions_are_contained(self) -> None:
        """A bug in a slot must not take the window down (PyQt6 aborts)."""
        self.window.show()
        pump(20)
        # Fault injection: the backend refuses to be created.
        def boom(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("no audio device")

        self.window.controller.player.select_backend = boom  # type: ignore[method-assign]
        index = self.window._backend_combo.findData("cli")
        self.window._on_backend_changed(index)  # must not raise
        pump(20)
        self.assertTrue(self.window._notice.isVisible())
        self.assertIn("_on_backend_changed", self.window._notice._label.text())
        self.assertIn("no audio device", self.window._notice._label.text())
        # …and the window is still alive afterwards.
        self.window._on_vote.__self__  # attribute access still fine
        self.window.retranslate()
        self.assertEqual(self.window._vote_question.text(), "Which one is lossless?")

    def test_language_switch_translates_every_visible_string(self) -> None:
        """A language switch must not leave half the window in English."""
        self.window.show()
        pump(30)

        # PySide6's findChildren() takes a single type, PyQt6 also accepts a
        # tuple — so ask once per type to stay binding agnostic.
        label_types = (
            QtWidgets.QLabel,
            QtWidgets.QPushButton,
            QtWidgets.QCheckBox,
            QtWidgets.QRadioButton,
        )

        def texts() -> list[str]:
            out: list[str] = []
            for kind in label_types:
                for widget in self.window.findChildren(kind):
                    text = widget.text()
                    if text and text.isascii() and any(ch.isalpha() for ch in text):
                        out.append(text)
            return out

        before = set(texts())
        Translator.instance().set_language("ru")
        pump(30)
        after = set(texts())
        # Strings that legitimately do not change: file metadata, the A/B
        # badges, codec names and the "A — B difference" enum-ish labels.
        allowed = {
            # badges, codec names and units: identical in both languages
            "A", "B", "FLAC", "MP3", "WAV", "44 kHz", "Qt Multimedia", "kHz",
            "LUFS", "dBTP", "ffmpeg", "ffprobe", "p-value", "A - B difference",
        }
        stale = {text for text in before & after if text not in allowed}
        self.assertFalse(stale, f"still English after switching: {sorted(stale)}")
        Translator.instance().set_language("en")
        pump(30)

    def test_status_chips_reflect_environment(self) -> None:
        self.window._sync_status_chips()
        self.assertIn("ffmpeg", self.window._ffmpeg_chip.text())

    def test_geometry_is_saved_on_close(self) -> None:
        self.window._save_settings()
        saved = AppSettings().load()
        self.assertTrue(saved.geometry)


def _forget_settings() -> None:
    AppSettings.default_path().unlink(missing_ok=True)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return path


class DropHelper:
    """Builds drag-and-drop events and keeps them alive.

    Qt does not take ownership of the ``QMimeData`` handed to ``QDropEvent``;
    letting the Python wrappers be collected first segfaults, so every event is
    parked on this object until the test finishes.
    """

    def __init__(self) -> None:
        self._kept: list[tuple[QtCore.QMimeData, QtGui.QDropEvent]] = []

    def event(self, *paths: Path) -> QtGui.QDropEvent:
        from flacblind.ui.qtcompat import Qt

        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(str(path)) for path in paths])
        event = QtGui.QDropEvent(
            QtCore.QPointF(10.0, 10.0),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self._kept.append((mime, event))
        return event


class TestInteractionFlow(unittest.TestCase):
    """Drives the window through a full session with a prepared pair stubbed in."""

    def setUp(self) -> None:
        from tests_support import StubPair

        Translator.instance().set_language("en")
        settings = AppSettings()
        settings.rounds = 5
        self.window = MainWindow(settings)
        # The final-summary dialog is modal; the tests exercise the state
        # machine, not the QMessageBox.
        self.window._show_report = lambda: None
        self.stub = StubPair()
        controller = self.window.controller
        controller.player.select_backend("auto")
        controller.pair = self.stub.pair
        controller.player.load_pair(self.stub.pair)
        controller.start_session()
        controller.state = "round"
        self.window._apply_state()
        self.window._refresh_waveform()
        self.window._update_round_label()
        pump(10)

    def tearDown(self) -> None:
        self.window.controller.player.shutdown()
        self.window.close()
        pump(10)
        _forget_settings()

    def test_vote_records_and_enables_next(self) -> None:
        self.assertTrue(self.window._vote_a.isEnabled())
        self.window._on_vote(SampleKey.A)
        self.assertEqual(len(self.window.controller.records()), 1)
        self.assertFalse(self.window._next_button.isHidden())
        self.assertFalse(self.window._vote_a.isEnabled())
        self.assertIn("lossless", self.window._feedback.text())

    def test_next_round_advances(self) -> None:
        self.window._on_vote(SampleKey.A)
        first = self.window.controller.session.current.index
        self.window._on_next()
        self.assertEqual(self.window.controller.session.current.index, first + 1)
        self.assertTrue(self.window._next_button.isHidden())
        self.assertEqual(len(self.window.controller.records()), 1)

    def test_five_rounds_finish_the_session(self) -> None:
        for _ in range(5):
            truth = self.window.controller.session.current.lossless_key
            self.window._on_vote(truth)
            if not self.window.controller.session_finished():
                self.window._on_next()
        self.assertEqual(len(self.window.controller.records()), 5)
        self.assertTrue(self.window.controller.session_finished())
        self.assertEqual(self.window.controller.state, "finished")
        self.assertTrue(self.window.controller.stats().is_significant)

    def test_shortcut_paths(self) -> None:
        self.window._on_play_sample(SampleKey.A)
        self.assertTrue(self.window.controller.player.is_playing)
        self.window._on_play_pause()
        self.assertFalse(self.window.controller.player.is_playing)

    def test_waveform_seek_moves_the_player(self) -> None:
        self.window._on_waveform_seek(0.5)
        self.assertAlmostEqual(
            self.window.controller.player.position_ms,
            self.window.controller.player.duration_ms / 2,
            delta=2,
        )

    def test_difference_view_toggle(self) -> None:
        self.window._wave_toggle.setCurrentIndex(1)
        pump(10)
        self.assertTrue(self.window._show_difference)
        self.assertTrue(self.window.settings.show_difference_view)
        self.window._wave_toggle.setCurrentIndex(0)
        pump(10)
        self.assertFalse(self.window._show_difference)

    def test_stale_auto_advance_timer_does_not_skip_a_round(self) -> None:
        self.window.settings.auto_advance = True
        self.window._on_vote(SampleKey.A)
        self.window._on_next()            # the user advances by hand
        rounds_seen = self.window.controller.session.round_number
        self.window._auto_next()          # the queued timer fires afterwards
        self.assertEqual(self.window.controller.session.round_number, rounds_seen)
        self.assertEqual(len(self.window.controller.records()), 1)

    def test_auto_advance_moves_on_by_itself(self) -> None:
        self.window.settings.auto_advance = True
        self.window._on_vote(SampleKey.A)
        self.window._auto_next()
        self.assertEqual(self.window.controller.state, "round")
        self.assertEqual(len(self.window.controller.records()), 1)

    def test_new_session_resets_state(self) -> None:
        self.window._on_vote(SampleKey.A)
        self.window._on_new_session()
        self.assertEqual(len(self.window.controller.records()), 0)
        self.assertEqual(self.window._results._table.rowCount(), 0)
        self.assertEqual(self.window.controller.state, "round")

    def test_endless_session_keeps_going(self) -> None:
        self.window.settings.rounds = 0
        self.window._on_new_session()
        for _ in range(8):
            truth = self.window.controller.session.current.lossless_key
            self.window._on_vote(truth)
            self.window._on_next()
        self.assertEqual(len(self.window.controller.records()), 8)
        self.assertFalse(self.window.controller.session_finished())
        self.window._on_finish()
        self.assertTrue(self.window.controller.session_finished())

    def test_require_both_blocks_early_votes(self) -> None:
        self.window.settings.require_both_heard = True
        self.window.controller.start_session()
        self.window._apply_state()
        allowed, reason = self.window.controller.can_vote()
        self.assertFalse(allowed)
        self.assertEqual(reason, "need-both")
        self.window.controller.play(SampleKey.A)
        self.window.controller.play(SampleKey.B)
        self.assertTrue(self.window.controller.can_vote()[0])


if __name__ == "__main__":
    unittest.main()
