"""Qt binding compatibility layer.

flacblind is written against the common subset of PyQt6 and PySide6.  This
module picks a binding (PyQt6 by default, PySide6 as a fallback, overridable
with ``FLACBLIND_QT_API``) and re-exports the pieces the UI needs, so the rest
of the code never has to care.

Rules that keep the code portable:

* always spell enums out in full (``Qt.AlignmentFlag.AlignLeft``) — PyQt6
  dropped the unscoped aliases, PySide6 keeps both;
* use ``Signal``/``Slot`` from here instead of the binding's own names;
* never pass a Python ``enum.Enum`` through a signal (PyQt6 marshals them
  badly) — use ``int`` or ``object``.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType
from typing import Any

__all__ = [
    "QT_API",
    "BINDING",
    "Qt",
    "QtCore",
    "QtGui",
    "QtWidgets",
    "QtMultimedia",
    "QObject",
    "QThread",
    "QApplication",
    "QWidget",
    "Signal",
    "Slot",
    "Property",
    "qt_exec",
    "media_available",
    "binding_version",
]

_REQUESTED = os.environ.get("FLACBLIND_QT_API", "").strip().lower()


def _load(requested: str) -> tuple[str, tuple[ModuleType, ...], Any, Any, Any]:
    """Import one binding, returning (name, modules, Signal, Slot, Property)."""
    candidates = (requested,) if requested in ("pyqt6", "pyside6") else ("pyqt6", "pyside6")
    errors: list[str] = []
    for name in candidates:
        try:
            if name == "pyqt6":
                from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore[import-not-found]

                try:
                    from PyQt6 import QtMultimedia  # type: ignore[import-not-found]
                except ImportError:  # Qt Multimedia is an optional wheel part
                    QtMultimedia = None  # type: ignore[assignment]
                return name, (QtCore, QtGui, QtWidgets, QtMultimedia), (
                    QtCore.pyqtSignal, QtCore.pyqtSlot, QtCore.pyqtProperty
                )
            from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore[import-not-found]

            try:
                from PySide6 import QtMultimedia  # type: ignore[import-not-found]
            except ImportError:
                QtMultimedia = None  # type: ignore[assignment]
            return name, (QtCore, QtGui, QtWidgets, QtMultimedia), (
                QtCore.Signal, QtCore.Slot, QtCore.Property
            )
        except ImportError as exc:  # pragma: no cover - depends on the install
            errors.append(f"{name}: {exc}")
    raise ImportError(
        "flacblind needs PyQt6 or PySide6.\n"
        "Install one of them, e.g.:\n"
        "  pip install PyQt6      (or: pip install PySide6)\n\n"
        "Details:\n  " + "\n  ".join(errors)
    )


BINDING, _MODULES, _SIGNALS, QT_API = None, (), None, "unavailable"
_name, modules, signals = _load(_REQUESTED)
QtCore, QtGui, QtWidgets, QtMultimedia = modules
Signal, Slot, Property = signals
BINDING = _name
QT_API = _name
Qt = QtCore.Qt

QApplication = QtWidgets.QApplication
QWidget = QtWidgets.QWidget
QObject = QtCore.QObject
QThread = QtCore.QThread


def qt_exec(app: QApplication) -> int:
    """``app.exec()`` under both bindings (PyQt6 kept only ``exec``)."""
    return int(app.exec())


def media_available() -> bool:
    """Is Qt Multimedia usable in this install?"""
    return QtMultimedia is not None and hasattr(QtMultimedia, "QMediaPlayer")


def binding_version() -> str:
    """Human readable binding + Qt version, for the About box."""
    if BINDING == "pyqt6":
        return f"PyQt {QtCore.PYQT_VERSION_STR} / Qt {QtCore.QT_VERSION_STR}"
    if BINDING == "pyside6":
        return f"PySide {QtCore.__version__.split('.')[0]} / Qt {QtCore.qVersion()}"
    return "none"  # pragma: no cover


if sys.version_info < (3, 10):  # pragma: no cover - guarded by pyproject anyway
    raise RuntimeError("flacblind requires Python 3.10 or newer.")
