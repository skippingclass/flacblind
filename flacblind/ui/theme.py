"""Dark theme: palette, stylesheet and small painting helpers.

A single QSS string drives the whole look.  Widgets opt into variants through
the Qt ``variant`` property (``QPushButton[variant="primary"]``), which is
re-polished with :func:`repolish` whenever it changes.
"""

from __future__ import annotations

from .qtcompat import QtCore, QtGui, QtWidgets

__all__ = [
    "Palette",
    "STYLESHEET",
    "apply_theme",
    "repolish",
    "font_family",
    "make_icon",
    "scaled_font",
]


class Palette:
    """Named colours used by both the QSS and the custom painters."""

    bg = "#0e1116"
    surface = "#151920"
    surface_alt = "#1b212a"
    elevated = "#222834"
    border = "#2a313d"
    border_soft = "#222834"
    text = "#e7ebf2"
    text_dim = "#9aa4b8"
    text_faint = "#6b7488"
    primary = "#4c8dff"
    primary_dim = "#2f6ad9"
    sample_a = "#37d9a0"
    sample_b = "#ffb454"
    success = "#3ddc84"
    danger = "#ff5f56"
    warning = "#ffcc66"
    grid = "#1f2530"

    @staticmethod
    def qcolor(name: str, alpha: int | None = None) -> "QtGui.QColor":
        color = QtGui.QColor(name)
        if alpha is not None:
            color.setAlpha(alpha)
        return color


