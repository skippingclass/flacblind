"""Immutable data structures shared by the core logic and the UI.

Nothing in this module imports Qt, so the whole data model can be exercised
from plain unit tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "SampleKey",
    "AudioKind",
    "AudioFormat",
    "ProbeInfo",
    "LoudnessMeasurement",
    "AudioConfig",
    "SegmentSpec",
    "Peaks",
    "SampleStats",
    "PreparedSample",
    "PreparedPair",
    "RoundRecord",
    "SessionStats",
    "SessionReport",
    "build_filter",
    "guess_kind",
    "format_tag",
    "clamp",
]


class SampleKey(str, Enum):
    """The two listeners under test.  ``A`` and ``B`` are the *blind* labels."""

    A = "A"
    B = "B"

    @property
    def other(self) -> "SampleKey":
        return SampleKey.B if self is SampleKey.A else SampleKey.A


class AudioKind(str, Enum):
    """Which side of the lossless/lossy divide a file sits on."""

    LOSSLESS = "lossless"
    LOSSY = "lossy"

    @property
    def display(self) -> str:
        return "Lossless" if self is AudioKind.LOSSLESS else "Lossy"


class AudioFormat(str, Enum):
    """Container/codec families we recognise when sniffing a file."""

    FLAC = "FLAC"
    WAV = "WAV"
    AIFF = "AIFF"
    ALAC = "ALAC"
    WAVPACK = "WavPack"
    APE = "APE"
    MP3 = "MP3"
    AAC = "AAC"
    OGG = "Ogg Vorbis"
    OPUS = "Opus"
    WMA = "WMA"
    OTHER = "Audio"

    @property
    def is_lossless(self) -> bool:
        return self in {
            AudioFormat.FLAC,
            AudioFormat.WAV,
            AudioFormat.AIFF,
            AudioFormat.ALAC,
            AudioFormat.WAVPACK,
            AudioFormat.APE,
        }


#: Maps a lossy codec to the UI tag shown on the drop card.
_LOSSY_TAGS: Mapping[AudioFormat, str] = {
    AudioFormat.MP3: "MP3",
    AudioFormat.AAC: "AAC",
    AudioFormat.OGG: "Vorbis",
    AudioFormat.OPUS: "Opus",
    AudioFormat.WMA: "WMA",
}


def guess_kind(fmt: AudioFormat, hint: AudioKind | None = None) -> AudioKind:
    """Best-effort classification of an audio format."""
    if fmt.is_lossless:
        return AudioKind.LOSSLESS
    if fmt in _LOSSY_TAGS:
        return AudioKind.LOSSY
    return hint or AudioKind.LOSSY


def format_tag(fmt: AudioFormat) -> str:
    """Short badge text for a format (``"FLAC"``, ``"MP3"``, ...)."""
    return _LOSSY_TAGS.get(fmt, fmt.value)


@dataclass(frozen=True, slots=True)
class ProbeInfo:
    """Metadata of a media file, as reported by ``ffprobe``."""

    path: Path
    duration_sec: float
    sample_rate: int
    channels: int
    codec: str
    fmt: AudioFormat = AudioFormat.OTHER
    bitrate_kbps: int = 0
    size_bytes: int = 0
    lossless: bool | None = None

    @property
    def format_tag(self) -> str:
        return format_tag(self.fmt)

    @property
    def detail_line(self) -> str:
        bits = [f"{self.duration_sec:.1f} s", f"{self.sample_rate // 1000} kHz"]
        if self.channels == 1:
            bits.append("mono")
        elif self.channels == 2:
            bits.append("stereo")
        else:
            bits.append(f"{self.channels} ch")
        if self.bitrate_kbps:
            bits.append(f"{self.bitrate_kbps} kbps")
        return "  ·  ".join(bits)


def build_filter(name: str, options: Sequence[tuple[str, object]], sep: str = ":") -> str:
    """Build a filter description, tolerating both ffmpeg option syntaxes.

    ffmpeg <= 8 expects ``loudnorm:I=-16:TP=-1.5`` while ffmpeg 9 only accepts
    ``loudnorm=I=-16:TP=-1.5``.  Rather than sniffing the version string we let
    :class:`flacblind.core.ffmpeg.FFmpeg` probe which one this build accepts and
    pass the separator in as ``sep``.
    """
    if not options:
        return name
    body = ":".join(f"{key}={value}" for key, value in options)
    return f"{name}{sep}{body}"


@dataclass(frozen=True, slots=True)
class LoudnessMeasurement:
    """First pass of a two-pass EBU R128 normalisation (``loudnorm``).

    Feeding these numbers back into the second pass makes both files land on
    *exactly* the same integrated loudness, which is the whole point of the
    tool — otherwise a louder master would masquerade as "better quality".
    """

    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float = 0.0

    def as_filter(self, cfg: "AudioConfig", sep: str = ":") -> str:
        """Build the ``loudnorm`` filter string for the second pass."""
        if not cfg.normalize:
            return "anull"
        return build_filter(
            "loudnorm",
            (
                ("I", f"{cfg.target_lufs:.1f}"),
                ("TP", f"{cfg.true_peak_db:.1f}"),
                ("LRA", f"{cfg.lra:.1f}"),
                ("measured_I", f"{self.input_i:.2f}"),
                ("measured_TP", f"{self.input_tp:.2f}"),
                ("measured_LRA", f"{self.input_lra:.2f}"),
                ("measured_thresh", f"{self.input_thresh:.2f}"),
                ("offset", f"{self.target_offset:.2f}"),
                ("linear", str(cfg.linear_normalization).lower()),
                ("print_format", "summary"),
            ),
            sep,
        )

    @property
    def lufs_delta(self) -> float:
        return self.input_i


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Everything that shapes the generated comparison WAV files."""

    target_lufs: float = -16.0
    true_peak_db: float = -1.5
    lra: float = 11.0
    sample_rate: int = 44100
    channels: int = 2
    normalize: bool = True
    linear_normalization: bool = True
    #: Dither the 16 bit output down-conversion.  Off by default: dithering
    #: would add noise that is *not* present in the source, which would be
    #: actively unfair to the lossless side of the comparison.
    dither: bool = False

    def with_target(self, lufs: float) -> "AudioConfig":
        return replace(self, target_lufs=lufs)


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    """The slice of each track that takes part in the test.

    ``duration_sec is None`` means "use the whole file"; the effective length
    is then derived from the shortest of the two sources.
    """

    start_sec: float = 0.0
    duration_sec: float | None = None
    whole_file: bool = True

    @staticmethod
    def whole() -> "SegmentSpec":
        return SegmentSpec(0.0, None, True)

    @staticmethod
    def slice_of(start_sec: float, duration_sec: float) -> "SegmentSpec":
        return SegmentSpec(max(0.0, start_sec), max(0.1, duration_sec), False)

    def clamp(self, source_duration: float) -> "SegmentSpec":
        """Trim the request to what a source actually has."""
        start = min(max(0.0, self.start_sec), max(0.0, source_duration - 0.05))
        remaining = max(0.0, source_duration - start)
        if self.whole_file or self.duration_sec is None:
            return SegmentSpec(start, remaining, True)
        return SegmentSpec(start, min(self.duration_sec, remaining), False)

    @property
    def requested_duration(self) -> float | None:
        return None if self.whole_file else self.duration_sec

    def describe(self, source_duration: float) -> str:
        if self.whole_file:
            return f"Whole file · {source_duration:.0f} s"
        return f"{_fmt_time(self.start_sec)} – {_fmt_time(self.start_sec + (self.duration_sec or 0.0))}"


