"""WAV reading and waveform envelope extraction.

The waveform widget repaints its playhead every frame, so the envelope is
computed **once** here into a small immutable :class:`Peaks` structure (a few
thousand buckets) instead of touching raw samples at paint time.

``numpy`` is used when available (fast, chunked) with a pure-stdlib fallback
so the app still runs on a bare Python install.
"""

from __future__ import annotations

import array
import contextlib
import wave
from pathlib import Path
from typing import Iterator, Sequence

from .errors import UnsupportedAudioError
from .models import Peaks, clamp, linear_to_db, mean

try:  # pragma: no cover - exercised by whichever branch is installed
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None  # type: ignore[assignment]

__all__ = [
    "HAVE_NUMPY",
    "WavInfo",
    "read_wav_info",
    "iter_wav_blocks",
    "compute_peaks",
    "peaks_from_file",
    "read_wav_float32",
    "compute_level_stats",
    "DEFAULT_BUCKETS",
    "synthesize_silence",
]

HAVE_NUMPY = _np is not None
DEFAULT_BUCKETS = 2400
_BLOCK_FRAMES = 1 << 16


class WavInfo:
    """Header of a PCM WAV file."""

    __slots__ = ("path", "channels", "sample_rate", "sample_width", "frames")

    def __init__(
        self,
        path: Path,
        channels: int,
        sample_rate: int,
        sample_width: int,
        frames: int,
    ) -> None:
        self.path = path
        self.channels = channels
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.frames = frames

    @property
    def duration_sec(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"WavInfo({self.path.name}, {self.channels}ch, {self.sample_rate} Hz, "
            f"{self.sample_width * 8}-bit, {self.duration_sec:.2f} s)"
        )


def read_wav_info(path: str | Path) -> WavInfo:
    """Read a WAV header, raising :class:`UnsupportedAudioError` if invalid."""
    path = Path(path)
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as handle:
            return WavInfo(
                path=path,
                channels=handle.getnchannels(),
                sample_rate=handle.getframerate(),
                sample_width=handle.getsampwidth(),
                frames=handle.getnframes(),
            )
    except wave.Error as exc:
        raise UnsupportedAudioError(
            f"{path.name} is not a readable WAV file: {exc}",
            hint="Re-prepare the track, or install ffmpeg so conversion can run.",
        ) from exc
    except FileNotFoundError as exc:
        raise UnsupportedAudioError(f"Temporary file disappeared: {path.name}") from exc


def _decode(raw: bytes, sample_width: int) -> "array.array[float]":
    """Convert a raw PCM block to float samples in ``[-1, 1]`` (mono mixdown)."""
    if sample_width == 1:  # unsigned 8 bit
        data = array.array("B")
        data.frombytes(raw)
        return array.array("f", ((value - 128) / 128.0 for value in data))
    if sample_width == 2:
        data = array.array("h")
        data.frombytes(raw)
        return array.array("f", (value / 32768.0 for value in data))
    if sample_width == 3:
        count = len(raw) // 3
        values = array.array("f", bytes(4 * count))
        for i in range(count):
            b0, b1, b2 = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
            value = (b0 | (b1 << 8) | (b2 << 16)) - 0x800000
            values[i] = value / 8388608.0
        return values
    if sample_width == 4:
        data = array.array("i")
        data.frombytes(raw)
        return array.array("f", (clamp(value / 2147483648.0, -1.0, 1.0) for value in data))
    raise UnsupportedAudioError(f"Unsupported WAV sample width: {sample_width * 8} bit")