_QSS = """
QWidget {{
    background: {bg};
    color: {text};
    font-size: 13px;
}}

QMainWindow, QDialog {{ background: {bg}; }}

/* ---- containers ------------------------------------------------- */
QFrame[card="true"] {{
    background: {surface};
    border: 1px solid {border_soft};
    border-radius: 12px;
}}
QGroupBox {{
    background: {surface};
    border: 1px solid {border_soft};
    border-radius: 12px;
    margin-top: 22px;
    padding: 12px 12px 12px 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 2px 6px;
    color: {text_dim};
    background: transparent;
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

/* ---- text ------------------------------------------------------- */
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 17px; font-weight: 700; }}
QLabel[role="subtitle"] {{ color: {text_dim}; font-size: 12px; }}
QLabel[role="section"] {{ color: {text_dim}; font-weight: 600; font-size: 11px; }}
QLabel[role="mono"] {{ font-family: "{mono}"; font-size: 12px; }}
QLabel[role="hint"] {{ color: {text_faint}; font-size: 11px; }}
QLabel[role="error"] {{ color: {danger}; }}
QLabel[role="success"] {{ color: {success}; }}
QLabel[role="warning"] {{ color: {warning}; }}

/* ---- buttons ---------------------------------------------------- */
QPushButton {{
    background: {elevated};
    color: {text};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 7px 14px;
}}
QPushButton:hover  {{ background: #2a3140; border-color: #39414f; }}
QPushButton:pressed{{ background: #1d222b; }}
QPushButton:disabled {{ color: {text_faint}; background: #171b23; border-color: {border_soft}; }}
QPushButton:checked {{ background: {primary_dim}; border-color: {primary}; }}

QPushButton[variant="primary"] {{
    background: {primary};
    border-color: {primary};
    color: #06121f;
    font-weight: 700;
}}
QPushButton[variant="primary"]:hover   {{ background: #6ba0ff; }}
QPushButton[variant="primary"]:disabled {{ background: #223449; border-color: #223449; color: #6d7a8c; }}

QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; color: {text_dim}; }}
QPushButton[variant="ghost"]:hover {{ background: {elevated}; color: {text}; }}

QPushButton[variant="sampleA"] {{
    background: rgba(55, 217, 160, 0.12);
    border: 1px solid rgba(55, 217, 160, 0.45);
    color: {sample_a};
    font-size: 15px; font-weight: 700;
}}
QPushButton[variant="sampleA"]:hover   {{ background: rgba(55, 217, 160, 0.2); }}
QPushButton[variant="sampleA"]:checked {{ background: {sample_a}; color: #05231a; }}

QPushButton[variant="sampleB"] {{
    background: rgba(255, 180, 84, 0.12);
    border: 1px solid rgba(255, 180, 84, 0.45);
    color: {sample_b};
    font-size: 15px; font-weight: 700;
}}
QPushButton[variant="sampleB"]:hover   {{ background: rgba(255, 180, 84, 0.2); }}
QPushButton[variant="sampleB"]:checked {{ background: {sample_b}; color: #2b1a03; }}

QPushButton[variant="voteA"] {{
    background: {surface_alt}; border: 1px solid {sample_a}; color: {sample_a};
    font-size: 15px; font-weight: 700; padding: 12px;
}}
QPushButton[variant="voteA"]:hover:!disabled {{ background: rgba(55, 217, 160, 0.16); }}
QPushButton[variant="voteB"] {{
    background: {surface_alt}; border: 1px solid {sample_b}; color: {sample_b};
    font-size: 15px; font-weight: 700; padding: 12px;
}}
QPushButton[variant="voteB"]:hover:!disabled {{ background: rgba(255, 180, 84, 0.16); }}
QPushButton[variant="vote"]:disabled {{ border-color: {border}; color: {text_faint}; }}

/* ---- inputs ----------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTimeEdit {{
    background: {surface_alt};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 5px 8px;
    selection-background-color: {primary};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {primary};
}}
QLineEdit[role="path"] {{ color: {text_dim}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{ width: 16px; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {elevated}; border: 1px solid {border};
    selection-background-color: {primary_dim}; outline: none;
}}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QCheckBox::indicator {{
    border: 1px solid {border}; border-radius: 4px; background: {surface_alt};
}}
QCheckBox::indicator:checked {{ background: {primary}; border-color: {primary}; }}
QRadioButton::indicator {{
    border: 1px solid {border}; border-radius: 8px; background: {surface_alt};
}}
QRadioButton::indicator:checked {{ background: {primary}; border: 4px solid {surface_alt}; }}

/* ---- sliders ---------------------------------------------------- */
QSlider::groove:horizontal {{
    height: 4px; background: {border}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {primary}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {text}; width: 12px; height: 12px;
    margin: -5px 0; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}

/* ---- progress --------------------------------------------------- */
QProgressBar {{
    background: {surface_alt}; border: none; border-radius: 5px;
    height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {primary}; border-radius: 5px; }}

/* ---- tables / lists --------------------------------------------- */
QTableWidget, QTreeWidget, QListWidget {{
    background: {surface_alt};
    border: 1px solid {border_soft};
    border-radius: 8px;
    gridline-color: {grid};
}}
QHeaderView::section {{
    background: {surface}; color: {text_dim};
    border: none; border-bottom: 1px solid {border_soft};
    padding: 6px 8px; font-weight: 600;
}}
QTableWidget::item {{ padding: 4px 6px; }}
QTableWidget::item:selected {{ background: {primary_dim}; color: #ffffff; }}

/* ---- scrollbars ------------------------------------------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #3b4454; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {border}; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- misc ------------------------------------------------------- */
QToolTip {{
    background: {elevated}; color: {text};
    border: 1px solid {border}; border-radius: 6px; padding: 5px 8px;
}}
QMenuBar {{ background: {bg}; border-bottom: 1px solid {border_soft}; }}
QMenuBar::item {{ padding: 6px 10px; background: transparent; border-radius: 6px; }}
QMenuBar::item:selected {{ background: {elevated}; }}
QMenu {{ background: {elevated}; border: 1px solid {border}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 6px 20px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {primary_dim}; }}
QMenu::separator {{ height: 1px; background: {border_soft}; margin: 4px 8px; }}
QStatusBar {{ background: {bg}; border-top: 1px solid {border_soft}; color: {text_dim}; }}
QStatusBar::item {{ border: none; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 4px; }}
QToolButton:hover {{ background: {elevated}; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 2px; }}
"""

#: Fallback monospace stack for the QSS ``font-family`` slots.
_MONO_STACK = '"JetBrains Mono", "Fira Code", "SF Mono", "Consolas", "DejaVu Sans Mono", monospace'


