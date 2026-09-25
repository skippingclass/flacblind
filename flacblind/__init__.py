"""flacblind — a blind ABX listening test for lossless vs. lossy audio.

The package is split in two halves:

``flacblind.core``
    Pure Python, Qt-free domain logic: media probing/normalising through
    ``ffmpeg``, waveform extraction, the ABX session state machine and the
    binomial statistics used to judge the result.  Everything in here can be
    imported and unit-tested without a GUI toolkit.

``flacblind.ui``
    The PyQt6 (or PySide6) presentation layer.  Long running work is pushed
    onto worker threads so the window never blocks.
"""

from __future__ import annotations

__all__ = [
    "__version__",
    "APP_NAME",
    "APP_ID",
]

__version__ = "2.0.0"
APP_NAME = "flacblind"
APP_ID = "io.github.flacblind"