@dataclass(frozen=True, slots=True)
class Peaks:
    """A down-sampled min/max/RMS envelope used to draw the waveform.

    Stored as plain tuples of floats: the widget paints it every frame, and a
    dependency-free representation keeps :mod:`flacblind.core` Qt- and
    numpy-free.
    """

    mins: tuple[float, ...]
    maxs: tuple[float, ...]
    rms: tuple[float, ...]
    total_frames: int
    sample_rate: int

    def __len__(self) -> int:
        return len(self.mins)

    @property
    def bucket_frames(self) -> float:
        if not len(self):
            return 0.0
        return self.total_frames / len(self)

    @property
    def duration_sec(self) -> float:
        return self.total_frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def is_empty(self) -> bool:
        return not len(self)

    def difference(self, other: "Peaks") -> "Peaks":
        """Envelope of ``|self - other|`` (used by the analysis view)."""
        n = min(len(self), len(other))
        if n == 0:
            return Peaks((), (), (), 0, self.sample_rate)
        diffs = tuple(abs(self.mins[i] - other.mins[i]) for i in range(n))
        sums = tuple(abs(self.maxs[i] - other.maxs[i]) for i in range(n))
        return Peaks(diffs, sums, diffs, self.total_frames, self.sample_rate)


@dataclass(frozen=True, slots=True)
class SampleStats:
    """Level statistics of a prepared WAV, shown next to the waveform."""

    peak_dbfs: float
    rms_dbfs: float
    crest_factor_db: float
    #: True when more than 50 % of the segment is below -60 dBFS.
    mostly_silent: bool = False

    @property
    def summary(self) -> str:
        return f"peak {self.peak_dbfs:.1f} dBFS · RMS {self.rms_dbfs:.1f} dBFS"