def _build_stylesheet() -> str:
    return _QSS.format(
        bg=Palette.bg,
        surface=Palette.surface,
        surface_alt=Palette.surface_alt,
        elevated=Palette.elevated,
        border=Palette.border,
        border_soft=Palette.border_soft,
        text=Palette.text,
        text_dim=Palette.text_dim,
        text_faint=Palette.text_faint,
        primary=Palette.primary,
        primary_dim=Palette.primary_dim,
        sample_a=Palette.sample_a,
        sample_b=Palette.sample_b,
        success=Palette.success,
        danger=Palette.danger,
        warning=Palette.warning,
        grid=Palette.grid,
        mono=_MONO_STACK,
    )


STYLESHEET = _build_stylesheet()


def font_family() -> str:
    """Pick the nicest available UI font for this platform."""
    families = set(QtGui.QFontDatabase.families())
    for name in ("Inter", "SF Pro Text", "Segoe UI Variable Text", "Segoe UI",
                 "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans", "Helvetica Neue"):
        if name in families:
            return name
    return QtWidgets.QApplication.font().family()  # pragma: no cover - exotic systems


def scaled_font(font: "QtGui.QFont", delta_pt: float = 0.0, min_pt: float = 7.0) -> "QtGui.QFont":
    """A copy of ``font`` resized by ``delta_pt`` points.

    Widgets styled by the stylesheet carry a **pixel**-sized font, and
    ``QFont.pointSizeF()`` returns ``-1`` for those.  Doing plain arithmetic on
    it therefore produced a size of 0 and made Qt complain
    (``QFont::setPointSizeF: Point size <= 0``) on every repaint, so the unit
    actually in use is detected and adjusted instead.
    """
    result = QtGui.QFont(font)
    if delta_pt == 0.0:
        return result
    if result.pointSizeF() > 0.0:
        result.setPointSizeF(max(min_pt, result.pointSizeF() + delta_pt))
    elif result.pixelSize() > 0:
        # points -> pixels at 96 dpi, rounded to whole pixels
        result.setPixelSize(max(1, round(result.pixelSize() + delta_pt * 96 / 72)))
    else:  # pragma: no cover - font with neither unit set
        result.setPointSizeF(max(min_pt, 10.0 + delta_pt))
    return result


def apply_theme(app: "QtWidgets.QApplication", *, font_size: int = 13) -> None:
    """Install the dark palette, stylesheet and default font on ``app``."""
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(Palette.bg))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(Palette.text))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(Palette.surface_alt))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(Palette.surface))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(Palette.text))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(Palette.elevated))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(Palette.text))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(Palette.primary))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#06121f"))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(Palette.elevated))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(Palette.text))
    palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(Palette.text_faint))
    palette.setColor(
        QtGui.QPalette.ColorGroup.Disabled,
        QtGui.QPalette.ColorRole.Text,
        QtGui.QColor(Palette.text_faint),
    )
    palette.setColor(
        QtGui.QPalette.ColorGroup.Disabled,
        QtGui.QPalette.ColorRole.ButtonText,
        QtGui.QColor(Palette.text_faint),
    )
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
    font = QtGui.QFont(font_family(), font_size)
    font.setHintingPreference(QtGui.QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)


