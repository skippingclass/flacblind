"""Paint every widget in every state.

An exception inside ``paintEvent``/``resizeEvent`` is fatal under PyQt6: Qt
logs the traceback and then calls ``qFatal``.  Three separate crashes came from
exactly that — an invalid ``QColor`` overload, a zero font size, a removed
``QMediaPlayer`` method — and none of them happened while the tests were merely
*constructing* widgets.  So this module does what a user does: it drives each
custom widget through all of its states and rasterises it, while recording
every unhandled Python exception and every Qt message.

A test fails if either recorder sees anything.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import ROOT  # noqa: F401

from flacblind.core.models import Peaks, SampleKey
from flacblind.core.settings import AppSettings
from flacblind.ui.i18n import Translator
from flacblind.ui.main_window import MainWindow
from flacblind.ui.qtcompat import QApplication, Qt, QtCore, QtGui
from flacblind.ui.results_panel import DistributionView, ResultsPanel
from flacblind.ui.theme import Palette, apply_theme
from flacblind.ui.waveform_view import WaveformView
from flacblind.ui.widgets import Chip, DropCard, Notice, StatTile

_app = QApplication.instance() or QApplication(["flacblind-tests"])
apply_theme(_app)

_problems: list[str] = []
_py_excepthook = sys.excepthook


def _record_py_exception(exc_type, exc_value, tb) -> None:  # noqa: ANN001
    import traceback

    _problems.append("python: " + "".join(traceback.format_exception(exc_type, exc_value, tb))[-600:])


def _record_qt_message(mode: object, context: object, message: str) -> None:  # noqa: ARG001
    text = str(message)
    if "Traceback" in text or "TypeError" in text or "AttributeError" in text:
        _problems.append(f"qt: {text[-600:]}")


sys.excepthook = _record_py_exception
QtCore.qInstallMessageHandler(_record_qt_message)


def make_peaks(buckets: int = 400, seconds: float = 30.0) -> Peaks:
    rms = tuple(0.05 + 0.45 * abs(((i % 37) - 18) / 18.0) for i in range(buckets))
    return Peaks(
        mins=tuple(-v for v in rms),
        maxs=rms,
        rms=rms,
        total_frames=int(seconds * 44100),
        sample_rate=44100,
    )


def rasterise(widget: QtWidgets.QWidget) -> None:  # type: ignore[name-defined]
    """Force a full repaint and keep the result alive."""
    widget.repaint()
    widget.grab()


def hover(widget: QtWidgets.QWidget, x: float, y: float) -> None:
    """Deliver a real mouse-move so hover-dependent code actually runs."""
    event = QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove,
        QtCore.QPointF(x, y),
        QtCore.QPointF(x, y),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QtCore.QCoreApplication.sendEvent(widget, event)


class PaintTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _problems.clear()
        self.addCleanup(self._assert_clean)

    def _assert_clean(self) -> None:
        if _problems:
            self.fail("painting raised:\n" + "\n---\n".join(_problems))


class TestWaveformView(PaintTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.view = WaveformView()
        self.view.resize(640, 200)
        self.addCleanup(self.view.deleteLater)

    def test_empty_states(self) -> None:
        self.view.set_empty_text("nothing here")
        rasterise(self.view)
        self.view.clear()
        rasterise(self.view)

    def test_with_peaks(self) -> None:
        self.view.set_peaks(make_peaks(), color=Palette.sample_a)
        for position in (0.0, 0.5, 1.0):
            self.view.set_position(position)
            rasterise(self.view)

    def test_hovered(self) -> None:
        """The state that crashed: a hover line with a translucent colour."""
        self.view.set_peaks(make_peaks(), color=Palette.sample_b)
        self.view.set_position(0.42)
        for x in (10.0, 320.0, 630.0):
            hover(self.view, x, 40.0)
            rasterise(self.view)
        self.assertIsNotNone(self.view._hover)

    def test_difference_envelope(self) -> None:
        a = make_peaks()
        b = Peaks(tuple(v * 0.5 for v in a.mins), a.maxs, a.rms, a.total_frames, a.sample_rate)
        self.view.set_peaks(a.difference(b), color=Palette.primary)
        rasterise(self.view)

    def test_extreme_sizes(self) -> None:
        self.view.set_peaks(make_peaks())
        for width, height in ((1, 1), (2, 20), (37, 23), (2000, 40)):
            self.view.resize(width, height)
            rasterise(self.view)
        self.view.resize(640, 200)

    def test_single_bucket(self) -> None:
        self.view.set_peaks(Peaks((-0.5,), (0.5,), (0.3,), 44100, 44100))
        rasterise(self.view)

    def test_silent_file(self) -> None:
        self.view.set_peaks(Peaks((0.0,) * 50, (0.0,) * 50, (0.0,) * 50, 44100, 44100))
        rasterise(self.view)

    def test_long_duration_ticks(self) -> None:
        self.view.set_peaks(make_peaks(buckets=2400, seconds=7200.0))
        rasterise(self.view)
        self.view.set_peaks(make_peaks(buckets=2400, seconds=2.0))
        rasterise(self.view)

    def test_dragging(self) -> None:
        self.view.set_peaks(make_peaks())
        self.view._dragging = True
        hover(self.view, 400.0, 60.0)
        rasterise(self.view)
        self.view._dragging = False


class TestDistributionView(PaintTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.view = DistributionView()
        self.view.resize(300, 110)
        self.addCleanup(self.view.deleteLater)

    def test_states(self) -> None:
        rasterise(self.view)                       # empty
        self.view.set_data(10, 7)
        rasterise(self.view)
        self.view.set_data(1, 0)
        rasterise(self.view)
        self.view.set_data(50, 25)
        rasterise(self.view)
        self.view.clear()
        rasterise(self.view)

    def test_hover_and_markers(self) -> None:
        self.view.set_data(10, 7)
        hover(self.view, 120.0, 40.0)
        rasterise(self.view)
        self.view._hover = -1
        rasterise(self.view)

    def test_tiny(self) -> None:
        self.view.set_data(10, 5)
        for width, height in ((1, 1), (10, 20), (400, 30)):
            self.view.resize(width, height)
            rasterise(self.view)
        self.view.resize(300, 110)


class TestResultsPanel(PaintTestCase):
    def test_panel_states(self) -> None:
        from flacblind.core.abx import ABXSession, TestConfig

        panel = ResultsPanel()
        panel.resize(320, 600)
        self.addCleanup(panel.deleteLater)
        rasterise(panel)

        session = ABXSession(TestConfig(rounds=10, seed=5))
        for i in range(10):
            truth = session.start().lossless_key
            session.vote(truth if i < 9 else truth.other)
            if not session.finished:
                session.next_round()
        panel.set_stats(session.stats(), session.records)
        rasterise(panel)
        panel.set_stats(None, ())
        rasterise(panel)


class TestSmallWidgets(PaintTestCase):
    def test_stat_tile(self) -> None:
        tile = StatTile("results.score", accent=Palette.primary)
        tile.set_value("9/10", accent=Palette.success)
        tile.set_value("—")
        tile.retranslate()
        rasterise(tile)

    def test_chip(self) -> None:
        chip = Chip("ffmpeg ✓", Palette.success)
        chip.set_accent(Palette.danger)
        chip.setText("")
        rasterise(chip)

    def test_notice(self) -> None:
        notice = Notice()
        notice.resize(400, 80)
        self.addCleanup(notice.deleteLater)
        notice.show_error("broken", "hint text")
        rasterise(notice)
        notice.show_warning("careful")
        rasterise(notice)
        notice.show_info("fyi", Palette.sample_a)
        rasterise(notice)
        notice.hide()
        rasterise(notice)

    def test_drop_card(self) -> None:
        card = DropCard(SampleKey.A, "sources.lossless", "sources.hint.lossless", Palette.sample_a)
        card.resize(320, 120)
        self.addCleanup(card.deleteLater)
        rasterise(card)
        card.set_path("/music/Аптека 3.flac")
        rasterise(card)
        card._hover = True
        card._update_border()
        rasterise(card)
        card.set_path(None)
        rasterise(card)


class TestWholeWindow(PaintTestCase):
    def setUp(self) -> None:
        super().setUp()
        Translator.instance().set_language("en")
        self.window = MainWindow(AppSettings())
        self.window.resize(1280, 800)
        self.window.show()
        self.addCleanup(self.window.close)
        self.addCleanup(self.window.deleteLater)

    def test_idle_window(self) -> None:
        rasterise(self.window)

    def test_hover_every_interactive_widget(self) -> None:
        for widget in (
            self.window._waveform,
            self.window._results._distribution,
            self.window._card_a,
            self.window._card_b,
            self.window._button_a,
            self.window._vote_a,
        ):
            for x, y in ((5.0, 5.0), (60.0, 30.0), (300.0, 90.0)):
                hover(widget, x, y)
            rasterise(self.window)

    def test_with_pair_and_hover(self) -> None:
        from tests_support import StubPair

        stub = StubPair()
        self.window.controller.pair = stub.pair
        self.window.controller.player.load_pair(stub.pair)
        self.window.controller.start_session()
        self.window.controller.player.play(SampleKey.A)
        self.window._refresh_waveform()
        self.window._update_level_hints()
        self.window._update_round_label()
        self.window._on_duration(stub.pair.duration_sec * 1000)
        self.window._on_position(4321)
        hover(self.window._waveform, 200.0, 50.0)
        rasterise(self.window)
        self.window._wave_toggle.setCurrentIndex(1)
        hover(self.window._results._distribution, 100.0, 30.0)
        rasterise(self.window)
        self.window.controller.player.stop()

    def test_both_languages(self) -> None:
        for language in ("ru", "en"):
            Translator.instance().set_language(language)
            rasterise(self.window)

    def test_voting_flow_keeps_painting(self) -> None:
        from tests_support import StubPair

        stub = StubPair()
        self.window.controller.pair = stub.pair
        self.window.controller.player.load_pair(stub.pair)
        self.window.controller.start_session()
        self.window._apply_state()
        self.window._refresh_waveform()
        for _ in range(3):
            self.window._on_vote(SampleKey.A)
            rasterise(self.window)
            self.window._on_next()
            rasterise(self.window)


if __name__ == "__main__":
    unittest.main()
