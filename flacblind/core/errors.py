"""Exception hierarchy for flacblind.

Every error carries a human readable ``hint`` that explains *how* to fix the
problem; the GUI shows it next to the error banner instead of a raw traceback.
"""

from __future__ import annotations

__all__ = [
    "FlacBlindError",
    "DependencyError",
    "MediaError",
    "UnsupportedAudioError",
    "PreparationError",
    "PreparationCancelled",
    "PlaybackError",
    "SessionError",
]


class FlacBlindError(Exception):
    """Base class for all errors raised by flacblind."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message

    @property
    def user_text(self) -> str:
        """Message plus remediation hint, ready to be shown in the UI."""
        return f"{self.message}\n{self.hint}" if self.hint else self.message


class DependencyError(FlacBlindError):
    """An external program (usually ffmpeg) is missing or unusable."""


class MediaError(FlacBlindError):
    """A media file could not be probed, decoded or converted."""


class UnsupportedAudioError(MediaError):
    """The file is not a decodable audio stream."""


class PreparationError(FlacBlindError):
    """The audio preparation pipeline failed."""


class PreparationCancelled(Exception):
    """Raised inside worker threads when the user aborts preparation."""


class PlaybackError(FlacBlindError):
    """No playback backend is available, or the backend failed."""


class SessionError(FlacBlindError):
    """The ABX session was driven into an invalid state."""
