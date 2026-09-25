"""Results panel: score, exact binomial p-value, verdict and round history.

The p-value deserves the centre of the panel: "9/10" means nothing without
knowing that a coin flipper reaches it in 11 of 1000 games, so the verdict is
computed from the binomial tail rather than from a hand-picked threshold.
"""

from __future__ import annotations

from typing import Sequence

from ..core.models import RoundRecord, SampleKey, SessionStats
from ..core.stats import (
    SIGNIFICANCE,
    STRONG_SIGNIFICANCE,
    fmt_p_value,
    score_histogram,
    verdict,
    z_score,
)
from .i18n import Translator, tr
from .qtcompat import Qt, QtCore, QtGui, QtWidgets, Signal
from .theme import Palette, scaled_font
from .widgets import StatTile, card, section_label  # noqa: F401

__all__ = ["ResultsPanel", "DistributionView"]


class DistributionView(QtWidgets.QWidget):
    """Histogram of the null distribution with the observed score marked.

    Each bar is the probability of that score for a listener with no hearing at
    all; the marked bar is what the user actually achieved, which makes "this
    could be luck" a glance rather than a calculation.
    """

    scoreClicked = Signal(int)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(96)
        self.setMouseTracking(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self._values: list[float] = []
        self._observed = -1
        self._hover = -1
        self._threshold = 0

    def set_data(self, rounds: int, correct: int) -> None:
        self._values = score_histogram(rounds)
        self._observed = correct if rounds else -1
        self.update()

    def clear(self) -> None:
        self._values = []
        self._observed = -1
        self.update()

    # -- painting --------------------------------------------------------
    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(0, 0, self.width(), self.height() - 16)
        painter.fillRect(QtCore.QRectF(0, 0, self.width(), self.height()), QtGui.QColor(Palette.surface))

        if not self._values:
            painter.setPen(QtGui.QColor(Palette.text_faint))
            painter.drawText(
                QtCore.QRectF(0, 0, self.width(), self.height()),
                int(Qt.AlignmentFlag.AlignCenter),
                tr("results.no_data"),
            )
            painter.end()
            return

        count = len(self._values)
        slot = rect.width() / count
        bar_width = max(2.0, slot * 0.72)
        for index, value in enumerate(self._values):
            height = max(1.0, value * (rect.height() - 4))
            x = slot * index + (slot - bar_width) / 2
            bar = QtCore.QRectF(x, rect.bottom() - height, bar_width, height)
            if index == self._observed:
                color = QtGui.QColor(Palette.success)
            elif index >= self._threshold:
                color = QtGui.QColor(Palette.primary)
                color.setAlpha(150)
            elif index == self._hover:
                color = QtGui.QColor(Palette.text_dim)
            else:
                color = QtGui.QColor(Palette.text_faint)
                color.setAlpha(90)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(color))
            painter.drawRoundedRect(bar, 2, 2)

        # Observed-score marker + labels
        if 0 <= self._observed < count:
            x = slot * self._observed + slot / 2
            pen = QtGui.QPen(QtGui.QColor(Palette.success))
            pen.setWidthF(1.4)
            painter.setPen(pen)
            painter.drawLine(
                QtCore.QPointF(x, rect.top()), QtCore.QPointF(x, rect.bottom())
            )

        painter.setFont(scaled_font(painter.font(), delta_pt=-2.0, min_pt=7.0))
        painter.setPen(QtGui.QColor(Palette.text_faint))
        for index in range(count):
            if count <= 16 or index % max(1, count // 12) == 0 or index == self._observed:
                x = slot * index + slot / 2
                painter.drawText(
                    QtCore.QRectF(x - 12, rect.bottom() + 1, 24, 14),
                    int(Qt.AlignmentFlag.AlignCenter),
                    str(index),
                )
        painter.end()
        del event

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if not self._values:
            return
        index = int(event.position().x() / (self.width() / len(self._values)))
        index = max(0, min(len(self._values) - 1, index))
        if index != self._hover:
            self._hover = index
            self.setToolTip(
                f"{index} / {len(self._values) - 1}  ·  "
                f"{self._values[index] * 100:.2f} %"
            )
            self.update()

    def leaveEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if not self._values and event.button() != Qt.MouseButton.LeftButton:
            return
        if self._values:
            index = int(event.position().x() / (self.width() / len(self._values)))
            index = max(0, min(len(self._values) - 1, index))
            self.scoreClicked.emit(index)
        super().mouseReleaseEvent(event)


class ResultsPanel(QtWidgets.QFrame):
    """Everything the listener sees after (and during) the test."""

    scoreClicked = Signal(int)
    exportRequested = Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._stats: SessionStats | None = None
        self._records: tuple[RoundRecord, ...] = ()
        self._endless = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self._title = QtWidgets.QLabel()
        self._title.setProperty("role", "section")
        layout.addWidget(self._title)

        # score + p-value
        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(8)
        self._score = StatTile("results.score", accent=Palette.primary)
        self._pvalue = StatTile("results.pvalue", accent=Palette.text)
        self._chance = StatTile("results.chance", accent=Palette.text_dim)
        for tile in (self._score, self._pvalue, self._chance):
            tile.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
            )
            tiles.addWidget(tile)
        layout.addLayout(tiles)

        self._verdict = QtWidgets.QLabel()
        self._verdict.setWordWrap(True)
        layout.addWidget(self._verdict)

        self._detail = QtWidgets.QLabel()
        self._detail.setProperty("role", "hint")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)

        self._distribution_title = section_label(tr("results.distribution"))
        layout.addWidget(self._distribution_title)
        self._distribution = DistributionView()
        self._distribution.setToolTip(tr("results.distribution.tip"))
        self._distribution.scoreClicked.connect(self.scoreClicked)
        layout.addWidget(self._distribution)

        self._history_label = QtWidgets.QLabel()
        self._history_label.setProperty("role", "section")
        layout.addWidget(self._history_label)

        self._table = QtWidgets.QTableWidget(0, 3)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setMinimumHeight(120)
        self._table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self._table, 1)

        self._export = QtWidgets.QPushButton()
        self._export.setProperty("variant", "primary")
        self._export.setCursor(Qt.CursorShape.PointingHandCursor)
        self._export.clicked.connect(self.exportRequested)
        layout.addWidget(self._export)

        Translator.instance().on_change(self.retranslate)
        self.retranslate()
        self.set_stats(None, ())

    # -- data ------------------------------------------------------------
    def set_stats(
        self,
        stats: SessionStats | None,
        records: Sequence[RoundRecord] = (),
        *,
        endless: bool = False,
    ) -> None:
        self._stats = stats
        self._records = tuple(records)
        self._endless = endless
        self._refresh()
        self._fill_table()

    def _refresh(self) -> None:
        stats = self._stats
        if stats is None or stats.rounds == 0:
            self._score.set_value("—")
            self._pvalue.set_value("—")
            self._chance.set_value("—")
            self._verdict.setText("")
            self._verdict.setStyleSheet("")
            self._detail.setText("")
            self._distribution.clear()
            return

        reading = verdict(stats.rounds, stats.correct)
        self._score.set_value(f"{stats.correct}/{stats.rounds}")
        self._score.setToolTip(
            f"{stats.correct} {tr('results.wins')} · "
            f"{stats.rounds - stats.correct} {tr('results.losses')}"
        )
        self._pvalue.set_value(fmt_p_value(stats.p_value))
        self._chance.set_value(f"{stats.p_value * 100:.2f} %")

        if reading.level == "strong":
            accent = Palette.success
        elif reading.level == "significant":
            accent = Palette.sample_a
        elif reading.level == "weak":
            accent = Palette.warning
        else:
            accent = Palette.text_dim
        self._pvalue.set_value(fmt_p_value(stats.p_value), accent=accent)
        self._verdict.setText(tr(f"results.verdict.{reading.level}"))
        self._verdict.setStyleSheet(f"color: {accent}; font-weight: 700; font-size: 14px;")

        low, high = stats.ci_low, stats.ci_high
        lines = [
            f"{tr('results.interval')}: {low * 100:.0f}–{high * 100:.0f} %  ·  "
            f"{tr('results.z')} = {z_score(stats.rounds, stats.correct):+.2f}",
            tr(f"results.detail.{reading.level}", pct=f"{stats.p_value * 100:.1f}"),
        ]
        if stats.margin > 0:
            lines.append(
                tr(
                    "results.needed",
                    n=stats.margin,
                    plural=Translator.instance().plural(stats.margin),
                )
            )
        elif stats.rounds and not stats.is_significant:
            lines.append(
                tr("results.needed.never", n=stats.rounds)
            )
        self._detail.setText("\n".join(lines))
        self._distribution.set_data(stats.rounds, stats.correct)

    def _fill_table(self) -> None:
        self._table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            guess = QtWidgets.QTableWidgetItem(record.guess.value)
            truth = QtWidgets.QTableWidgetItem(record.lossless_key.value)
            mark = "✓" if record.correct else "✗"
            result = QtWidgets.QTableWidgetItem(mark)
            for column, item in enumerate((QtWidgets.QTableWidgetItem(str(record.index)), guess, truth, result)):
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                self._table.setItem(row, column, item)
            result.setForeground(QtGui.QColor(Palette.success if record.correct else Palette.danger))
            truth.setForeground(
                QtGui.QColor(Palette.sample_a if record.lossless_key is SampleKey.A else Palette.sample_b)
            )
        # Keep the newest round in view.
        if self._records:
            self._table.scrollToBottom()

    # -- i18n ------------------------------------------------------------
    def retranslate(self) -> None:
        self._title.setText(tr("results.title"))
        self._distribution_title.setText(tr("results.distribution"))
        self._history_label.setText(tr("results.history"))
        self._export.setText(tr("results.export"))
        self._table.setHorizontalHeaderLabels(
            [tr("results.col.round"), tr("results.col.guess"), tr("results.col.truth"), "✓"]
        )
        self._score.retranslate()
        self._pvalue.retranslate()
        self._chance.retranslate()
        self._refresh()
        self._fill_table()

    # -- helpers for the window -----------------------------------------
    def verdict_text(self) -> str:
        """Short verdict label for the status bar (translated)."""
        stats = self._stats
        if not stats or stats.rounds == 0:
            return ""
        reading = verdict(stats.rounds, stats.correct)
        return tr(f"results.verdict.{reading.level}")

    @staticmethod
    def significance_marker() -> float:
        return SIGNIFICANCE

    @staticmethod
    def strong_marker() -> float:
        return STRONG_SIGNIFICANCE
