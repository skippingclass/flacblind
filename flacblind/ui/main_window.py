"""The main window.

Layout (left → centre → right):

* **left**   sources, processing options, segment, session settings
* **centre** transport + waveform + the vote
* **right**  results: score, exact p-value, null distribution, history

The window is the *view*; every decision is delegated to
:class:`~flacblind.ui.session_controller.SessionController`, and the only Qt
work happening on other threads is the ffmpeg pipeline.
"""

from __future__ import annotations

import functools
import inspect
import traceback
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..core.models import RoundRecord, SampleKey, SegmentSpec, SessionReport
from ..core.stats import fmt_p_value
from ..core.settings import AppSettings
from .i18n import Translator, tr
from .player import available_backends
from .qtcompat import (
    Qt,
    QtCore,
    QtGui,
    QtWidgets,
    binding_version,
)
from .results_panel import ResultsPanel
from .session_controller import AppState, SessionController, export_report
from .theme import Palette, make_icon, scaled_font
from .waveform_view import WaveformView, format_position
from .widgets import (
    Chip,
    DropCard,
    Notice,
    SegmentEditor,
    card,
    form_row,
    h2,
    mono,
    section_label,
)

__all__ = ["MainWindow", "safe_slot"]


def safe_slot(handler: "Callable[..., None]") -> "Callable[..., None]":
    """Wrap a slot so a bug shows a notice instead of killing the app.

    PyQt6 aborts the process (``qFatal``) when an exception escapes a slot, so
    a single AttributeError deep inside a backend would take the window down
    with it.  Every user-facing action goes through this decorator.

    The wrapper is variadic, which means PyQt6 stops dropping the surplus
    arguments it normally discards — ``clicked`` sends ``checked``, ``triggered``
    sends ``checked``, ``currentIndexChanged`` sends an int.  Slots that take no
    argument are therefore called without them, exactly like PyQt6 would.
    """
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        parameters = {}
    takes_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters.values()
    )
    takes_varkw = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    max_positional = 0 if takes_varargs else max(len(parameters) - 1, 0)
    accepted_keywords = {
        name
        for name, parameter in parameters.items()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name != "self"
    }

    @functools.wraps(handler)
    def wrapper(self: "MainWindow", *args: object, **kwargs: object) -> None:
        try:
            if not takes_varargs and len(args) > max_positional:
                args = args[:max_positional]          # drop Qt's surplus args
            if kwargs and not takes_varkw:
                kwargs = {k: v for k, v in kwargs.items() if k in accepted_keywords}
            handler(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberate last-resort guard
            traceback.print_exc()
            try:
                self._notice.show_error(
                    tr("common.action_failed").format(action=handler.__name__),
                    f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001, S110  # pragma: no cover
                pass

    return wrapper


_SAMPLE_RATE_CHOICES = (44100, 48000, 96000, 22050)
_AUDIO_PATTERN = (
    "*.flac *.wav *.wave *.aiff *.aif *.m4a *.mp3 *.aac *.ogg *.oga *.opus "
    "*.wv *.ape *.wma *.mp4"
)


def _file_filter() -> str:
    """The QFileDialog filter, translated."""
    return (
        f"{tr('common.filter')} ({_AUDIO_PATTERN});;"
        f"{tr('common.all_files')} (*)"
    )


class MainWindow(QtWidgets.QMainWindow):
    """The application window."""

    def __init__(self, settings: AppSettings | None = None) -> None:
        """Build the window.

        ``settings`` is used **as-is**; loading it from disk is the caller's
        job (see :func:`flacblind.app.build_application`) so that tests and
        embedding apps stay in control of their configuration.
        """
        super().__init__()
        self.settings = settings if settings is not None else AppSettings().load()
        self.controller = SessionController(self.settings)
        self._probe_worker = None
        self._probe_queue: list[str] = []
        self._history: list[RoundRecord] = []
        self._last_report: SessionReport | None = None
        self._segment = self.settings.segment()
        self._show_difference = bool(self.settings.show_difference_view)
        self._retranslate_hook = Translator.instance().on_change(self.retranslate)
        self._destroyed = False
        self._group_titles: list[tuple[QtWidgets.QWidget, str]] = []
        self._notice = Notice()

        self.setWindowTitle(tr("app.title"))
        self.setMinimumSize(1080, 700)
        self.setAcceptDrops(True)

        self._build_ui()
        self._build_menus()
        self._build_shortcuts()
        self._build_statusbar()
        self._connect_signals()
        self._load_persisted_state()
        self.retranslate()
        self._apply_state()
        self._update_dependent_labels()

    # =====================================================================
    #  Construction
    # =====================================================================
    def _build_ui(self) -> None:
        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_center_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([390, 700, 330])
        self.setCentralWidget(splitter)

    def _build_left_panel(self) -> QtWidgets.QWidget:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        # AsNeeded rather than AlwaysOff: with a long translation the sidebar
        # should be scrollable, never silently clipped.
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        panel = QtWidgets.QWidget()
        panel.setMinimumWidth(300)
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # -- sources ---------------------------------------------------
        sources = card()
        sources_layout = sources.layout()
        assert isinstance(sources_layout, QtWidgets.QVBoxLayout)
        sources_layout.setContentsMargins(12, 12, 12, 12)
        sources_layout.setSpacing(8)
        self._sources_title = section_label(tr("sources.title"))
        sources_layout.addWidget(self._sources_title)

        self._card_a = DropCard(
            SampleKey.A, "sources.lossless", "sources.hint.lossless", Palette.sample_a
        )
        self._card_b = DropCard(
            SampleKey.B, "sources.lossy", "sources.hint.lossy", Palette.sample_b
        )
        sources_layout.addWidget(self._card_a)
        sources_layout.addWidget(self._card_b)

        self._swap_button = QtWidgets.QPushButton(tr("sources.swap"))
        self._swap_button.setProperty("variant", "ghost")
        self._swap_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._swap_button.clicked.connect(self._on_swap)
        sources_layout.addWidget(self._swap_button)
        layout.addWidget(sources)

        # -- prepare ---------------------------------------------------
        prepare_card = card()
        prepare_layout = prepare_card.layout()
        assert isinstance(prepare_layout, QtWidgets.QVBoxLayout)
        prepare_layout.setContentsMargins(12, 12, 12, 12)
        prepare_layout.setSpacing(8)
        self._prepare_title = section_label(tr("prepare.title"))
        prepare_layout.addWidget(self._prepare_title)

        self._prepare_button = QtWidgets.QPushButton(tr("prepare.start"))
        self._prepare_button.setProperty("variant", "primary")
        self._prepare_button.setIcon(make_icon("refresh", "#06121f", 16))
        self._prepare_button.setMinimumHeight(38)
        self._prepare_button.setCursor(Qt.CursorShape.PointingHandCursor)
        prepare_layout.addWidget(self._prepare_button)

        buttons = QtWidgets.QHBoxLayout()
        self._cancel_button = QtWidgets.QPushButton(tr("prepare.cancel"))
        self._cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_button.setEnabled(False)
        self._cancel_button.clicked.connect(self._on_cancel)
        buttons.addWidget(self._cancel_button)
        buttons.addStretch(1)
        prepare_layout.addLayout(buttons)

        self._progress = QtWidgets.QProgressBar()
        self._progress.setRange(0, 1000)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.hide()
        prepare_layout.addWidget(self._progress)

        self._stage_label = QtWidgets.QLabel("")
        self._stage_label.setProperty("role", "hint")
        prepare_layout.addWidget(self._stage_label)
        layout.addWidget(prepare_card)

        # -- processing options ---------------------------------------
        processing = self._group("prepare.processing")
        processing_layout = processing.layout()
        self._loudness_check = QtWidgets.QCheckBox(tr("prepare.loudness"))
        self._loudness_check.setToolTip(tr("prepare.loudness.tip"))
        self._loudness_check.setChecked(True)
        self._lufs = QtWidgets.QDoubleSpinBox()
        self._lufs.setRange(-40.0, -5.0)
        self._lufs.setSingleStep(0.5)
        self._lufs.setDecimals(1)
        self._lufs.setSuffix(" LUFS")
        self._lufs.setMinimumWidth(132)
        self._lufs.setValue(self.settings.target_lufs)
        self._true_peak = QtWidgets.QDoubleSpinBox()
        self._true_peak.setRange(-9.0, 0.0)
        self._true_peak.setSingleStep(0.5)
        self._true_peak.setDecimals(1)
        self._true_peak.setSuffix(" dBTP")
        self._true_peak.setMinimumWidth(132)
        self._true_peak.setValue(self.settings.true_peak_db)
        self._lra = QtWidgets.QDoubleSpinBox()
        self._lra.setRange(1.0, 20.0)
        self._lra.setSingleStep(1.0)
        self._lra.setDecimals(1)
        self._lra.setMinimumWidth(132)
        self._lra.setValue(self.settings.lra)
        self._rate = QtWidgets.QComboBox()
        for rate in _SAMPLE_RATE_CHOICES:
            self._rate.addItem(f"{rate // 1000} kHz", rate)
        index = self._rate.findData(self.settings.sample_rate)
        self._rate.setCurrentIndex(max(0, index))

        self._row_target = form_row(self._lufs, label=tr("prepare.target"))
        self._row_true_peak = form_row(self._true_peak, label=tr("prepare.true_peak"))
        self._row_lra = form_row(self._lra, label=tr("prepare.lra"))
        self._row_rate = form_row(self._rate, label=tr("prepare.rate"))
        self._levels_title = section_label(tr("prepare.levels"))
        self._seed_label = QtWidgets.QLabel(tr("session.seed"))
        assert isinstance(processing_layout, QtWidgets.QVBoxLayout)
        processing_layout.addWidget(self._loudness_check)
        for row in (self._row_target, self._row_true_peak, self._row_lra, self._row_rate):
            processing_layout.addWidget(row)
        processing_layout.addWidget(self._levels_title)
        self._level_hint = QtWidgets.QLabel("")
        self._level_hint.setProperty("role", "hint")
        self._level_hint.setWordWrap(True)
        processing_layout.addWidget(self._level_hint)
        layout.addWidget(processing)

        # -- segment ---------------------------------------------------
        segment_group = self._group(tr("prepare.segment"))
        self._segment_editor = SegmentEditor()
        self._segment_editor.set_segment(self._segment)
        self._segment_editor.changed.connect(self._on_segment_changed)
        segment_layout = segment_group.layout()
        assert isinstance(segment_layout, QtWidgets.QVBoxLayout)
        segment_layout.addWidget(self._segment_editor)
        layout.addWidget(segment_group)

        # -- session ---------------------------------------------------
        session_group = self._group(tr("session.title"))
        self._round_buttons: dict[int, QtWidgets.QRadioButton] = {}
        rounds_layout = QtWidgets.QHBoxLayout()
        rounds_layout.setSpacing(6)
        for value in (5, 10, 20, 0):
            button = QtWidgets.QRadioButton()
            self._round_buttons[value] = button
            rounds_layout.addWidget(button)
        rounds_layout.addStretch(1)
        self._new_session = QtWidgets.QPushButton(tr("session.new"))
        self._new_session.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reveal_check = QtWidgets.QCheckBox(tr("session.reveal"))
        self._reveal_check.setToolTip(tr("session.reveal.tip"))
        self._reveal_check.setChecked(self.settings.reveal_after_vote)
        self._require_both = QtWidgets.QCheckBox(tr("session.require_both"))
        self._require_both.setChecked(self.settings.require_both_heard)
        self._require_both.setToolTip(tr("vote.need_both"))
        self._auto_advance = QtWidgets.QCheckBox(tr("settings.auto_advance"))
        self._auto_advance.setChecked(self.settings.auto_advance)
        self._auto_advance.setToolTip(tr("settings.auto_advance.tip"))
        self._seed = QtWidgets.QSpinBox()
        self._seed.setRange(0, 999999)
        self._seed.setSpecialValueText("—")
        self._seed.setToolTip(tr("session.seed.tip"))
        self._seed.setValue(self.settings.seed or 0)
        session_layout = session_group.layout()
        assert isinstance(session_layout, QtWidgets.QVBoxLayout)
        self._rounds_title = section_label(tr("session.rounds"))
        session_layout.addWidget(self._rounds_title)
        session_layout.addLayout(rounds_layout)
        session_layout.addWidget(self._reveal_check)
        session_layout.addWidget(self._require_both)
        session_layout.addWidget(self._auto_advance)
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(8)
        self._seed_label.setProperty("role", "subtitle")
        bottom.addWidget(self._seed_label)
        bottom.addWidget(self._seed, 1)
        bottom.addWidget(self._new_session)
        session_layout.addLayout(bottom)
        layout.addWidget(session_group)
        layout.addStretch(1)

        scroll.setWidget(panel)
        scroll.setMinimumWidth(330)
        scroll.setMaximumWidth(430)
        return scroll

    def _group(self, title_key: str) -> QtWidgets.QGroupBox:
        """A titled settings group; titles are re-applied on language change."""
        group = QtWidgets.QGroupBox(tr(title_key))
        group.setLayout(QtWidgets.QVBoxLayout())
        group.layout().setContentsMargins(12, 8, 12, 12)
        group.layout().setSpacing(8)
        self._group_titles.append((group, title_key))
        return group

    def _build_center_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # -- stage -----------------------------------------------------
        stage = card()
        stage_layout = stage.layout()
        assert isinstance(stage_layout, QtWidgets.QVBoxLayout)
        stage_layout.setContentsMargins(16, 14, 16, 14)
        stage_layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        self._round_label = h2("")
        self._round_chip = Chip("", Palette.primary)
        self._score_chip = Chip("", Palette.success)
        header.addWidget(self._round_label)
        header.addWidget(self._score_chip)
        header.addStretch(1)
        header.addWidget(self._round_chip)
        stage_layout.addLayout(header)

        samples = QtWidgets.QHBoxLayout()
        samples.setSpacing(10)
        self._button_a = QtWidgets.QPushButton(tr("player.sample", key="A"))
        self._button_a.setProperty("variant", "sampleA")
        self._button_b = QtWidgets.QPushButton(tr("player.sample", key="B"))
        self._button_b.setProperty("variant", "sampleB")
        for button in (self._button_a, self._button_b):
            button.setCheckable(True)
            button.setMinimumHeight(64)
            button.setMinimumWidth(140)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        samples.addWidget(self._button_a)
        samples.addWidget(self._button_b)
        stage_layout.addLayout(samples)

        self._switch_hint = QtWidgets.QLabel(tr("player.switching"))
        self._switch_hint.setProperty("role", "hint")
        self._switch_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        stage_layout.addWidget(self._switch_hint)

        # transport
        transport = QtWidgets.QHBoxLayout()
        transport.setSpacing(8)
        self._play_button = QtWidgets.QPushButton()
        self._play_button.setIcon(make_icon("play", Palette.text, 18))
        self._play_button.setProperty("variant", "primary")
        self._play_button.setMinimumWidth(120)
        self._play_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_button = QtWidgets.QPushButton()
        self._stop_button.setIcon(make_icon("stop", Palette.text_dim, 18))
        self._stop_button.setProperty("variant", "ghost")
        self._stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._position = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self._position.setRange(0, 1000)
        self._position.setEnabled(False)
        self._time_label = mono("0:00 / 0:00")
        self._time_label.setMinimumWidth(110)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loop_button = QtWidgets.QPushButton()
        self._loop_button.setIcon(make_icon("loop", Palette.text_dim, 18))
        self._loop_button.setCheckable(True)
        self._loop_button.setChecked(True)
        self._loop_button.setProperty("variant", "ghost")
        self._loop_button.setToolTip(tr("player.loop"))
        self._loop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_button = QtWidgets.QPushButton()
        self._mute_button.setIcon(make_icon("volume", Palette.text_dim, 18))
        self._mute_button.setCheckable(True)
        self._mute_button.setProperty("variant", "ghost")
        self._mute_button.setToolTip(tr("player.mute"))
        self._mute_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._volume = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self._volume.setToolTip(tr("player.volume"))
        self._volume.setRange(0, 100)
        self._volume.setValue(int(self.settings.volume * 100))
        self._volume.setFixedWidth(96)
        for widget in (
            self._play_button,
            self._stop_button,
            self._loop_button,
            self._mute_button,
            self._volume,
        ):
            transport.addWidget(widget)
        transport.addWidget(self._position, 1)
        transport.addWidget(self._time_label)
        stage_layout.addLayout(transport)

        small = QtWidgets.QHBoxLayout()
        small.setSpacing(8)
        self._backend_combo = QtWidgets.QComboBox()
        self._backend_combo.setToolTip(tr("player.backend"))
        self._backend_combo.setMinimumWidth(220)
        self._backend_note = QtWidgets.QLabel("")
        self._backend_note.setProperty("role", "hint")
        small.addWidget(self._backend_combo)
        small.addWidget(self._backend_note)
        small.addStretch(1)
        stage_layout.addLayout(small)
        layout.addWidget(stage)

        # -- waveform --------------------------------------------------
        wave_card = card()
        wave_layout = wave_card.layout()
        assert isinstance(wave_layout, QtWidgets.QVBoxLayout)
        wave_layout.setContentsMargins(12, 12, 12, 12)
        wave_layout.setSpacing(8)
        wave_header = QtWidgets.QHBoxLayout()
        self._wave_title = section_label(tr("player.view"))
        self._wave_toggle = QtWidgets.QComboBox()
        self._wave_toggle.addItem("", "wave")
        self._wave_toggle.addItem("", "diff")
        self._wave_toggle.setToolTip(tr("player.view.diff.tip"))
        self._wave_toggle.setCurrentIndex(1 if self._show_difference else 0)
        self._wave_toggle.setFixedWidth(150)
        wave_header.addWidget(self._wave_title)
        wave_header.addStretch(1)
        wave_header.addWidget(self._wave_toggle)
        wave_layout.addLayout(wave_header)

        self._waveform = WaveformView()
        self._waveform.set_empty_text(tr("app.idle"))
        wave_layout.addWidget(self._waveform, 1)
        self._wave_hint = QtWidgets.QLabel(tr("player.seek_hint"))
        self._wave_hint.setProperty("role", "hint")
        wave_layout.addWidget(self._wave_hint)
        layout.addWidget(wave_card, 1)

        # -- vote ------------------------------------------------------
        vote_card = card()
        vote_layout = vote_card.layout()
        assert isinstance(vote_layout, QtWidgets.QVBoxLayout)
        vote_layout.setContentsMargins(16, 14, 16, 14)
        vote_layout.setSpacing(10)
        self._vote_question = QtWidgets.QLabel(tr("vote.title"))
        font = scaled_font(self._vote_question.font(), delta_pt=1.0)
        font.setBold(True)
        self._vote_question.setFont(font)
        vote_layout.addWidget(self._vote_question)

        votes = QtWidgets.QHBoxLayout()
        votes.setSpacing(10)
        self._vote_a = QtWidgets.QPushButton(tr("vote.a"))
        self._vote_a.setProperty("variant", "voteA")
        self._vote_b = QtWidgets.QPushButton(tr("vote.b"))
        self._vote_b.setProperty("variant", "voteB")
        for button in (self._vote_a, self._vote_b):
            button.setMinimumHeight(56)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(False)
            votes.addWidget(button)
        vote_layout.addLayout(votes)

        self._vote_hint = QtWidgets.QLabel(tr("vote.shortcut"))
        self._vote_hint.setProperty("role", "hint")
        self._vote_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vote_layout.addWidget(self._vote_hint)

        feedback = QtWidgets.QHBoxLayout()
        self._feedback = QtWidgets.QLabel("")
        self._feedback.setProperty("role", "hint")
        self._feedback_key = "session.start_playing"
        self._feedback.setWordWrap(True)
        self._next_button = QtWidgets.QPushButton(tr("session.next"))
        self._next_button.setProperty("variant", "primary")
        self._next_button.setMinimumHeight(38)
        self._next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_button.setVisible(False)
        self._skip_button = QtWidgets.QPushButton(tr("session.skip"))
        self._skip_button.setProperty("variant", "ghost")
        self._skip_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skip_button.setVisible(False)
        feedback.addWidget(self._feedback, 1)
        feedback.addWidget(self._skip_button)
        feedback.addWidget(self._next_button)
        vote_layout.addLayout(feedback)
        layout.addWidget(vote_card)

        # Notices (errors, warnings) sit between the waveform and the vote so
        # they are impossible to miss but never cover the controls.
        self._notice.setParent(panel)
        layout.insertWidget(layout.count() - 1, self._notice)
        return panel

    def _build_right_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        self._results = ResultsPanel()
        self._results.exportRequested.connect(self._on_export)
        layout.addWidget(self._results)
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(430)
        return panel

    def _build_menus(self) -> None:
        bar = self.menuBar()
        self._menu_file = bar.addMenu("")
        self._action_open = QtGui.QAction(self)
        self._action_open.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        self._action_open.triggered.connect(self._on_open)
        self._menu_file.addAction(self._action_open)
        self._recent_menu = self._menu_file.addMenu("")
        self._menu_file.addSeparator()
        self._action_export = QtGui.QAction(self)
        self._action_export.setShortcut(QtGui.QKeySequence("Ctrl+E"))
        self._action_export.triggered.connect(self._on_export)
        self._menu_file.addAction(self._action_export)
        self._menu_file.addSeparator()
        self._action_quit = QtGui.QAction(self)
        self._action_quit.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        self._action_quit.triggered.connect(self.close)
        self._menu_file.addAction(self._action_quit)

        self._menu_session = bar.addMenu("")
        self._action_new = QtGui.QAction(self)
        self._action_new.setShortcut(QtGui.QKeySequence("Ctrl+N"))
        self._action_new.triggered.connect(self._on_new_session)
        self._menu_session.addAction(self._action_new)
        self._action_finish = QtGui.QAction(self)
        self._action_finish.setShortcut(QtGui.QKeySequence("Ctrl+Return"))
        self._action_finish.triggered.connect(self._on_finish)
        self._menu_session.addAction(self._action_finish)

        self._menu_view = bar.addMenu("")
        self._language_group: list[QtGui.QAction] = []
        for code, label in Translator.available():
            action = QtGui.QAction(label, self)
            action.setCheckable(True)
            action.setData(code)
            action.triggered.connect(lambda _checked=False, c=code: self._set_language(c))
            self._menu_view.addAction(action)
            self._language_group.append(action)
        self._menu_view.addSeparator()
        self._action_difference = QtGui.QAction(self)
        self._action_difference.setCheckable(True)
        self._action_difference.triggered.connect(self._on_toggle_difference)
        self._menu_view.addAction(self._action_difference)

        self._menu_help = bar.addMenu("")
        self._action_shortcuts = QtGui.QAction(self)
        self._action_shortcuts.setShortcut(QtGui.QKeySequence("F1"))
        self._action_shortcuts.triggered.connect(self._show_shortcuts)
        self._menu_help.addAction(self._action_shortcuts)
        self._action_about = QtGui.QAction(self)
        self._action_about.triggered.connect(self._show_about)
        self._menu_help.addAction(self._action_about)

    def _build_shortcuts(self) -> None:
        self._shortcuts: dict[str, QtGui.QShortcut] = {}

        def add(key: str, sequence: QtGui.QKeySequence, handler: Any) -> None:
            shortcut = QtGui.QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts[key] = shortcut

        add("space", QtGui.QKeySequence(Qt.Key.Key_Space), self._on_play_pause)
        add("a", QtGui.QKeySequence(Qt.Key.Key_A), lambda: self._on_play_sample(SampleKey.A))
        add("b", QtGui.QKeySequence(Qt.Key.Key_B), lambda: self._on_play_sample(SampleKey.B))
        add("vote_a", QtGui.QKeySequence(Qt.Key.Key_1), lambda: self._on_vote(SampleKey.A))
        add("vote_b", QtGui.QKeySequence(Qt.Key.Key_2), lambda: self._on_vote(SampleKey.B))
        add("left", QtGui.QKeySequence(Qt.Key.Key_Left), lambda: self._on_seek(-1))
        add("right", QtGui.QKeySequence(Qt.Key.Key_Right), lambda: self._on_seek(1))
        add("esc", QtGui.QKeySequence(Qt.Key.Key_Escape), self._on_escape)
        add("next", QtGui.QKeySequence(Qt.Key.Key_N), self._on_next)
        add("mute", QtGui.QKeySequence(Qt.Key.Key_M), self._on_mute)

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        self._status_label = QtWidgets.QLabel(tr("app.idle"))
        self._ffmpeg_chip = Chip("", Palette.text_dim)
        self._temp_chip = Chip("", Palette.text_faint)
        self._temp_chip.setToolTip("")
        self._delete_temp = QtWidgets.QCheckBox(tr("settings.delete_temp"))
        self._delete_temp.setToolTip(tr("settings.delete_temp.tip"))
        self._delete_temp.setChecked(self.settings.delete_temp_on_exit)
        self._delete_temp.toggled.connect(self._on_delete_temp_toggled)
        bar.addWidget(self._status_label, 1)
        bar.addPermanentWidget(self._ffmpeg_chip)
        bar.addPermanentWidget(self._delete_temp)
        bar.addPermanentWidget(self._temp_chip)

    def _connect_signals(self) -> None:
        self._card_a.browseRequested.connect(lambda: self._on_browse(SampleKey.A))
        self._card_b.browseRequested.connect(lambda: self._on_browse(SampleKey.B))
        self._card_a.fileDropped.connect(lambda path: self._on_drop(path, SampleKey.A))
        self._card_b.fileDropped.connect(lambda path: self._on_drop(path, SampleKey.B))
        self._card_a.clearRequested.connect(lambda: self._on_clear(SampleKey.A))
        self._card_b.clearRequested.connect(lambda: self._on_clear(SampleKey.B))

        self._prepare_button.clicked.connect(self._on_prepare)
        self._new_session.clicked.connect(self._on_new_session)
        self._next_button.clicked.connect(self._on_next)
        self._skip_button.clicked.connect(self._on_skip)
        self._button_a.clicked.connect(lambda: self._on_play_sample(SampleKey.A))
        self._button_b.clicked.connect(lambda: self._on_play_sample(SampleKey.B))
        self._vote_a.clicked.connect(lambda: self._on_vote(SampleKey.A))
        self._vote_b.clicked.connect(lambda: self._on_vote(SampleKey.B))
        self._play_button.clicked.connect(self._on_play_pause)
        self._stop_button.clicked.connect(self._on_stop)
        self._mute_button.toggled.connect(self.controller.player.set_muted)
        self._loop_button.toggled.connect(self.controller.player.set_loop)
        self._volume.valueChanged.connect(
            lambda value: self.controller.player.set_volume(value / 100.0)
        )
        self._position.sliderReleased.connect(self._on_slider_released)
        self._position.valueChanged.connect(self._on_slider_moved)
        self._waveform.seekRequested.connect(self._on_waveform_seek)
        self._waveform.hovered.connect(self._on_waveform_hover)
        self._wave_toggle.currentIndexChanged.connect(self._on_wave_mode)

        self._loudness_check.toggled.connect(self._on_processing_changed)
        for spin in (self._lufs, self._true_peak, self._lra):
            spin.valueChanged.connect(self._on_processing_changed)
        self._rate.currentIndexChanged.connect(self._on_processing_changed)
        self._reveal_check.toggled.connect(self._on_session_settings_changed)
        self._require_both.toggled.connect(self._on_session_settings_changed)
        self._auto_advance.toggled.connect(self._on_session_settings_changed)
        self._seed.valueChanged.connect(self._on_session_settings_changed)
        for value, button in self._round_buttons.items():
            button.clicked.connect(lambda _checked=False, v=value: self._on_rounds_changed(v))
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)

        player = self.controller.player
        player.positionChanged.connect(self._on_position)
        player.durationChanged.connect(self._on_duration)
        player.stateChanged.connect(self._on_playing)
        player.activeChanged.connect(self._on_active)
        player.errorOccurred.connect(self._on_player_error)
        player.backendChanged.connect(lambda _key: self._sync_backend_combo())

    # =====================================================================
    #  State
    # =====================================================================
    def _load_persisted_state(self) -> None:
        if self.settings.geometry:
            try:
                self.restoreGeometry(
                    QtCore.QByteArray.fromBase64(self.settings.geometry.encode("ascii"))
                )
            except (ValueError, TypeError):  # pragma: no cover - corrupted state
                pass

        self._card_a.set_path(self.settings.lossless_path or None)
        self._card_b.set_path(self.settings.lossy_path or None)
        self._loudness_check.setChecked(self.settings.normalize)
        self._lufs.setValue(self.settings.target_lufs)
        self._true_peak.setValue(self.settings.true_peak_db)
        self._lra.setMinimumWidth(132)
        self._lra.setValue(self.settings.lra)
        self._reveal_check.setChecked(self.settings.reveal_after_vote)
        self._require_both.setChecked(self.settings.require_both_heard)
        self._auto_advance.setChecked(self.settings.auto_advance)
        self._seed.setValue(self.settings.seed or 0)
        self._delete_temp.setChecked(self.settings.delete_temp_on_exit)
        button = self._round_buttons.get(self.settings.rounds)
        if button is not None:
            button.setChecked(True)
        self._wave_toggle.setCurrentIndex(1 if self._show_difference else 0)
        self._action_difference.setChecked(self._show_difference)

        self._sync_backend_combo()
        self._sync_status_chips()
        self._rebuild_recent_menu()
        self.controller.player.set_loop(self.settings.loop)
        self.controller.player.set_volume(self.settings.volume)
        self.controller.player.set_muted(False)

        # Probe the remembered files in the background so the cards fill in.
        pending = [p for p in (self.settings.lossless_path, self.settings.lossy_path) if p]
        if pending:
            self._queue_probe(pending)
        removed = self.controller.sweep_temporaries()
        if removed:
            self._temp_chip.setText(tr("msg.temp_cleaned", n=len(removed)))

    def _sync_status_chips(self) -> None:
        ffmpeg = self.controller.ffmpeg
        self._ffmpeg_chip.setText(
            f"ffmpeg {'✓' if ffmpeg.available else '✗'} · "
            f"ffprobe {'✓' if ffmpeg.has_ffprobe else '✗'}"
        )
        self._ffmpeg_chip.set_accent(Palette.success if ffmpeg.available else Palette.danger)
        self._ffmpeg_chip.setToolTip(ffmpeg.version)
        workspace = self.controller.workspace
        if workspace is not None and workspace.path.exists():
            size = workspace.size_bytes / (1024 * 1024)
            self._temp_chip.setText(f"{workspace.path.name} · {size:.1f} MB")
            self._temp_chip.setToolTip(str(workspace.path))
            self._temp_chip.set_accent(Palette.text_dim)
        else:
            self._temp_chip.setText("")
            self._temp_chip.setToolTip("")

    def _queue_probe(self, paths: list[str]) -> None:
        from .workers import ProbeWorker

        if self._probe_worker is not None and self._probe_worker.isRunning():
            self._probe_queue.extend(paths)
            return
        self._probe_worker = ProbeWorker(self.controller.ffmpeg, paths)
        self._probe_worker.probed.connect(self._on_probed)
        self._probe_worker.failed.connect(self._on_probe_failed)
        self._probe_worker.finished.connect(self._drain_probe_queue)
        self._probe_worker.start()

    def _drain_probe_queue(self) -> None:
        if self._probe_queue:
            remaining, self._probe_queue = self._probe_queue, []
            self._queue_probe(remaining)

    @safe_slot
    def _on_probed(self, path: str, info: object) -> None:
        if path == self.settings.lossless_path:
            self._card_a.set_probe(info)  # type: ignore[arg-type]
            duration = max(0.0, getattr(info, "duration_sec", 0.0))  # type: ignore[attr-defined]
        else:
            self._card_b.set_probe(info)  # type: ignore[arg-type]
            duration = max(0.0, getattr(info, "duration_sec", 0.0))  # type: ignore[attr-defined]
        self._segment_editor.set_source_duration(duration)
        self._update_dependent_labels()

    @safe_slot
    def _on_probe_failed(self, path: str, message: str) -> None:
        if path == self.settings.lossless_path:
            self._card_a.set_status(message, "error")
        else:
            self._card_b.set_status(message, "error")

    # =====================================================================
    #  User intents: sources
    # =====================================================================
    @safe_slot
    def _on_browse(self, key: SampleKey) -> None:
        start = self.settings.recent[0] if self.settings.recent else str(Path.home())
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("menu.open"), start, _file_filter()
        )
        if path:
            self._assign(key, path)

    @safe_slot
    def _on_drop(self, path: str, key: SampleKey | None) -> None:
        if key is None:
            key = SampleKey.A if not self.settings.lossless_path else SampleKey.B
        self._assign(key, path)

    @safe_slot
    def _on_clear(self, key: SampleKey) -> None:
        self._assign(key, None)

    @safe_slot
    def _on_swap(self) -> None:
        self.controller.swap_sources()
        self._card_a.set_path(self.settings.lossless_path or None)
        self._card_b.set_path(self.settings.lossy_path or None)
        self._apply_state()

    def _assign(self, key: SampleKey, path: str | None) -> None:
        other = SampleKey.B if key is SampleKey.A else SampleKey.A
        other_path = (
            self.settings.lossy_path if key is SampleKey.A else self.settings.lossless_path
        )
        if path and other_path and Path(path) == Path(other_path):
            self._notice.show_warning(tr("sources.same_file"))
            return
        self.controller.set_source(key, path)
        card_widget = self._card_a if key is SampleKey.A else self._card_b
        card_widget.set_path(path)
        if path:
            self._queue_probe([path])
            self._rebuild_recent_menu()
        self._notice.hide()
        self._apply_state()
        self._update_dependent_labels()

    def _rebuild_recent_menu(self) -> None:
        self._recent_menu.clear()
        self._recent_menu.setTitle(tr("sources.recent"))
        self._recent_menu.setEnabled(bool(self.settings.recent))
        for path in self.settings.recent:
            action = QtGui.QAction(path, self)
            action.triggered.connect(lambda _checked=False, p=path: self._assign_recent(p))
            self._recent_menu.addAction(action)

    def _assign_recent(self, path: str) -> None:
        key = SampleKey.A if not self.settings.lossless_path else SampleKey.B
        if Path(path) == Path(self.settings.lossless_path or ""):
            key = SampleKey.A
        elif Path(path) == Path(self.settings.lossy_path or ""):
            key = SampleKey.B
        elif key is SampleKey.A and self.settings.lossy_path:
            key = SampleKey.B
        self._assign(key, path)

    @safe_slot
    def _on_open(self) -> None:
        start = self.settings.recent[0] if self.settings.recent else str(Path.home())
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, tr("menu.open"), start, _file_filter()
        )
        if not paths:
            return
        if len(paths) == 1:
            key = SampleKey.A if not self.settings.lossless_path else SampleKey.B
            self._assign(key, paths[0])
        else:
            self._assign(SampleKey.A, paths[0])
            self._assign(SampleKey.B, paths[1])

    # =====================================================================
    #  User intents: preparation
    # =====================================================================
    @safe_slot
    def _on_prepare(self) -> None:
        if self.controller.preparing:
            self._on_cancel()
            return
        started, reason = self.controller.start_preparation()
        if not started:
            if reason == "sources":
                self._notice.show_warning(tr("msg.files_missing"))
            elif reason:
                self._notice.show_error(tr("msg.ffmpeg_missing"), reason)
            return
        worker = self.controller.worker
        assert worker is not None
        worker.progress.connect(self._on_prepare_progress)
        worker.prepared.connect(self._on_prepare_done)
        worker.failed.connect(self._on_prepare_failed)
        worker.cancelled.connect(self._on_prepare_cancelled)
        worker.finished.connect(self._on_prepare_finished)
        # Connections are complete before the thread starts, so no progress
        # signal can be missed.
        worker.start()
        self._progress.setValue(0)
        self._progress.show()
        self._status_label.setText(tr("msg.preparing"))
        self._apply_state()

    @safe_slot
    def _on_cancel(self) -> None:
        self.controller.cancel_preparation()

    def _on_prepare_progress(self, fraction: float, label: str) -> None:
        self._progress.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        self._stage_label.setText(label)

    @safe_slot
    def _on_prepare_done(self, result: object) -> None:
        pair = getattr(result, "pair", result)
        self.controller.on_prepared(result)
        duration = pair.duration_sec  # type: ignore[attr-defined]
        self._segment_editor.set_source_duration(duration)
        self._segment_editor.set_peaks(pair.sample_a.peaks)  # type: ignore[attr-defined]
        self._refresh_waveform()
        self._update_level_hints()
        self._sync_status_chips()
        warnings = list(getattr(result, "warnings", ()) or ())
        for warning in warnings:
            self._notice.show_warning(warning)
        if not warnings:
            self._status_label.setText(tr("msg.ready"))
        self._round_chip.setText("")
        self._update_round_label()
        self._set_feedback("session.start_playing")
        self._apply_state()

    @safe_slot
    def _on_prepare_failed(self, message: str, hint: str) -> None:
        self.controller.on_prepare_failed(message, hint)
        self._notice.show_error(message, hint)
        self._status_label.setText(message)
        self._apply_state()

    @safe_slot
    def _on_prepare_cancelled(self) -> None:
        self.controller.on_prepare_cancelled()
        self._status_label.setText(tr("app.ready"))
        self._apply_state()

    @safe_slot
    def _on_prepare_finished(self) -> None:
        self._progress.hide()
        self._stage_label.setText("")
        self._cancel_button.setEnabled(False)
        self._apply_state()

    # =====================================================================
    #  User intents: playback
    # =====================================================================
    @safe_slot
    def _on_play_sample(self, key: SampleKey) -> None:
        if self.controller.pair is None:
            if self.controller.sources_ready():
                self._on_prepare()
            else:
                self._notice.show_warning(tr("msg.files_missing"))
            return
        if self.controller.player.is_playing and self.controller.player.active is key:
            self.controller.player.pause()
            return
        self.controller.play(key)
        self._on_active(key)

    @safe_slot
    def _on_play_pause(self) -> None:
        self.controller.toggle()
        if self.controller.player.active is not None:
            self._on_active(self.controller.player.active)

    @safe_slot
    def _on_stop(self) -> None:
        self.controller.stop_playback()

    @safe_slot
    def _on_escape(self) -> None:
        self.controller.player.pause()

    @safe_slot
    def _on_mute(self) -> None:
        self.controller.player.set_muted(not self.controller.player.muted)

    @safe_slot
    def _on_seek(self, direction: int) -> None:
        step = 15000 if QtWidgets.QApplication.keyboardModifiers() & (
            Qt.KeyboardModifier.ShiftModifier
        ) else 5000
        self.controller.player.nudge(direction * step)

    def _on_slider_moved(self, value: int) -> None:
        if self.controller.player.duration_ms:
            self._waveform.set_position(value / 1000.0)

    @safe_slot
    def _on_slider_released(self) -> None:
        if self.controller.player.duration_ms:
            self.controller.player.seek_fraction(self._position.value() / 1000.0)

    @safe_slot
    def _on_waveform_seek(self, fraction: float) -> None:
        self.controller.player.seek_fraction(fraction)

    def _on_waveform_hover(self, fraction: float | None) -> None:
        duration = self.controller.player.duration_ms
        if fraction is None or not duration:
            self._time_label.setToolTip("")
            return
        self._time_label.setToolTip(format_position(int(fraction * duration), duration))

    @safe_slot
    def _on_wave_mode(self, index: int) -> None:
        self._show_difference = index == 1
        self.settings.show_difference_view = self._show_difference
        self._action_difference.setChecked(self._show_difference)
        self._refresh_waveform()

    @safe_slot
    def _on_toggle_difference(self) -> None:
        self._wave_toggle.setCurrentIndex(1 if self._action_difference.isChecked() else 0)

    @safe_slot
    def _on_position(self, position_ms: int) -> None:
        duration = self.controller.player.duration_ms
        if duration:
            self._waveform.set_position(position_ms / duration)
            if not self._position.isSliderDown():
                self._position.setValue(int(position_ms / duration * 1000))
        self._time_label.setText(format_position(position_ms, duration))

    @safe_slot
    def _on_duration(self, duration_ms: int) -> None:
        self._position.setEnabled(bool(duration_ms))
        self._time_label.setText(format_position(0, duration_ms))
        self._refresh_waveform()

    @safe_slot
    def _on_playing(self, playing: bool) -> None:
        icon = "pause" if playing else "play"
        self._play_button.setIcon(make_icon(icon, Palette.text, 18))
        for button, key in ((self._button_a, SampleKey.A), (self._button_b, SampleKey.B)):
            button.setChecked(playing and self.controller.player.active is key)
        self._update_dependent_labels()

    @safe_slot
    def _on_active(self, key: SampleKey) -> None:
        self._button_a.setChecked(self.controller.player.active is SampleKey.A)
        self._button_b.setChecked(self.controller.player.active is SampleKey.B)
        self._refresh_waveform()

    @safe_slot
    def _on_player_error(self, message: str) -> None:
        self._notice.show_error(message)

    @safe_slot
    def _on_backend_changed(self, index: int) -> None:
        key = self._backend_combo.itemData(index)
        if not key or key == self.controller.player.backend_key:
            self._sync_backend_combo()
            return
        self.settings.backend = key
        self.controller.player.select_backend(key)
        self._sync_backend_combo()

    def _sync_backend_combo(self) -> None:
        current = self.controller.player.backend_key
        self._backend_combo.blockSignals(True)
        self._backend_combo.clear()
        self._backend_combo.addItem(tr("player.backend.auto"), "auto")
        for info in available_backends():
            label = tr(f"player.backend.{info.key}")
            if not info.available:
                label = f"{label} — {info.detail}"
            self._backend_combo.addItem(label, info.key)
            if not info.available:
                model = self._backend_combo.model()
                if model is not None:
                    item = model.item(self._backend_combo.count() - 1)
                    if item is not None:
                        item.setEnabled(False)
        index = self._backend_combo.findData(current)
        if index < 0:
            index = 0
        self._backend_combo.setCurrentIndex(index)
        self._backend_combo.blockSignals(False)
        note = self.controller.player.capabilities()
        self._backend_note.setText(
            f"{tr(f'player.backend.{self.controller.player.backend_key}')}"
            + (f" · {tr('player.sample_accurate')}" if note == "sample-accurate" else "")
            + (f" · {tr('player.reduced')}" if note == "no seek" else "")
        )

    # =====================================================================
    #  User intents: voting
    # =====================================================================
    def _set_feedback(self, key: str, **kwargs: object) -> None:
        """Show a translatable hint and remember the key for retranslate()."""
        self._feedback_key = key
        self._feedback.setText(tr(key, **kwargs))

    @safe_slot
    def _on_vote(self, guess: SampleKey) -> None:
        if self.controller.pair is None:
            return
        allowed, reason = self.controller.can_vote()
        if not allowed:
            self._set_feedback(
                {
                    "need-both": "vote.need_both",
                    "need-plays": "vote.need_plays",
                    "already-voted": "vote.already",
                    "no-round": "results.no_data",
                }.get(reason, "vote.need_both")
            )
            return
        record = self.controller.vote(guess)
        self._history.append(record)
        self._results.set_stats(
            self.controller.stats(), self.controller.records(), endless=self.settings.rounds <= 0
        )
        self._score_chip.setText(
            f"{self.controller.stats().correct}/{self.controller.stats().rounds}"
        )
        accent = Palette.success if record.correct else Palette.danger
        if self.settings.reveal_after_vote:
            verdict_text = tr("vote.correct") if record.correct else tr("vote.wrong")
            self._feedback.setText(
                f"{verdict_text} — {tr('vote.truth', key=record.lossless_key.value)}"
            )
        else:
            self._feedback.setText(tr("vote.already"))
        self._feedback_key = ""
        self._feedback.setStyleSheet(f"color: {accent}; font-weight: 600;")
        self._next_button.setVisible(True)
        self._skip_button.setVisible(False)
        self._update_round_label()
        self._apply_state()
        if self.settings.auto_advance and not self.controller.session_finished():
            # A short beat so the reveal is readable before the next round.
            QtCore.QTimer.singleShot(900, self._auto_next)

    @safe_slot
    def _auto_next(self) -> None:
        """Advance automatically a moment after a vote.

        Guarded on the state, not just on the button: the user may have already
        pressed *Next* by hand, and a stale timer must not skip a round.
        """
        if self._destroyed or not self.settings.auto_advance:
            return
        if self.controller.state == AppState.VOTED:
            self._on_next()

    @safe_slot
    def _on_next(self) -> None:
        self.controller.next_round()
        self._set_feedback("session.start_playing")
        self._feedback.setStyleSheet("")
        self._next_button.setVisible(False)
        self._update_round_label()
        self._apply_state()

    @safe_slot
    def _on_skip(self) -> None:
        self.controller.skip_round()
        self._update_round_label()
        self._apply_state()

    @safe_slot
    def _on_finish(self) -> None:
        self.controller.finish()
        self._show_report()
        self._apply_state()

    @safe_slot
    def _on_new_session(self) -> None:
        self.controller.new_session()
        self._history.clear()
        self._set_feedback("session.start_playing")
        self._feedback.setStyleSheet("")
        self._next_button.setVisible(False)
        self._results.set_stats(self.controller.stats(), (), endless=self.settings.rounds <= 0)
        self._score_chip.setText("")
        self._update_round_label()
        self._apply_state()

    # =====================================================================
    #  Settings intents
    # =====================================================================
    @safe_slot
    def _on_processing_changed(self, *_args: object) -> None:
        self.settings.normalize = self._loudness_check.isChecked()
        self.settings.target_lufs = self._lufs.value()
        self.settings.true_peak_db = self._true_peak.value()
        self.settings.lra = self._lra.value()
        self.settings.sample_rate = int(self._rate.currentData() or 44100)
        enabled = self._loudness_check.isChecked()
        for widget in (self._lufs, self._true_peak, self._lra):
            widget.setEnabled(enabled)
        self._prepare_button.setText(
            tr("prepare.reprepare") if self.controller.pair is not None else tr("prepare.start")
        )
        self._apply_state()

    @safe_slot
    def _on_segment_changed(self, segment: SegmentSpec) -> None:
        self._segment = segment
        self.settings.apply_segment(segment)
        if self.controller.pair is not None:
            self._show_stale_notice()

    @safe_slot
    def _on_session_settings_changed(self, *_args: object) -> None:
        self.settings.reveal_after_vote = self._reveal_check.isChecked()
        self.settings.require_both_heard = self._require_both.isChecked()
        self.settings.auto_advance = self._auto_advance.isChecked()
        self.settings.seed = self._seed.value() or None

    @safe_slot
    def _on_rounds_changed(self, value: int) -> None:
        self.settings.rounds = value
        if self.controller.pair is not None:
            self._on_new_session()

    @safe_slot
    def _on_delete_temp_toggled(self, checked: bool) -> None:
        self.settings.delete_temp_on_exit = checked

    @safe_slot
    def _set_language(self, code: str) -> None:
        self.settings.language = code
        Translator.instance().set_language(code)

    # =====================================================================
    #  Enabling / disabling
    # =====================================================================
    def _apply_state(self) -> None:
        state = self.controller.state
        preparing = self.controller.preparing
        has_pair = self.controller.pair is not None
        sources = self.controller.sources_ready()

        self._prepare_button.setText(
            tr("prepare.busy") if preparing
            else (tr("prepare.reprepare") if has_pair else tr("prepare.start"))
        )
        self._prepare_button.setEnabled(not preparing and (sources or has_pair))
        self._cancel_button.setEnabled(preparing)
        for widget in (self._card_a, self._card_b):
            widget.set_busy(preparing)

        can_play = has_pair and not preparing
        self._button_a.setEnabled(can_play)
        self._button_b.setEnabled(can_play)
        self._play_button.setEnabled(can_play)
        self._stop_button.setEnabled(can_play)
        self._position.setEnabled(can_play and bool(self.controller.player.duration_ms))

        allowed, reason = self.controller.can_vote()
        votable = bool(allowed and state in (AppState.ROUND, AppState.READY) and not preparing)
        self._vote_a.setEnabled(votable)
        self._vote_b.setEnabled(votable)
        self._new_session.setEnabled(has_pair and not preparing)
        self._action_finish.setEnabled(has_pair and state not in (AppState.FINISHED, AppState.IDLE))
        self._skip_button.setVisible(state == AppState.ROUND and has_pair)

        if state == AppState.IDLE:
            self._status_label.setText(tr("app.idle"))
        elif state == AppState.PREPARING:
            self._status_label.setText(tr("msg.preparing"))
        elif state == AppState.FINISHED:
            self._status_label.setText(self._results.verdict_text())
        elif has_pair:
            self._status_label.setText(tr("app.ready"))
        if reason in ("need-both", "need-plays") and state == AppState.ROUND:
            self._vote_a.setToolTip(tr("vote.need_both" if reason == "need-both" else "vote.need_plays"))
            self._vote_b.setToolTip(self._vote_a.toolTip())
        else:
            self._vote_a.setToolTip("")
            self._vote_b.setToolTip("")

        self._update_round_label()
        self._update_dependent_labels()

    def _update_round_label(self) -> None:
        session = self.controller.session
        if session is None:
            self._round_label.setText("")
            self._round_chip.setVisible(False)
            self._score_chip.setVisible(False)
            return
        total = self.settings.rounds
        if total <= 0:
            self._round_label.setText(tr("session.round_endless", current=session.round_number))
        else:
            self._round_label.setText(
                tr("session.round", current=session.round_number, total=total)
            )
        stats = self.controller.stats()
        self._results.set_stats(stats, session.records, endless=total <= 0)
        self._round_chip.setVisible(True)
        self._round_chip.setText(tr("session.rounds.endless") if total <= 0 else f"{total}")
        if stats.rounds:
            self._score_chip.setText(f"{stats.correct}/{stats.rounds}")
        self._score_chip.setVisible(bool(self._score_chip.text()))

    def _update_dependent_labels(self) -> None:
        """Enable/disable things that depend on the current playback state."""
        loaded = self.controller.pair is not None
        self._wave_toggle.setEnabled(loaded)
        difference_available = bool(
            loaded and self.controller.pair is not None and self.controller.pair.difference_peaks()
        )
        index = self._wave_toggle.findData("diff")
        if index >= 0:
            model = self._wave_toggle.model()
            item = model.item(index) if model is not None else None
            if item is not None:
                item.setEnabled(difference_available)
            if not difference_available and self._wave_toggle.currentIndex() == index:
                self._wave_toggle.setCurrentIndex(0)

    def _update_level_hints(self) -> None:
        pair = self.controller.pair
        if pair is None:
            self._level_hint.setText("")
            return
        lines = [
            f"A · {pair.sample_a.stats.summary}",
            f"B · {pair.sample_b.stats.summary}",
        ]
        if pair.loudness_delta_db:
            lines.append(f"Δ loudness: {pair.loudness_delta_db:.2f} LU")
        if pair.duration_mismatch_sec > 0.05:
            lines.append(f"Δ duration: {pair.duration_mismatch_sec:.2f} s")
        self._level_hint.setText("\n".join(lines))

    def _refresh_waveform(self) -> None:
        pair = self.controller.pair
        if pair is None:
            self._waveform.clear()
            return
        if self._show_difference:
            peaks = pair.difference_peaks()
            self._waveform.set_peaks(peaks, color=Palette.primary, duration=pair.duration_sec)
        else:
            active = self.controller.player.active or SampleKey.A
            sample = pair.get(active)
            color = Palette.sample_a if active is SampleKey.A else Palette.sample_b
            self._waveform.set_peaks(sample.peaks, color=color, duration=pair.duration_sec)
        self._waveform.set_position(
            self.controller.player.position_ms / max(1, self.controller.player.duration_ms)
        )

    def _show_stale_notice(self) -> None:
        """Settings changed after the audio was built: the pair is now stale."""
        self._notice.show_info(tr("prepare.reprepare"))

    # =====================================================================
    #  Dialogs
    # =====================================================================
    def _show_report(self) -> None:
        report = self.controller.build_report()
        if report is None:
            return
        self._last_report = report
        self._results.set_stats(report.stats, report.records, endless=report.endless)
        reading = self.controller.verdict()
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(tr("results.title"))
        box.setIcon(
            QtWidgets.QMessageBox.Icon.Information
            if reading.level in ("significant", "strong")
            else QtWidgets.QMessageBox.Icon.Question
        )
        level = reading.level
        box.setText(f"{report.accuracy}  ·  {tr(f'results.verdict.{level}')}")
        box.setInformativeText(
            f"p = {fmt_p_value(report.stats.p_value)}\n\n"
            + tr(f"results.detail.{level}", pct=f"{report.stats.p_value * 100:.1f}")
        )
        box.setStandardButtons(
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Save
        )
        button = box.button(QtWidgets.QMessageBox.StandardButton.Save)
        if button is not None:
            button.setText(tr("results.export"))
        box.exec()
        if box.clickedButton() is button:
            self._on_export()

    @safe_slot
    def _on_export(self) -> None:
        report = self._last_report or self.controller.build_report()
        if report is None or not report.records:
            self._notice.show_warning(tr("results.no_data"))
            return
        suggested = str(Path.home() / f"flacblind-{report.correct}-{report.total}.json")
        path, selected = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("results.export"), suggested, "JSON (*.json);;CSV (*.csv)"
        )
        if not path:
            return
        fmt = "csv" if path.lower().endswith(".csv") or "csv" in selected.lower() else "json"
        try:
            written = export_report(path, report, fmt=fmt)
        except OSError as exc:
            self._notice.show_error(tr("results.export_error", error=str(exc)))
            return
        self._status_label.setText(tr("results.exported", path=str(written)))
        self._notice.show_info(tr("results.exported", path=str(written)))

    def _show_shortcuts(self) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(tr("shortcuts.title"))
        rows = [
            ("Space", tr("shortcuts.space")),
            ("A", tr("shortcuts.a")),
            ("B", tr("shortcuts.b")),
            ("1", tr("shortcuts.1")),
            ("2", tr("shortcuts.2")),
            ("← / →", tr("shortcuts.arrows")),
            ("Esc", tr("shortcuts.esc")),
            ("N", tr("shortcuts.n")),
            ("M", tr("shortcuts.m")),
            ("Ctrl+E", tr("menu.export")),
            ("F1", tr("menu.shortcuts")),
        ]
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText("\n".join(f"{key:<10}{label}" for key, label in rows))
        box.exec()

    def _show_about(self) -> None:
        QtWidgets.QMessageBox.about(
            self,
            tr("about.title"),
            f"<b>{tr('app.title')}</b><br><i>{tr('app.subtitle')}</i>"
            f"<br>{tr('about.version', version=__version__, binding=binding_version())}"
            f"<p>{tr('about.text')}</p>",
        )

    # =====================================================================
    #  i18n / window events
    # =====================================================================
    @staticmethod
    def _set_form_label(row: QtWidgets.QWidget, text: str) -> None:
        """Update the leading label of a :func:`form_row` in place."""
        labels = row.findChildren(QtWidgets.QLabel)
        if labels:
            labels[0].setText(text)

    def retranslate(self) -> None:
        if self._destroyed:
            return
        self.setWindowTitle(tr("app.title"))
        self._menu_file.setTitle(tr("menu.file"))
        self._menu_session.setTitle(tr("menu.session"))
        self._menu_view.setTitle(tr("menu.view"))
        self._menu_help.setTitle(tr("menu.help"))
        self._action_open.setText(tr("menu.open"))
        self._action_export.setText(tr("menu.export"))
        self._action_quit.setText(tr("menu.quit"))
        self._action_new.setText(tr("session.new"))
        self._action_finish.setText(tr("session.finish"))
        self._action_shortcuts.setText(tr("menu.shortcuts"))
        self._action_about.setText(tr("menu.about"))
        self._action_difference.setText(tr("player.view.diff"))
        for action in self._language_group:
            action.setChecked(action.data() == Translator.instance().language)

        for widget, title_key in self._group_titles:
            widget.setTitle(tr(title_key))
        self._sources_title.setText(tr("sources.title"))
        self._prepare_title.setText(tr("prepare.title"))
        self._swap_button.setText(tr("sources.swap"))
        self._loudness_check.setText(tr("prepare.loudness"))
        self._loudness_check.setToolTip(tr("prepare.loudness.tip"))
        self._levels_title.setText(tr("prepare.levels"))
        self._rounds_title.setText(tr("session.rounds"))
        self._seed_label.setText(tr("session.seed"))
        self._set_form_label(self._row_target, tr("prepare.target"))
        self._set_form_label(self._row_true_peak, tr("prepare.true_peak"))
        self._set_form_label(self._row_lra, tr("prepare.lra"))
        self._set_form_label(self._row_rate, tr("prepare.rate"))
        self._card_a.retranslate()
        self._card_b.retranslate()
        self._prepare_button.setText(
            tr("prepare.busy") if self.controller.preparing
            else (tr("prepare.reprepare") if self.controller.pair else tr("prepare.start"))
        )
        self._cancel_button.setText(tr("prepare.cancel"))
        self._new_session.setText(tr("session.new"))
        self._next_button.setText(tr("session.next"))
        self._skip_button.setText(tr("session.skip"))
        self._button_a.setText(tr("player.sample", key="A"))
        self._button_b.setText(tr("player.sample", key="B"))
        self._vote_a.setText(tr("vote.a"))
        self._vote_b.setText(tr("vote.b"))
        self._vote_question.setText(tr("vote.title"))
        self._vote_hint.setText(tr("vote.shortcut"))
        self._switch_hint.setText(tr("player.switching"))
        self._volume.setToolTip(tr("player.volume"))
        self._auto_advance.setText(tr("settings.auto_advance"))
        self._auto_advance.setToolTip(tr("settings.auto_advance.tip"))
        self._require_both.setToolTip(tr("vote.need_both"))
        self._rebuild_recent_menu()
        self._wave_hint.setText(tr("player.seek_hint"))
        self._wave_title.setText(tr("player.view"))
        self._wave_toggle.setItemText(0, tr("player.view.wave"))
        self._wave_toggle.setItemText(1, tr("player.view.diff"))
        self._waveform.set_empty_text(
            tr("app.idle") if self.controller.pair is None else tr("player.seek_hint")
        )
        self._reveal_check.setText(tr("session.reveal"))
        self._reveal_check.setToolTip(tr("session.reveal.tip"))
        self._require_both.setText(tr("session.require_both"))
        self._delete_temp.setText(tr("settings.delete_temp"))
        self._delete_temp.setToolTip(tr("settings.delete_temp.tip"))
        self._loop_button.setToolTip(tr("player.loop"))
        self._mute_button.setToolTip(tr("player.mute"))
        self._play_button.setToolTip(tr("player.play"))
        self._stop_button.setToolTip(tr("player.stop"))
        self._position.setToolTip(tr("player.position"))
        self._seed.setToolTip(tr("session.seed.tip"))
        for value, button in self._round_buttons.items():
            button.setText(
                tr("session.rounds.endless") if value == 0 else tr("session.rounds.n", n=value)
            )
        self._update_level_hints()
        self._segment_editor.retranslate()
        self._results.retranslate()
        if self._feedback_key and not self._feedback.text().startswith(("✓", "✗", "Not", "Correct")):
            self._feedback.setText(tr(self._feedback_key))
        self._sync_backend_combo()
        self._apply_state()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802 - Qt naming
        """Swallow shortcuts while a text/number input has focus."""
        focus = QtWidgets.QApplication.focusWidget()
        if isinstance(
            focus,
            (QtWidgets.QLineEdit, QtWidgets.QAbstractSpinBox, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit),
        ):
            super().keyPressEvent(event)
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._notice.hide()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and Path(path).is_file():
                key = SampleKey.A if not self.settings.lossless_path else SampleKey.B
                if self.settings.lossless_path and self.settings.lossy_path:
                    # Both slots taken: put the file in the slot of the same "kind"
                    key = self._slot_for(Path(path))
                self._assign(key, path)
                event.acceptProposedAction()
                return
        event.ignore()

    def _slot_for(self, path: Path) -> SampleKey:
        """Route a dropped file to the lossless or lossy slot by extension."""
        from ..core.ffmpeg import format_from_codec

        fmt = format_from_codec("", path)
        if fmt.is_lossless:
            return SampleKey.A
        return SampleKey.B

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self._save_settings()
        if self._probe_worker is not None and self._probe_worker.isRunning():
            self._probe_worker.wait(1500)
        self.controller.shutdown()
        self._destroyed = True
        if self._retranslate_hook is not None:
            self._retranslate_hook()
            self._retranslate_hook = None
        event.accept()

    def _save_settings(self) -> None:
        self.settings.geometry = bytes(
            self.saveGeometry().toBase64()
        ).decode("ascii")
        self.settings.loop = self._loop_button.isChecked()
        self.settings.volume = self._volume.value() / 100.0
        self.settings.delete_temp_on_exit = self._delete_temp.isChecked()
        self.settings.show_difference_view = self._show_difference
        self.settings.save()
