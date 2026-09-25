"""Sample alignment of the two prepared renders.

Two files that encode the same music rarely start at the same sample: an MP3
carries encoder priming/padding (typically 1105 samples ≈ 25 ms) and a trimmed
master can start a few milliseconds early.  For an ABX test that matters — you
are supposed to compare the *same* moment of the song, and an 18 ms offset is
both a tiny bit of pre-echo you can hear and a lie in the waveform overlay.

The fix is cheap: cross-correlate a decimated copy of both segments, find the
lag, and re-render the later file from ``start + lag`` with the exact length of
the earlier one.  Without numpy the step is skipped and the difference is
reported as a warning instead of being silently ignored.
"""

from __future__ import annotations

import contextlib
import wave
from dataclasses import dataclass

from .models import PreparedSample

try:  # pragma: no cover - numpy is a soft dependency
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None  # type: ignore[assignment]

#: Whether the correlation-based alignment can run at all.
HAVE_NUMPY = _np is not None

__all__ = [
    "Alignment",
    "HAVE_NUMPY",
    "MIN_ALIGN_MS",
    "MAX_OFFSET_SEC",
    "estimate_offset_samples",
    "needs_alignment",
]

#: Decimation factor target: ~8 kHz is plenty for a lag of a few hundred ms
#: and keeps the FFT tiny.
TARGET_RATE = 8000
#: Correlate over this much audio (seconds) — the start of the segment.
WINDOW_SEC = 6.0
#: Largest lag we are willing to correct, in seconds.
MAX_OFFSET_SEC = 0.35
#: Below this the difference is inaudible and not worth a re-render.
MIN_ALIGN_MS = 2.0


@dataclass(frozen=True, slots=True)
class Alignment:
    """Result of the alignment check for one pair of samples."""

    offset_ms: float = 0.0
    aligned: bool = False
    detail: str = ""

    @property
    def meaningful(self) -> bool:
        return abs(self.offset_ms) >= MIN_ALIGN_MS


def _read_decimated(path, target_rate: int) -> "tuple[object, int] | None":  # noqa: ANN001
    """``(mono decimated samples, decimation step)`` or ``None`` on failure."""
    if _np is None:
        return None
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            frames = min(handle.getnframes(), int(WINDOW_SEC * rate))
            raw = handle.readframes(frames)
    except (OSError, wave.Error):
        return None
    if width != 2:
        return None
    samples = _np.frombuffer(raw, dtype="<i2").astype(_np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    step = max(1, int(rate / target_rate))
    if step == 1:
        return samples, 1
    usable = (samples.size // step) * step
    return samples[:usable].reshape(-1, step).mean(axis=1), step


def estimate_offset_samples(
    path_a,
    path_b,
    *,
    max_offset_sec: float = MAX_OFFSET_SEC,
) -> int | None:
    """Signed lag of ``path_b`` relative to ``path_a``.

    The result is expressed in **samples of the source files** (not of the
    decimated signal the correlation runs on) and is accurate to about one
    decimation step, i.e. ~0.7 ms for 44.1 kHz material.  A positive result
    means B starts *later* than A, i.e. B has to be re-rendered from a slightly
    earlier position to line up.  ``None`` means "do not touch anything".
    """
    if _np is None:
        return None
    first_read = _read_decimated(path_a, TARGET_RATE)
    second_read = _read_decimated(path_b, TARGET_RATE)
    if first_read is None or second_read is None:
        return None
    (first, step), (second, _) = first_read, second_read
    size = min(first.size, second.size)
    if size < TARGET_RATE // 4:
        return None
    first = _np.ascontiguousarray(first[:size])
    second = _np.ascontiguousarray(second[:size])
    # Remove DC and normalise so the correlation peak is sharp.
    first -= first.mean()
    second -= second.mean()
    norm = float(_np.linalg.norm(first) * _np.linalg.norm(second))
    if norm <= 1e-9:
        return None
    # Zero-pad to at least len(a)+len(b)-1 so the correlation is linear: lag k
    # then lives at index k, and negative lags wrap to the end of the array.
    size_fft = 1 << max(1, (2 * size - 1).bit_length())
    spectrum = _np.fft.rfft(first, size_fft) * _np.conj(_np.fft.rfft(second, size_fft))
    correlation = _np.fft.irfft(spectrum, size_fft)
    correlation /= norm
    max_lag = int(max_offset_sec * TARGET_RATE)
    if max_lag >= size:
        max_lag = size - 1
    head = correlation[: max_lag + 1]          # lags  0 .. +max_lag
    tail = correlation[size_fft - max_lag :]    # lags -max_lag .. -1
    head_peak = int(_np.argmax(head))
    tail_peak = int(_np.argmax(tail))
    if float(head[head_peak]) >= float(tail[tail_peak]):
        lag, peak = head_peak, float(head[head_peak])
    else:
        # `tail` starts at array index size_fft - max_lag, i.e. at lag -max_lag.
        lag, peak = tail_peak - max_lag, float(tail[tail_peak])
    if peak < 0.2:  # no convincing correlation → do not touch anything
        return None
    # With this correlation convention a *delayed* second signal peaks at a
    # negative index, so flip the sign to keep "positive == B is late", then
    # convert from decimated samples back to source samples.
    return -lag * step


def needs_alignment(a: PreparedSample, b: PreparedSample) -> bool:
    """Is it worth correlating these two at all?"""
    if a.frames == 0 or b.frames == 0:
        return False
    if abs(a.frames - b.frames) <= 4 and a.sample_rate == b.sample_rate:
        return False
    return _np is not None


def alignment_of(a: PreparedSample, b: PreparedSample) -> Alignment:
    """Measure the offset of ``b`` relative to ``a`` (both prepared WAVs)."""
    if not needs_alignment(a, b):
        return Alignment()
    lag = estimate_offset_samples(a.wav_path, b.wav_path)
    if lag is None:
        return Alignment(detail="no reliable correlation between the two renders")
    if a.sample_rate != b.sample_rate:  # pragma: no cover - we always force one rate
        return Alignment(detail="sample rates differ")
    offset_ms = lag * 1000.0 / a.sample_rate
    return Alignment(offset_ms=offset_ms, aligned=abs(offset_ms) >= MIN_ALIGN_MS)