def repolish(widget: "QtWidgets.QWidget") -> None:
    """Re-evaluate a widget's stylesheet after a ``variant`` change."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def make_icon(name: str, color: str = Palette.text, size: int = 18) -> "QtGui.QIcon":
    """Tiny vector icons so the app needs no asset files."""
    icon = QtGui.QIcon()
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(max(1.4, size / 12.0))
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    s = size
    if name == "play":
        painter.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(0.28 * s, 0.18 * s), QtCore.QPointF(0.82 * s, 0.5 * s), QtCore.QPointF(0.28 * s, 0.82 * s)]))
    elif name == "pause":
        painter.drawLine(QtCore.QPointF(0.36 * s, 0.2 * s), QtCore.QPointF(0.36 * s, 0.8 * s))
        painter.drawLine(QtCore.QPointF(0.64 * s, 0.2 * s), QtCore.QPointF(0.64 * s, 0.8 * s))
    elif name == "stop":
        painter.drawRect(QtCore.QRectF(0.26 * s, 0.26 * s, 0.48 * s, 0.48 * s))
    elif name == "loop":
        path = QtGui.QPainterPath()
        path.moveTo(0.22 * s, 0.62 * s)
        path.arcTo(QtCore.QRectF(0.22 * s, 0.28 * s, 0.56 * s, 0.44 * s), 180, -180)
        path.moveTo(0.78 * s, 0.38 * s)
        path.lineTo(0.78 * s, 0.22 * s)
        path.lineTo(0.62 * s, 0.3 * s)
        path.moveTo(0.78 * s, 0.38 * s)
        path.lineTo(0.62 * s, 0.3 * s)
        painter.drawPath(path)
    elif name == "volume":
        path = QtGui.QPainterPath()
        path.moveTo(0.2 * s, 0.4 * s)
        path.lineTo(0.34 * s, 0.4 * s)
        path.lineTo(0.5 * s, 0.26 * s)
        path.lineTo(0.5 * s, 0.74 * s)
        path.lineTo(0.34 * s, 0.6 * s)
        path.lineTo(0.2 * s, 0.6 * s)
        path.closeSubpath()
        painter.setBrush(QtGui.QBrush(QtGui.QColor(color)))
        painter.drawPath(path)
        painter.drawArc(QtCore.QRectF(0.5 * s, 0.3 * s, 0.34 * s, 0.4 * s), -60 * 16, 120 * 16)
    elif name == "mute":
        path = QtGui.QPainterPath()
        path.moveTo(0.2 * s, 0.4 * s)
        path.lineTo(0.34 * s, 0.4 * s)
        path.lineTo(0.5 * s, 0.26 * s)
        path.lineTo(0.5 * s, 0.74 * s)
        path.lineTo(0.34 * s, 0.6 * s)
        path.lineTo(0.2 * s, 0.6 * s)
        path.closeSubpath()
        painter.setBrush(QtGui.QBrush(QtGui.QColor(color)))
        painter.drawPath(path)
        painter.drawLine(QtCore.QPointF(0.6 * s, 0.36 * s), QtCore.QPointF(0.84 * s, 0.64 * s))
        painter.drawLine(QtCore.QPointF(0.84 * s, 0.36 * s), QtCore.QPointF(0.6 * s, 0.64 * s))
    elif name == "folder":
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(0.18 * s, 0.74 * s), QtCore.QPointF(0.18 * s, 0.28 * s),
            QtCore.QPointF(0.42 * s, 0.28 * s), QtCore.QPointF(0.5 * s, 0.38 * s),
            QtCore.QPointF(0.82 * s, 0.38 * s), QtCore.QPointF(0.82 * s, 0.74 * s),
        ]))
        painter.drawLine(QtCore.QPointF(0.18 * s, 0.74 * s), QtCore.QPointF(0.82 * s, 0.74 * s))
    elif name == "export":
        painter.drawLine(QtCore.QPointF(0.5 * s, 0.2 * s), QtCore.QPointF(0.5 * s, 0.6 * s))
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(0.34 * s, 0.42 * s), QtCore.QPointF(0.5 * s, 0.2 * s), QtCore.QPointF(0.66 * s, 0.42 * s),
        ]))
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(0.24 * s, 0.56 * s), QtCore.QPointF(0.24 * s, 0.8 * s),
            QtCore.QPointF(0.76 * s, 0.8 * s), QtCore.QPointF(0.76 * s, 0.56 * s),
        ]))
    elif name == "refresh":
        painter.drawArc(QtCore.QRectF(0.24 * s, 0.24 * s, 0.52 * s, 0.52 * s), 60 * 16, 260 * 16)
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(0.62 * s, 0.16 * s), QtCore.QPointF(0.78 * s, 0.24 * s), QtCore.QPointF(0.66 * s, 0.36 * s),
        ]))
    painter.end()
    icon.addPixmap(pixmap)
    return icon
