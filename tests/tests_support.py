"""A prepared-pair stand-in so GUI tests do not need ffmpeg."""

from __future__ import annotations

import math
import tempfile
import wave
from pathlib import Path

from helpers import write_quiet_wav

from flacblind.core.models import (
    AudioFormat,
    AudioKind,
    Peaks,
    PreparedPair,
    PreparedSample,
    SampleKey,
    SampleStats,
)


def make_sample(key: SampleKey, wav_path: Path, seconds: float = 20.0) -> PreparedSample:
    rate = 44100
    buckets = 600
    # A plausible-looking envelope: a build-up, a loud middle, a fade-out.
    rms = []
    for i in range(buckets):
        t = i / buckets
        rms.append(0.05 + 0.45 * (1.0 if 0.25 < t < 0.8 else 0.3))
    return PreparedSample(
        key=key,
        kind=AudioKind.LOSSLESS if key is SampleKey.A else AudioKind.LOSSY,
        fmt=AudioFormat.FLAC if key is SampleKey.A else AudioFormat.MP3,
        source_path=wav_path.with_suffix(".flac"),
        wav_path=wav_path,
        duration_sec=seconds,
        sample_rate=rate,
        channels=2,
        frames=int(seconds * rate),
        peaks=Peaks(
            mins=tuple(-v for v in rms),
            maxs=tuple(rms),
            rms=tuple(rms),
            total_frames=int(seconds * rate),
            sample_rate=rate,
        ),
        stats=SampleStats(-8.2, -15.1, 6.9),
    )


class StubPair:
    """A real :class:`PreparedPair` over two synthetic-but-valid WAV files."""

    def __init__(self, seconds: float = 20.0, directory: Path | None = None) -> None:
        root = directory or Path(tempfile.mkdtemp(prefix="fb-test-pair-"))
        self.directory = root
        self.pair = PreparedPair(
            sample_a=make_sample(SampleKey.A, write_quiet_wav(root / "a.wav"), seconds),
            sample_b=make_sample(SampleKey.B, write_quiet_wav(root / "b.wav"), seconds),
            segment=None,  # type: ignore[arg-type]
        )