def iter_wav_blocks(
    path: str | Path,
    *,
    block_frames: int = _BLOCK_FRAMES,
    mono: bool = True,
) -> Iterator[tuple[array.array, int, int]]:
    """Yield ``(samples, frame_offset, frame_count)`` chunks of float samples.

    ``samples`` is a flat array; when ``mono`` is set it holds one value per
    frame (channels averaged), otherwise one value per channel per frame.
    """
    path = Path(path)
    with contextlib.closing(wave.open(str(path), "rb")) as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        remaining = handle.getnframes()
        offset = 0
        while remaining > 0:
            raw = handle.readframes(min(block_frames, remaining))
            if not raw:
                break
            samples = _decode(raw, width)
            count = len(samples) // channels
            if mono and channels > 1:
                block = _mixdown(samples, channels, count)
            else:
                block = samples
            yield block, offset, count
            offset += count
            remaining -= count


def _mixdown(samples: array.array, channels: int, count: int) -> array.array:
    if channels == 2:
        return array.array(
            "f", ((samples[i] + samples[i + 1]) * 0.5 for i in range(0, count * 2, 2))
        )
    if channels == 1:
        return array.array("f", samples[:count])
    out = array.array("f", bytes(4 * count))
    for i in range(count):
        base = i * channels
        out[i] = sum(samples[base : base + channels]) / channels
    return out


def compute_peaks(
    blocks: Sequence[tuple[Sequence[float], int, int]],
    total_frames: int,
    sample_rate: int,
    buckets: int = DEFAULT_BUCKETS,
) -> Peaks:
    """Reduce a stream of blocks into a min/max/RMS envelope.

    ``blocks`` is the sequence produced by :func:`iter_wav_blocks`; it is
    consumed twice (once for numpy, once for the fallback) only when numpy is
    missing, so callers should materialise it via :func:`peaks_from_file`.
    """
    buckets = max(1, min(buckets, max(1, total_frames)))
    if total_frames <= 0:
        return Peaks((), (), (), 0, sample_rate)
    if _np is not None:
        return _peaks_numpy(blocks, total_frames, sample_rate, buckets)
    return _peaks_python(blocks, total_frames, sample_rate, buckets)


