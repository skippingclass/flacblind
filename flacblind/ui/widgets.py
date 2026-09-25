"""Reusable widgets: drop cards, statistic tiles, the segment editor, notices.

Each widget owns its own texts and subscribes to the translator, so switching
language updates the whole window without a restart.
"""

from __future__ import annotations

import os
from pathlib import Path
from ..core.models import ProbeInfo, SampleKey, SegmentSpec
from .i18n import Translator, tr
from .qtcompat import QObject, Qt, QtGui, QtWidgets, Signal
from .theme import Palette, make_icon, repolish, scaled_font

__all__ = [
    "DropCard",
    "StatTile",
    "SegmentEditor",
    "Notice",
    "Chip",
    "section_label",
    "card",
    "h2",
    "mono",
    "form_row",
]


# --------------------------------------------------------------------------- #
#  Layout helpers
# --------------------------------------------------------------------------- #
def section_label(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setProperty("role", "section")
    return label


def h2(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setProperty("role", "title")
    return label


def mono(text: str = "") -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setProperty("role", "mono")
    return label


def card(layout: QtWidgets.QVBoxLayout | None = None) -> QtWidgets.QFrame:
    """A rounded surface panel."""
    frame = QtWidgets.QFrame()
    frame.setProperty("card", True)
    frame.setLayout(layout or QtWidgets.QVBoxLayout())
    return frame


def form_row(*widgets: QtWidgets.QWidget, label: str = "") -> QtWidgets.QWidget:
    """A ``label: widget`` row for the settings forms."""
    row = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    if label:
        text = QtWidgets.QLabel(label)
        text.setMinimumWidth(84)
        text.setProperty("role", "subtitle")
        layout.addWidget(text)
    for widget in widgets:
        layout.addWidget(widget)
    return row


# --------------------------------------------------------------------------- #
#  Drop card
# --------------------------------------------------------------------------- #
class DropCard(QtWidgets.QFrame):
    """One input slot: click or drop a file, shows format/duration metadata.

    Emits :attr:`fileDropped` with the path; the window decides which slot the
    user aimed at (the card itself only reports *what* was dropped).
    """

    fileDropped = Signal(str)
    browseRequested = Signal()
    clearRequested = Signal()

    def __init__(
        self,
        key: SampleKey,
        title_key: str,
        hint_key: str,
        accent: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self._title_key = title_key
        self._hint_key = hint_key
        self._accent = accent
        self._path: Path | None = None
        self._probe: ProbeInfo | None = None
        self._error = ""
        self._hover = False

        self.setAcceptDrops(True)
        self.setObjectName("dropCard")
        self.setProperty("card", True)
        self.setMinimumHeight(96)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)
        self._badge = QtWidgets.QLabel(key.value)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge.setFixedSize(22, 22)
        self._badge.setStyleSheet(
            f"background: {accent}; color: #06121f; border-radius: 11px; font-weight: 800;"
        )
        self._title = QtWidgets.QLabel()
        self._title.setProperty("role", "section")
        header.addWidget(self._badge)
        header.addWidget(self._title)
        header.addStretch(1)
        layout.addLayout(header)

        self._name = QtWidgets.QLabel()
        self._name.setWordWrap(False)
        font = self._name.font()
        font.setBold(True)
        self._name.setFont(font)
        layout.addWidget(self._name)

        self._meta = QtWidgets.QLabel()
        self._meta.setProperty("role", "hint")
        layout.addWidget(self._meta)

        self._status = QtWidgets.QLabel()
        self._status.setProperty("role", "hint")
        layout.addWidget(self._status)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(6)
        self._browse = QtWidgets.QPushButton()
        self._browse.setIcon(make_icon("folder", Palette.text_dim, 16))
        self._browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse.clicked.connect(self.browseRequested)
        self._clear = QtWidgets.QPushButton()
        self._clear.setIcon(make_icon("refresh", Palette.text_dim, 16))
        self._clear.setProperty("variant", "ghost")
        self._clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear.clicked.connect(self.clearRequested)
        buttons.addWidget(self._browse)
        buttons.addStretch(1)
        buttons.addWidget(self._clear)
        layout.addLayout(buttons)

        self._translator = Translator.instance()
        self._translator.on_change(self.retranslate)
        self.retranslate()

    # -- content ---------------------------------------------------------
    def set_path(self, path: str | os.PathLike[str] | None) -> None:
        self._path = Path(path) if path else None
        self._error = ""
        self.retranslate()

    def set_probe(self, probe: ProbeInfo | None) -> None:
        self._probe = probe
        self.retranslate()

    def set_status(self, text: str, role: str = "hint") -> None:
        self._status.setText(text)
        self._status.setProperty("role", role)
        repolish(self._status)

    def set_busy(self, busy: bool) -> None:
        self._browse.setEnabled(not busy)
        self._clear.setEnabled(not busy)
        self.setEnabled(True)

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def probe(self) -> ProbeInfo | None:
        return self._probe

    # -- i18n ------------------------------------------------------------
    def retranslate(self) -> None:
        self._title.setText(tr(self._title_key))
        self._browse.setText(tr("sources.browse") if self._path is None else tr("sources.change"))
        self._clear.setToolTip(tr("sources.clear"))
        if self._path is None:
            self._name.setText(tr("sources.drop"))
            self._name.setStyleSheet(f"color: {Palette.text_faint};")
            self._meta.setText(tr(self._hint_key))
            self._status.setText("")
        else:
            self._name.setStyleSheet("")
            self._name.setText(self._path.name)
            self._name.setToolTip(str(self._path.parent))
            parts: list[str] = []
            if self._probe is not None:
                parts.append(self._probe.format_tag)
                parts.append(self._probe.detail_line)
            else:
                parts.append(tr("sources.auto"))
            self._meta.setText("  ·  ".join(p for p in parts if p))
            if self._error:
                self._status.setText(self._error)
                self._status.setProperty("role", "error")
            else:
                self._status.setText("")
            repolish(self._status)
        self._update_border()

    def _update_border(self) -> None:
        accent = self._accent
        if self._hover:
            border, background = accent, f"rgba(255,255,255,0.04)"
        elif self._path is not None:
            border, background = f"{accent}66", "transparent"
        else:
            border, background = Palette.border, "transparent"
        self.setStyleSheet(
            f"QFrame#dropCard {{ background: {background}; border: 1px solid {border};"
            f" border-radius: 12px; }}"
        )

    # -- drag & drop -----------------------------------------------------
    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hover = True
            self._update_border()

    def dragLeaveEvent(self, event: QtGui.QDragEvent) -> None:  # noqa: N802
        self._hover = False
        self._update_border()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: N802
        self._hover = False
        self._update_border()
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                self.fileDropped.emit(path)
                break
        event.acceptProposedAction()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.browseRequested.emit()
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------- #
#  Stat tile
# --------------------------------------------------------------------------- #
class StatTile(QtWidgets.QFrame):
    """A small labelled number, used across the results panel."""

    def __init__(
        self,
        title_key: str,
        value_key: str = "common.none",
        *,
        accent: str = Palette.text,
        tooltip: str = "",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._title_key = title_key
        self._value_key = value_key
        self._accent = accent
        self._tooltip_key = tooltip

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self._title = QtWidgets.QLabel()
        self._title.setProperty("role", "section")
        self._title.setWordWrap(True)
        self._title.setFont(scaled_font(self._title.font(), delta_pt=-2.0, min_pt=7.5))
        self._value = QtWidgets.QLabel()
        font = scaled_font(self._value.font(), delta_pt=5.0)
        font.setBold(True)
        self._value.setFont(font)
        layout.addWidget(self._title)
        layout.addWidget(self._value)
        Translator.instance().on_change(self.retranslate)
        self.retranslate()

    def set_value(self, text: str, *, accent: str | None = None) -> None:
        self._value.setText(text)
        if accent:
            self._value.setStyleSheet(f"color: {accent};")
        self.setToolTip(self._value.toolTip())

    def set_value_key(self, key: str, **kwargs: object) -> None:
        self.set_value(tr(key, **kwargs))

    def retranslate(self) -> None:
        self._title.setText(tr(self._title_key))
        if self._value_key:
            self._value.setText(tr(self._value_key))
        if self._tooltip_key:
            self.setToolTip(tr(self._tooltip_key))


class Chip(QtWidgets.QLabel):
    """A tiny status pill."""

    def __init__(self, text: str = "", accent: str = Palette.text_dim, parent: QObject | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_accent(accent)

    def set_accent(self, accent: str) -> None:
        self.setStyleSheet(
            f"background: {accent}22; color: {accent}; border: 1px solid {accent}55;"
            f"border-radius: 9px; padding: 2px 8px; font-size: 11px;"
        )


# --------------------------------------------------------------------------- #
#  Notice
# --------------------------------------------------------------------------- #
class Notice(QtWidgets.QFrame):
    """Inline error / warning banner."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        self._label = QtWidgets.QLabel()
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._close = QtWidgets.QPushButton()
        self._close.setText("✕")
        self._close.setProperty("variant", "ghost")
        self._close.setFixedWidth(28)
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.clicked.connect(self.hide)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._close, 0)
        self.hide()

    def show_error(self, message: str, hint: str = "") -> None:
        text = message if not hint else f"{message}\n{hint}"
        self._label.setText(text)
        self.setStyleSheet(
            f"QFrame {{ background: {Palette.danger}1a; border: 1px solid {Palette.danger}66;"
            f" border-radius: 10px; }}"
        )
        self.show()

    def show_warning(self, message: str) -> None:
        self._label.setText(message)
        self.setStyleSheet(
            f"QFrame {{ background: {Palette.warning}1a; border: 1px solid {Palette.warning}55;"
            f" border-radius: 10px; }}"
        )
        self.show()

    def show_info(self, message: str, accent: str = Palette.primary) -> None:
        self._label.setText(message)
        self.setStyleSheet(
            f"QFrame {{ background: {accent}1a; border: 1px solid {accent}55; border-radius: 10px; }}"
        )
        self.show()


def suggest_segment_from_peaks(peaks: object, length: float = 30.0) -> SegmentSpec:
    """Pick a lively window of ``length`` seconds, skipping intros and silence.

    Used by the segment editor's "jump to the loudest part" button: the busiest
    window of a track is almost always where codec artefacts are audible.
    """
    envelope = getattr(peaks, "rms", None)
    if not envelope:
        return SegmentSpec.whole()
    duration = peaks.duration_sec
    if duration <= length:
        return SegmentSpec.whole()
    window = max(1, int(len(envelope) * length / duration))
    step = max(1, window // 4)
    best_index, best_energy = 0, -1.0
    for start in range(0, max(1, len(envelope) - window), step):
        energy = sum(envelope[start:start + window])
        if energy > best_energy:
            best_index, best_energy = start, energy
    offset = peaks.bucket_frames * best_index / max(1, peaks.sample_rate)
    return SegmentSpec.slice_of(offset, length)


# --------------------------------------------------------------------------- #
#  Segment editor
# --------------------------------------------------------------------------- #
class SegmentEditor(QtWidgets.QWidget):
    """Whole-file vs. custom segment, with clamping to the source length."""

    changed = Signal(object)   # SegmentSpec

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_duration = 0.0
        self._segment = SegmentSpec.whole()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._whole = QtWidgets.QRadioButton()
        self._whole.setChecked(True)
        self._custom = QtWidgets.QRadioButton()
        self._whole.toggled.connect(self._on_mode)
        modes = QtWidgets.QHBoxLayout()
        modes.setSpacing(12)
        modes.addWidget(self._whole)
        modes.addWidget(self._custom)
        modes.addStretch(1)
        layout.addLayout(modes)

        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        self._start = QtWidgets.QDoubleSpinBox()
        self._start.setRange(0.0, 36000.0)
        self._start.setSingleStep(1.0)
        self._start.setDecimals(1)
        self._start.setSuffix(f" {tr('prepare.seconds')}")
        self._length = QtWidgets.QDoubleSpinBox()
        self._length.setRange(1.0, 36000.0)
        self._length.setValue(30.0)
        self._length.setSingleStep(5.0)
        self._length.setDecimals(1)
        self._length.setSuffix(f" {tr('prepare.seconds')}")
        self._start.setEnabled(False)
        self._length.setEnabled(False)
        self._start.valueChanged.connect(self._on_value)
        self._length.valueChanged.connect(self._on_value)
        self._start_label = QtWidgets.QLabel()
        self._length_label = QtWidgets.QLabel()
        for row, (label, widget) in enumerate(
            ((self._start_label, self._start), (self._length_label, self._length))
        ):
            label.setProperty("role", "subtitle")
            grid.addWidget(label, row, 0)
            grid.addWidget(widget, row, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self._hint = QtWidgets.QLabel()
        self._hint.setProperty("role", "hint")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._jump = QtWidgets.QPushButton()
        self._jump.setProperty("variant", "ghost")
        self._jump.setCursor(Qt.CursorShape.PointingHandCursor)
        self._jump.clicked.connect(self._jump_to_loudest)
        layout.addWidget(self._jump, 0, Qt.AlignmentFlag.AlignLeft)

        Translator.instance().on_change(self.retranslate)
        self.retranslate()

    # -- state -----------------------------------------------------------
    def set_source_duration(self, duration: float) -> None:
        self._source_duration = max(0.0, duration)
        self._start.setMaximum(max(0.0, self._source_duration - 0.5))
        if self._start.value() > self._start.maximum():
            self._start.setValue(max(0.0, self._start.maximum()))
        self._on_value()

    def set_segment(self, segment: SegmentSpec) -> None:
        self._segment = segment
        self._whole.setChecked(segment.whole_file)
        self._custom.setChecked(not segment.whole_file)
        self._start.setValue(segment.start_sec)
        if segment.duration_sec:
            self._length.setValue(segment.duration_sec)
        self._on_value()

    def segment(self) -> SegmentSpec:
        return self._segment

    def _on_mode(self, checked: bool) -> None:
        self._start.setEnabled(not checked)
        self._length.setEnabled(not checked)
        self._on_value()

    def _on_value(self, *_args: object) -> None:
        if self._whole.isChecked():
            segment = SegmentSpec.whole()
        else:
            length = min(self._length.value(), max(1.0, self._source_duration - self._start.value()))
            if self._source_duration > 0 and length != self._length.value():
                self._length.setValue(length)
            segment = SegmentSpec.slice_of(self._start.value(), length)
        if segment != self._segment:
            self._segment = segment
            self.changed.emit(segment)
        self._update_hint()

    def _jump_to_loudest(self) -> None:
        """Move the segment start to the busiest part of the waveform."""
        peaks = getattr(self, "_peaks", None)
        if peaks is None or not getattr(peaks, "rms", ()) or self._source_duration <= 0:
            return
        length = self._length.value() if not self._whole.isChecked() else min(30.0, self._source_duration)
        segment = suggest_segment_from_peaks(peaks, length)
        if segment.whole_file:
            return
        self._whole.setChecked(False)
        self._start.setValue(round(segment.start_sec, 1))

    def set_peaks(self, peaks: object) -> None:
        self._peaks = peaks
        self._jump.setEnabled(bool(peaks))

    def _update_hint(self) -> None:
        if self._source_duration <= 0:
            self._hint.setText(tr("prepare.segment.hint"))
            return
        if self._segment.whole_file:
            self._hint.setText(f"{tr('prepare.segment.whole')} · {self._source_duration:.1f} s")
        else:
            end = self._segment.start_sec + (self._segment.duration_sec or 0.0)
            self._hint.setText(
                f"{_fmt(self._segment.start_sec)} → {_fmt(end)}"
                f"  ·  {self._segment.duration_sec or 0:.0f} s"
            )

    def retranslate(self) -> None:
        unit = f" {tr('prepare.seconds')}"
        self._start.setSuffix(unit)
        self._length.setSuffix(unit)
        self._whole.setText(tr("prepare.segment.whole"))
        self._custom.setText(tr("prepare.segment.custom"))
        self._start_label.setText(tr("prepare.segment.start"))
        self._length_label.setText(tr("prepare.segment.length"))
        self._jump.setText(tr("prepare.segment.jump"))
        self._update_hint()


def _fmt(seconds: float) -> str:
    minutes, secs = divmod(int(round(max(0.0, seconds))), 60)
    return f"{minutes:d}:{secs:02d}"
