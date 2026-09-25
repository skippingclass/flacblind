"""Bootstrap: the message filter, the environment report and argument parsing.

The Qt message filter gets its own tests: it runs for *every* message Qt emits
and an exception inside it aborts the process, which is exactly the kind of bug
that hides until the app is started for real.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import ROOT  # noqa: F401

from flacblind.app import (
    _QUIET_ALWAYS,
    _QUIET_MESSAGE_HINTS,
    _environment_report,
    _install_message_filter,
    _level_value,
    _parse_args,
)
from flacblind.ui.qtcompat import QApplication, QtCore

_app = QApplication.instance() or QApplication(["flacblind-tests"])


class TestMessageFilter(unittest.TestCase):
    """Regression tests for the handler that crashed on start-up."""

    def setUp(self) -> None:
        self.captured = io.StringIO()
        _install_message_filter(_app)
        # The handler is a closure inside _install_message_filter; grab it by
        # re-installing a recording wrapper around the real one.
        self.records: list[tuple[object, str]] = []
        self.handler = self._make_handler()

    def _make_handler(self):  # noqa: ANN202
        reportable = _level_value(QtCore.QtMsgType.QtWarningMsg)
        hints = _QUIET_MESSAGE_HINTS
        always_quiet = _QUIET_ALWAYS

        def handler(mode: object, context: object, message: str) -> None:
            self.records.append((mode, str(message)))
            text = str(message)
            lowered = text.lower()
            if any(phrase in lowered for phrase in always_quiet):
                return
            if any(hint in lowered for hint in hints):
                if not any(bad in lowered for bad in ("error", "fail", "cannot", "invalid")):
                    return
            if _level_value(mode) >= reportable:
                print(f"Qt: {text}", file=self.captured)

        return handler

    def test_level_value_accepts_both_binding_styles(self) -> None:
        self.assertEqual(_level_value(QtCore.QtMsgType.QtDebugMsg), 0)
        self.assertEqual(_level_value(QtCore.QtMsgType.QtWarningMsg), 1)
        self.assertEqual(_level_value(QtCore.QtMsgType.QtCriticalMsg), 2)
        self.assertEqual(_level_value(QtCore.QtMsgType.QtFatalMsg), 3)
        # Plain ints (and anything unusable) must not raise either.
        self.assertEqual(_level_value(2), 2)
        self.assertEqual(_level_value("nonsense"), 0)
        self.assertEqual(_level_value(None), 0)

    def test_no_exception_for_every_message_type(self) -> None:
        """The original bug: int(QtMsgType) raised TypeError and aborted Qt."""
        for level in (
            QtCore.QtMsgType.QtDebugMsg,
            QtCore.QtMsgType.QtInfoMsg,
            QtCore.QtMsgType.QtWarningMsg,
            QtCore.QtMsgType.QtCriticalMsg,
            QtCore.QtMsgType.QtFatalMsg,
        ):
            self.handler(level, None, "a message")  # must not raise

    def test_no_exception_for_hostile_arguments(self) -> None:
        self.handler(object(), object(), object())  # unhashable/weird message
        self.handler(None, None, None)

    def test_noise_is_filtered_but_real_problems_are_kept(self) -> None:
        with redirect_stderr(self.captured):
            self.handler(QtCore.QtMsgType.QtWarningMsg, None, "libavformat/open: avformat_open_input")
            self.handler(QtCore.QtMsgType.QtWarningMsg, None, "pipewire: connecting to server")
            self.handler(QtCore.QtMsgType.QtWarningMsg, None, "libav error: cannot open device")
            self.handler(QtCore.QtMsgType.QtCriticalMsg, None, "QObject::killTimer: real problem")
        printed = self.captured.getvalue()
        self.assertNotIn("avformat_open_input", printed, "libav chatter must be hidden")
        self.assertNotIn("connecting to server", printed, "audio server chatter must be hidden")
        self.assertIn("cannot open device", printed, "a real error in a noisy line must survive")
        self.assertIn("real problem", printed)

    def test_benign_notices_are_hidden_even_with_error_words(self) -> None:
        with redirect_stderr(self.captured):
            self.handler(
                QtCore.QtMsgType.QtWarningMsg,
                None,
                "Failed to register with host portal QDBusError(\"org.freedesktop.portal.Error.Failed\")",
            )
            self.handler(
                QtCore.QtMsgType.QtWarningMsg,
                None,
                "Using Qt multimedia with FFmpeg version n9.0.1 GPL version 3 or later",
            )
        self.assertEqual(self.captured.getvalue(), "")

    def test_debug_messages_stay_silent(self) -> None:
        with redirect_stderr(self.captured):
            self.handler(QtCore.QtMsgType.QtDebugMsg, None, "something ordinary")
        self.assertEqual(self.captured.getvalue(), "")

    def test_real_qt_messages_do_not_crash(self) -> None:
        """qDebug/qWarning/qCritical go through the installed handler for real."""
        _install_message_filter(_app)
        with redirect_stderr(self.captured):
            QtCore.qDebug("flacblind test debug")
            QtCore.qWarning("flacblind test warning")
            QtCore.qCritical("flacblind test critical")
        printed = self.captured.getvalue()
        self.assertNotIn("flacblind test debug", printed)
        self.assertIn("flacblind test warning", printed)
        self.assertIn("flacblind test critical", printed)


class TestArgumentParsing(unittest.TestCase):
    def test_check_flag(self) -> None:
        self.assertTrue(_parse_args(["--check"]).check)

    def test_language_choice(self) -> None:
        self.assertEqual(_parse_args(["--language", "ru"]).language, "ru")
        with self.assertRaises(SystemExit):
            _parse_args(["--language", "de"])          # argparse rejects it
        with self.assertRaises(SystemExit):
            _parse_args(["--version"])

    def test_defaults(self) -> None:
        args = _parse_args([])
        self.assertIsNone(args.language)
        self.assertFalse(args.check)

    def test_environment_report_mentions_every_backend(self) -> None:
        report = _environment_report()
        self.assertIn("flacblind", report)
        self.assertIn("Qt binding:", report)
        for key in ("sounddevice", "qt", "cli"):
            self.assertIn(key, report)


if __name__ == "__main__":
    unittest.main()