def _bucket_starts(total_frames: int, buckets: int) -> "list[int]":
    return [(i * total_frames) // buckets for i in range(buckets)]


def _peaks_numpy(blocks, total_frames: int, sample_rate: int, buckets: int) -> Peaks:  # type: ignore[no-untyped-def]
    np = _np
    starts = _bucket_starts(total_frames, buckets)
    boundaries = starts + [total_frames]
    counts = np.diff(np.asarray(boundaries, dtype=np.int64))
    mins = np.full(buckets, np.inf, dtype=np.float32)
    maxs = np.full(buckets, -np.inf, dtype=np.float32)
    sumsq = np.zeros(buckets, dtype=np.float64)

    for samples, offset, count in blocks:
        if count <= 0:
            continue
        chunk = np.frombuffer(memoryview(samples).cast("B"), dtype=np.float32)
        if chunk.size < count:  # pragma: no cover - defensive
            chunk = chunk[:count]
            count = int(chunk.size)
        # Map every frame of the chunk onto its bucket, then reduce with
        # reduceat: a C-level segmented reduction.  (np.minimum.at would be
        # orders of magnitude slower on a 10 minute file.)
        idx = (np.arange(offset, offset + count, dtype=np.int64) * buckets) // total_frames
        local = np.concatenate(([0], np.flatnonzero(np.diff(idx)) + 1))
        seg_min = np.minimum.reduceat(chunk, local)
        seg_max = np.maximum.reduceat(chunk, local)
        seg_sq = np.add.reduceat(chunk.astype(np.float64) ** 2, local)
        hit = idx[local]  # distinct bucket per segment, so plain fancy-indexing is safe
        mins[hit] = np.minimum(mins[hit], seg_min)
        maxs[hit] = np.maximum(maxs[hit], seg_max)
        sumsq[hit] += seg_sq

    mins[~np.isfinite(mins)] = 0.0
    maxs[~np.isfinite(maxs)] = 0.0
    rms = np.sqrt(np.divide(sumsq, np.maximum(counts, 1), dtype=np.float64))
    return Peaks(
        mins=tuple(float(v) for v in mins),
        maxs=tuple(float(v) for v in maxs),
        rms=tuple(float(v) for v in rms),
        total_frames=total_frames,
        sample_rate=sample_rate,
    )


def _peaks_python(blocks, total_frames: int, sample_rate: int, buckets: int) -> Peaks:  # type: ignore[no-untyped-def]
    """Pure-stdlib fallback for installs without numpy."""
    starts = _bucket_starts(total_frames, buckets)
    mins = [0.0] * buckets
    maxs = [0.0] * buckets
    sumsq = [0.0] * buckets
    for samples, offset, count in blocks:
        for i in range(count):
            value = samples[i]
            bucket = ((offset + i) * buckets) // total_frames
            if value < mins[bucket]:
                mins[bucket] = value
            elif value > maxs[bucket]:
                maxs[bucket] = value
            sumsq[bucket] += value * value
    rms = [0.0] * buckets
    for b in range(buckets):
        width = (starts[b + 1] if b + 1 < buckets else total_frames) - starts[b]
        rms[b] = (sumsq[b] / width) ** 0.5 if width > 0 else 0.0
    return Peaks(tuple(mins), tuple(maxs), tuple(rms), total_frames, sample_rate)


def peaks_from_file(
    path: str | Path,
    buckets: int = DEFAULT_BUCKETS,
    *,
    blocks: Sequence[tuple[Sequence[float], int, int]] | None = None,
) -> tuple[Peaks, WavInfo]:
    """Compute the envelope of a WAV file, returning it with the header info."""
    info = read_wav_info(path)
    if info.frames <= 0:
        return Peaks((), (), (), 0, info.sample_rate), info
    source = blocks if blocks is not None else list(iter_wav_blocks(info.path))
    return compute_peaks(source, info.frames, info.sample_rate, buckets), info


def read_wav_float32(path: str | Path) -> "tuple[object, int, int]":
    """Read a WAV into an ``(frames, channels)`` float32 array plus the rate.

    Only 16 bit PCM is handled natively; other widths are upsampled through
    the generic block decoder.  Used by the sounddevice playback backend.
    """
    info = read_wav_info(path)
    if _np is None:  # pragma: no cover - numpy is a soft dependency
        raise UnsupportedAudioError(
            "Sample-accurate playback needs numpy.",
            hint="Install numpy (pip install numpy) or choose the Qt playback backend.",
        )
    np = _np
    with contextlib.closing(wave.open(str(info.path), "rb")) as handle:
        raw = handle.readframes(info.frames)
    if info.sample_width == 2:
        flat = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif info.sample_width == 1:
        flat = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif info.sample_width == 4:
        flat = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        flat = np.fromiter(_decode(raw, info.sample_width), dtype=np.float32, count=info.frames * info.channels)
    return flat.reshape(-1, info.channels), info.sample_rate


def compute_level_stats(peaks: Peaks) -> tuple[float, float, float]:
    """Return ``(peak_dbfs, rms_dbfs, crest_db)`` for an envelope."""
    if peaks.is_empty:
        return -120.0, -120.0, 0.0
    peak = max(max(abs(v) for v in peaks.mins), max(abs(v) for v in peaks.maxs), 1e-6)
    rms = max((mean(v * v for v in peaks.rms)) ** 0.5, 1e-6)
    return linear_to_db(peak), linear_to_db(rms), linear_to_db(peak / rms)


def silence_ratio(peaks: Peaks, threshold_db: float = -60.0) -> float:
    """Fraction of the envelope below ``threshold_db`` (0 = nothing silent)."""
    if peaks.is_empty:
        return 0.0
    limit = 10 ** (threshold_db / 20.0)
    quiet = sum(1 for value in peaks.rms if value < limit)
    return quiet / len(peaks)


def synthesize_silence(path: str | Path, seconds: float = 0.5, sample_rate: int = 44100) -> Path:
    """Write a silent WAV (test helper for empty/edge-case files)."""
    path = Path(path)
    with contextlib.closing(wave.open(str(path), "wb")) as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return path
