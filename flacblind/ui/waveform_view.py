"""The waveform / timeline view.

A cached :class:`QPixmap` holds the envelope; only the playhead is repainted
per frame, so a 2400-bucket waveform scrolls smoothly at 60 fps without
re-plotting samples.  Clicking or dragging anywhere on the waveform emits
``seekRequested`` with a 0..1 fraction, which the window turns into a
millisecond position.
"""

from __future__ import annotations

import math
from ..core.models import Peaks
from .i18n import tr
from .qtcompat import QtCore, QtGui, QtWidgets, Qt, Signal
from .theme import Palette, scaled_font

__all__ = ["WaveformView", "format_position"]


class WaveformView(QtWidgets.QWidget):
    """Interactive waveform with a playhead, a hover cursor and time ticks."""

    seekRequested = Signal(float)      # 0..1 of the segment
    scrubbed = Signal(float)
    hovered = Signal(object)           # fraction | None

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding
        )

        self._peaks: Peaks | None = None
        self._color = Palette.sample_a
        self._position = 0.0          # 0..1
        self._hover: float | None = None
        self._duration = 0.0
        self._cache: QtGui.QPixmap | None = None
        self._cache_key: tuple = ()
        self._dragging = False
        self._empty_text = ""

    # ------------------------------------------------------------- content
    def set_peaks(
        self,
        peaks: Peaks | None,
        *,
        color: str = Palette.sample_a,
        duration: float | None = None,
    ) -> None:
        self._peaks = peaks
        self._color = color
        self._duration = duration if duration is not None else (peaks.duration_sec if peaks else 0.0)
        self._cache = None
        self.update()

    def set_color(self, color: str) -> None:
        if color != self._color:
            self._color = color
            self._cache = None
            self.update()

    def set_position(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if abs(fraction - self._position) > 0.0005:
            self._position = fraction
            self.update()

    @property
    def position(self) -> float:
        return self._position

    def set_empty_text(self, text: str) -> None:
        if text != self._empty_text:
            self._empty_text = text
            self.update()

    def clear(self) -> None:
        self._peaks = None
        self._cache = None
        self._position = 0.0
        self._duration = 0.0
        self.update()

    @property
    def duration(self) -> float:
        return self._duration

    # ------------------------------------------------------------- painting
    def _plot_rect(self) -> QtCore.QRectF:
        """Area reserved for the waveform (time ticks live below it)."""
        margin = 6.0
        height = max(20.0, self.height() - 32.0)
        return QtCore.QRectF(margin, margin, max(1.0, self.width() - 2 * margin), height)

    def _render_cache(self, rect: QtCore.QRectF) -> QtGui.QPixmap:
        key = (self._peaks, rect.width(), rect.height(), self._color, self.width(), self.height())
        if self._cache is not None and self._cache_key == key:
            return self._cache
        ratio = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else self.devicePixelRatio()
        pixmap = QtGui.QPixmap(max(1, int(self.width() * ratio)), max(1, int(self.height() * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QtGui.QColor(Palette.surface))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        self._paint_background(painter, rect)
        if self._peaks is not None and not self._peaks.is_empty:
            self._paint_envelope(painter, rect)
        painter.end()
        self._cache = pixmap
        self._cache_key = key
        return pixmap

    def _paint_background(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        painter.fillRect(QtCore.QRectF(0, 0, self.width(), self.height()), QtGui.QColor(Palette.surface))
        # centre line + subtle vertical grid
        pen = QtGui.QPen(QtGui.QColor(Palette.grid))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        middle = rect.center().y()
        painter.drawLine(QtCore.QPointF(rect.left(), middle), QtCore.QPointF(rect.right(), middle))
        if self._duration > 0:
            for fraction in self._tick_fractions():
                x = rect.left() + rect.width() * fraction
                painter.drawLine(QtCore.QPointF(x, rect.top()), QtCore.QPointF(x, rect.bottom()))

    def _tick_fractions(self) -> list[float]:
        """Choose a readable tick interval for the current duration."""
        if self._duration <= 0:
            return []
        target_seconds = 5.0
        for candidate in (1, 2, 5, 10, 15, 30, 60, 120, 300):
            if self._duration / candidate <= 12:
                target_seconds = float(candidate)
                break
        else:
            target_seconds = float(self._duration / 12)
        count = max(1, int(self._duration / target_seconds))
        return [i / count for i in range(1, count + 1)] if count <= 24 else []

    def _paint_envelope(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        peaks = self._peaks
        assert peaks is not None
        count = len(peaks)
        if count == 0:
            return
        width = rect.width()
        height = rect.height()
        middle = rect.center().y()
        half = height / 2 - 2.0

        # Perceptual scaling: a linear envelope of music looks like a solid
        # block, so compress amplitudes with a square root.
        loudest = max(max(abs(v) for v in peaks.mins), max(abs(v) for v in peaks.maxs), 1e-6)
        scale = half / math.sqrt(loudest) if loudest > 0 else 0.0

        # Reduce the envelope to one min/max pair per pixel column once, then
        # paint the filled body and both outlines from that.
        #
        # The bucket range is clamped: a wide window (or a very short segment
        # with few buckets) can have *more* pixel columns than buckets, and an
        # unclamped range would run off the end of the tuple — an exception
        # inside paintEvent, i.e. a crash under PyQt6.
        columns = max(1, int(width))
        step = count / columns
        tops: list[float] = []
        bottoms: list[float] = []
        for x in range(columns):
            start = min(count - 1, int(x * step))
            end = min(count, max(start + 1, int((x + 1) * step)))
            window = range(start, end)
            tops.append(min(peaks.mins[i] for i in window))
            bottoms.append(max(peaks.maxs[i] for i in window))

        color = QtGui.QColor(self._color)
        fill = QtGui.QColor(color)
        fill.setAlpha(70)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        path = QtGui.QPainterPath()
        path.moveTo(rect.left(), middle)
        for x in range(columns):
            px = rect.left() + x
            path.lineTo(px, middle + tops[x] * scale)
            path.lineTo(px, middle + bottoms[x] * scale)
        path.lineTo(rect.right(), middle)
        path.closeSubpath()
        painter.drawPath(path)

        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        stroke = QtGui.QColor(color)
        stroke.setAlpha(220)
        pen = QtGui.QPen(stroke)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        for values in (tops, bottoms):
            outline = QtGui.QPainterPath()
            for x in range(columns):
                y = middle + values[x] * scale
                if x == 0:
                    outline.moveTo(rect.left(), y)
                else:
                    outline.lineTo(rect.left() + x, y)
            painter.drawPath(outline)

    def _paint_ticks(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        if self._duration <= 0:
            return
        font = scaled_font(painter.font(), delta_pt=-2.0, min_pt=7.0)
        painter.setFont(font)
        metrics = QtGui.QFontMetrics(font)
        painter.setPen(QtGui.QColor(Palette.text_faint))
        y = rect.bottom() + 3
        for fraction in self._tick_fractions():
            x = rect.left() + rect.width() * fraction
            seconds = fraction * self._duration
            label = _fmt_time(seconds)
            width = metrics.horizontalAdvance(label)
            painter.drawText(
                QtCore.QRectF(min(max(0.0, x - width / 2), self.width() - width), y - 2, width, 14),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                label,
            )

    def _paint_playhead(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        x = rect.left() + rect.width() * self._position
        pen = QtGui.QPen(QtGui.QColor("#ffffff"))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.drawLine(QtCore.QPointF(x, rect.top() - 2), QtCore.QPointF(x, rect.bottom() + 2))
        painter.setBrush(QtGui.QBrush(QtGui.QColor("#ffffff")))
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        head = QtGui.QPolygonF(
            [
                QtCore.QPointF(x - 4, rect.top() - 6),
                QtCore.QPointF(x + 4, rect.top() - 6),
                QtCore.QPointF(x, rect.top()),
            ]
        )
        painter.drawPolygon(head)

    def _paint_hover(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        if self._hover is None or self._duration <= 0:
            return
        x = rect.left() + rect.width() * self._hover
        # NB: QColor(name, alpha) is not a valid overload — go via the helper.
        pen = QtGui.QPen(Palette.qcolor(Palette.text_dim, 160))
        pen.setWidthF(1.0)
        pen.setStyle(QtCore.Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawLine(QtCore.QPointF(x, rect.top()), QtCore.QPointF(x, rect.bottom()))

    def _paint_placeholder(self, painter: QtGui.QPainter, rect: QtCore.QRectF) -> None:
        if self._peaks is not None and not self._peaks.is_empty:
            return
        painter.setPen(QtGui.QColor(Palette.text_faint))
        painter.setFont(scaled_font(painter.font(), delta_pt=1.0))
        painter.drawText(
            QtCore.QRectF(0, 0, self.width(), self.height()),
            int(Qt.AlignmentFlag.AlignCenter),
            self._empty_text or tr("app.idle"),
        )

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 - Qt naming
        rect = self._plot_rect()
        painter = QtGui.QPainter(self)
        painter.drawPixmap(0, 0, self._render_cache(rect))
        self._paint_placeholder(painter, rect)
        self._paint_ticks(painter, rect)
        self._paint_hover(painter, rect)
        self._paint_playhead(painter, rect)
        painter.end()
        del event

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._cache = None

    # ------------------------------------------------------------- mouse
    def _fraction_at(self, x: float) -> float:
        rect = self._plot_rect()
        if rect.width() <= 0:
            return 0.0
        return max(0.0, min(1.0, (x - rect.left()) / rect.width()))

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() != Qt.MouseButton.LeftButton or self._duration <= 0:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        fraction = self._fraction_at(event.position().x())
        self.set_position(fraction)
        self.scrubbed.emit(fraction)
        self.seekRequested.emit(fraction)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt naming
        fraction = self._fraction_at(event.position().x())
        if self._dragging:
            self.set_position(fraction)
            self.scrubbed.emit(fraction)
            self.seekRequested.emit(fraction)
        if self._hover is None or abs(fraction - self._hover) > 0.002:
            self._hover = fraction
            self.hovered.emit(fraction)
            self.update()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QtCore.QEvent) -> None:  # noqa: N802 - Qt naming
        self._hover = None
        self.hovered.emit(None)
        self.update()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802 - Qt naming
        # Double click jumps back to the start of the segment.
        self.set_position(0.0)
        self.seekRequested.emit(0.0)
        super().mouseDoubleClickEvent(event)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt naming
        return QtCore.QSize(480, 190)


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        minutes, secs = divmod(int(round(seconds)), 60)
        return f"{minutes:d}:{secs:02d}"
    hours, rest = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def format_position(position_ms: int, duration_ms: int) -> str:
    """``m:ss / m:ss`` label for the transport row."""
    now = _fmt_time(position_ms / 1000.0)
    if duration_ms <= 0:
        return now
    return f"{now} / {_fmt_time(duration_ms / 1000.0)}"
