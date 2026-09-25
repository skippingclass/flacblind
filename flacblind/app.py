"""Application bootstrap: high DPI, theme, window, event loop."""

from __future__ import annotations

import argparse
import signal
import sys
import traceback
from typing import Sequence

from . import APP_ID, APP_NAME, __version__
from .core.settings import AppSettings
from .ui.qtcompat import (
    QT_API,
    QApplication,
    QtCore,
    QtWidgets,
    binding_version,
    media_available,
    qt_exec,
)
from .ui.theme import apply_theme

__all__ = ["main", "build_application"]


#: Qt Multimedia embeds libav and logs its input dumps to stderr; keep those
#: out of the user's terminal while still showing genuine warnings and errors.
#: Matched case-insensitively as substrings of the message.
_QUIET_MESSAGE_HINTS = (
    "libav",
    "ffmpeg",
    "avformat",
    "mov,mp4",
    "pulseaudio",
    "pipewire",
)

#: Notices that are always benign, even though they contain the word "error".
#: (The Wayland portal only finds our app id once the packaged .desktop file is
#: installed, so it fails on every start from a checkout.)
_QUIET_ALWAYS = (
    "failed to register with host portal",
)


def _level_value(level: object) -> int:
    """Numeric severity of a Qt message type.

    ``QtMsgType`` is a plain ``enum.Enum`` in PyQt6 (so ``int(level)`` and
    ``level >= …`` both raise ``TypeError``) and an int-like enum in PySide6.
    Going through ``.value`` works for both.
    """
    value = getattr(level, "value", level)
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def _install_message_filter(app: "QApplication") -> None:
    """Silence Qt Multimedia's libav chatter, keep real warnings.

    The handler must never raise: Qt calls it from C++ on every message, and
    an exception here aborts the process.
    """
    # Warnings and worse are worth seeing; debug/info chatter is not.
    reportable = _level_value(QtCore.QtMsgType.QtWarningMsg)

    def handler(mode: object, context: object, message: str) -> None:  # noqa: ARG001
        try:
            text = str(message)
            lowered = text.lower()
            if any(phrase in lowered for phrase in _QUIET_ALWAYS):
                return
            if any(hint in lowered for hint in _QUIET_MESSAGE_HINTS):
                # Noisy source: only surface it if it also looks like a problem.
                if not any(bad in lowered for bad in ("error", "fail", "cannot", "invalid")):
                    return
            if _level_value(mode) >= reportable:
                print(f"Qt: {text}", file=sys.stderr)
        except Exception:  # noqa: BLE001, S110 - a logger must never crash Qt
            pass

    try:
        QtCore.qInstallMessageHandler(handler)
    except Exception:  # noqa: BLE001, S110  # pragma: no cover - binding without API
        pass


def build_application(argv: Sequence[str] | None = None) -> "tuple[QApplication, QtWidgets.QWidget | None]":
    """Create the QApplication and the main window (without running the loop)."""
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    _install_message_filter(app)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("flacblind")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("flacblind")
    app.setDesktopFileName(APP_ID)
    apply_theme(app)
    from .ui.main_window import MainWindow

    window = MainWindow(AppSettings().load())
    return app, window


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flacblind",
        description="Blind ABX test between lossless and lossy audio (PyQt6 GUI).",
    )
    parser.add_argument("--version", action="version", version=f"flacblind {__version__}")
    parser.add_argument(
        "--language",
        choices=("en", "ru"),
        help="override the interface language for this run",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report ffmpeg / playback availability and exit",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _environment_report() -> str:
    from .core.ffmpeg import FFmpeg
    from .ui.player import available_backends

    ffmpeg = FFmpeg()
    lines = [
        f"flacblind {__version__}",
        f"Qt binding:   {binding_version()}",
        f"Qt Multimedia: {'available' if media_available() else 'missing'}",
        f"ffmpeg:       {ffmpeg.version if ffmpeg.available else 'NOT FOUND'}",
        f"ffprobe:      {'found' if ffmpeg.has_ffprobe else 'not found'}",
    ]
    for info in available_backends():
        mark = "✓" if info.available else "✗"
        lines.append(f"playback {mark} {info.key:<13} {info.detail}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point used by ``flacblind.py``, ``python -m flacblind`` and the script."""
    try:
        args = _parse_args(argv)
    except SystemExit as exit_error:  # --help / --version
        return int(exit_error.code or 0)

    if args.check:
        print(_environment_report())
        return 0

    try:
        app, window = build_application()
    except ImportError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.language:
        from .ui.i18n import Translator

        Translator.instance().set_language(args.language)

    # Ctrl+C should quit the app, not kill it mid-write.
    signal.signal(signal.SIGINT, lambda *_args: app.quit())
    signal_timer = QtCore.QTimer()
    signal_timer.start(250)
    signal_timer.timeout.connect(lambda: None)

    window.show()
    return qt_exec(app)


def _excepthook(exc_type, exc_value, exc_tb) -> None:  # pragma: no cover - last resort
    """Report unhandled GUI errors without losing the traceback."""
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(text, file=sys.stderr)
    try:
        from .ui.qtcompat import QtWidgets

        box = QtWidgets.QMessageBox()
        box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        box.setWindowTitle("flacblind")
        box.setText("Something went wrong.")
        box.setDetailedText(text)
        box.exec()
    except Exception:  # noqa: BLE001, S110 - the console message is enough
        pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
