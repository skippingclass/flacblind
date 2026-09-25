"""Qt-free domain logic for flacblind.

Importing this package pulls in nothing but the standard library (plus numpy
when it happens to be installed), so the whole audio/statistics core can be
tested on a headless machine.
"""

from __future__ import annotations

from .abx import ABXRound, ABXSession, TestConfig
from .errors import (
    DependencyError,
    FlacBlindError,
    MediaError,
    PlaybackError,
    PreparationCancelled,
    PreparationError,
    SessionError,
    UnsupportedAudioError,
)
from .ffmpeg import FFmpeg
from .models import (
    AudioConfig,
    AudioKind,
    PreparedPair,
    PreparedSample,
    RoundRecord,
    SampleKey,
    SegmentSpec,
    SessionReport,
    SessionStats,
)
from .prepare import AudioPreparer, PrepareRequest, SourceSpec
from .stats import p_value_greater, verdict
from .tempstore import TempWorkspace, sweep_stale_workspaces

__all__ = [
    "ABXRound",
    "ABXSession",
    "AudioConfig",
    "AudioKind",
    "AudioPreparer",
    "DependencyError",
    "FFmpeg",
    "FlacBlindError",
    "MediaError",
    "PlaybackError",
    "PrepareRequest",
    "PreparationCancelled",
    "PreparationError",
    "PreparedPair",
    "PreparedSample",
    "RoundRecord",
    "SampleKey",
    "SegmentSpec",
    "SessionError",
    "SessionReport",
    "SessionStats",
    "SourceSpec",
    "TempWorkspace",
    "TestConfig",
    "UnsupportedAudioError",
    "p_value_greater",
    "sweep_stale_workspaces",
    "verdict",
]