@dataclass(frozen=True, slots=True)
class PreparedSample:
    """One normalised, trimmed, analysis-ready WAV."""

    key: SampleKey
    kind: AudioKind
    fmt: AudioFormat
    source_path: Path
    wav_path: Path
    duration_sec: float
    sample_rate: int
    channels: int
    frames: int
    peaks: Peaks
    stats: SampleStats
    loudness: LoudnessMeasurement | None = None
    probe: ProbeInfo | None = None
    measurement_error: str = ""

    @property
    def label(self) -> str:
        return f"{self.key.value} · {self.kind.display}"

    @property
    def bit_depth(self) -> int:
        return 16


@dataclass(frozen=True, slots=True)
class PreparedPair:
    """The two samples plus everything discovered while preparing them."""

    sample_a: PreparedSample
    sample_b: PreparedSample
    segment: SegmentSpec
    warnings: tuple[str, ...] = ()

    def __iter__(self):
        yield self.sample_a
        yield self.sample_b

    def get(self, key: SampleKey) -> PreparedSample:
        return self.sample_a if key is SampleKey.A else self.sample_b

    @property
    def duration_sec(self) -> float:
        return min(self.sample_a.duration_sec, self.sample_b.duration_sec)

    @property
    def duration_mismatch_sec(self) -> float:
        return abs(self.sample_a.duration_sec - self.sample_b.duration_sec)

    @property
    def loudness_delta_db(self) -> float:
        a, b = self.sample_a.loudness, self.sample_b.loudness
        if not a or not b:
            return 0.0
        return abs(a.input_i - b.input_i)

    def difference_peaks(self) -> Peaks | None:
        """Envelope of the sample-by-sample difference of the two WAVs."""
        if self.sample_a.frames != self.sample_b.frames:
            return None
        return self.sample_a.peaks.difference(self.sample_b.peaks)


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One completed ABX round."""

    index: int  # 1-based
    guess: SampleKey
    correct: bool
    lossless_key: SampleKey
    played_sec: float = 0.0
    switched: bool = False

    @property
    def truth(self) -> SampleKey:
        return self.lossless_key


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Binomial analysis of the votes cast so far."""

    rounds: int
    correct: int
    p_value: float
    p_value_two_sided: float
    ci_low: float
    ci_high: float
    needed_for_significance: int

    @property
    def rate(self) -> float:
        return self.correct / self.rounds if self.rounds else 0.0

    @property
    def chance_level(self) -> float:
        return self.correct / 2 if self.rounds else 0.0

    @property
    def is_significant(self) -> bool:
        return self.rounds > 0 and self.p_value < 0.05

    @property
    def is_strong(self) -> bool:
        return self.rounds > 0 and self.p_value < 0.01

    @property
    def margin(self) -> int:
        """How many more correct votes would reach p < 0.05 (0 = already there)."""
        if self.rounds <= 0:
            return 0
        if self.is_significant:
            return 0
        return max(0, self.needed_for_significance - self.correct)


@dataclass(frozen=True, slots=True)
class SessionReport:
    """Everything needed to render (or export) the final verdict."""

    records: tuple[RoundRecord, ...]
    stats: SessionStats
    endless: bool
    confidence: float = 0.95
    segment: str = ""
    sample_rate: int = 0

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def correct(self) -> int:
        return self.stats.correct

    @property
    def accuracy(self) -> str:
        return f"{self.correct}/{self.total}" if self.total else "0/0"

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 2,
            "rounds": self.total,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "p_value_one_sided": round(self.stats.p_value, 6),
            "p_value_two_sided": round(self.stats.p_value_two_sided, 6),
            "confidence_interval_95": [round(self.stats.ci_low, 4), round(self.stats.ci_high, 4)],
            "verdict": "significant" if self.stats.is_significant else "not significant",
            "endless_mode": self.endless,
            "segment": self.segment,
            "sample_rate": self.sample_rate,
            "history": [
                {
                    "round": r.index,
                    "guess": r.guess.value,
                    "truth": r.lossless_key.value,
                    "correct": r.correct,
                    "switched": r.switched,
                }
                for r in self.records
            ],
        }


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(round(seconds)), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def clamp(value: float, low: float, high: float) -> float:
    """``min(max(value, low), high)`` that also copes with NaN."""
    if math.isnan(value):
        return low
    return max(low, min(high, value))


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def linear_to_db(value: float, floor: float = -120.0) -> float:
    return 20.0 * math.log10(max(1e-12, value)) if value > 0 else floor
