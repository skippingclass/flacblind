"""Persistent user settings (JSON, one flat file, no Qt involved)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .models import AudioConfig, SegmentSpec

__all__ = ["AppSettings", "PLAYBACK_BACKENDS", "ROUND_CHOICES"]

PLAYBACK_BACKENDS: tuple[tuple[str, str], ...] = (
    ("auto", "Automatic (best available)"),
    ("sounddevice", "Sample-accurate (sounddevice)"),
    ("qt", "Qt Multimedia"),
    ("cli", "External player (paplay/ffplay/...)"),
)

ROUND_CHOICES: tuple[int, ...] = (5, 10, 20, 0)  # 0 == endless


def _config_dir() -> Path:
    """XDG-ish config directory, so the app behaves on Linux, macOS and Windows."""
    if os.name == "nt":  # pragma: no cover - Windows
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "flacblind"
    if sys.platform == "darwin":  # pragma: no cover - macOS only
        return Path.home() / "Library" / "Application Support" / "flacblind"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "flacblind"


@dataclass
class AppSettings:
    """Everything the user can tweak, persisted between runs."""

    # sources
    lossless_path: str = ""
    lossy_path: str = ""
    recent: list[str] = field(default_factory=list)

    # processing
    target_lufs: float = -16.0
    true_peak_db: float = -1.5
    lra: float = 11.0
    sample_rate: int = 44100
    normalize: bool = True
    segment_start: float = 0.0
    segment_duration: float = 30.0
    whole_file: bool = True

    # session
    rounds: int = 10
    reveal_after_vote: bool = True
    require_both_heard: bool = False
    min_plays: int = 0
    seed: int | None = None

    # playback
    backend: str = "auto"
    loop: bool = True
    volume: float = 0.8
    crossfade_ms: int = 0
    auto_advance: bool = True

    # housekeeping
    delete_temp_on_exit: bool = True
    language: str = "en"
    show_difference_view: bool = False

    # window state (kept as plain values so the model stays Qt-free)
    geometry: str = ""
    state: str = ""

    # ----------------------------------------------------------------- paths
    @staticmethod
    def default_path() -> Path:
        return _config_dir() / "settings.json"

    # ----------------------------------------------------------- conversions
    def audio_config(self) -> AudioConfig:
        return AudioConfig(
            target_lufs=self.target_lufs,
            true_peak_db=self.true_peak_db,
            lra=self.lra,
            sample_rate=self.sample_rate,
            normalize=self.normalize,
        )

    def segment(self) -> SegmentSpec:
        if self.whole_file:
            return SegmentSpec.whole()
        return SegmentSpec.slice_of(self.segment_start, self.segment_duration)

    def apply_audio_config(self, cfg: AudioConfig) -> None:
        self.target_lufs = cfg.target_lufs
        self.true_peak_db = cfg.true_peak_db
        self.lra = cfg.lra
        self.sample_rate = cfg.sample_rate
        self.normalize = cfg.normalize

    def apply_segment(self, segment: SegmentSpec) -> None:
        self.whole_file = segment.whole_file
        self.segment_start = segment.start_sec
        if segment.duration_sec is not None:
            self.segment_duration = segment.duration_sec

    def apply_test_config(self, config: Any) -> None:
        """Copy the fields of an :class:`flacblind.core.abx.TestConfig`."""
        self.rounds = config.rounds
        self.reveal_after_vote = config.reveal_after_vote
        self.require_both_heard = config.require_both_heard
        self.min_plays = config.min_plays
        self.seed = config.seed

    def remember(self, path: str, limit: int = 8) -> None:
        if not path:
            return
        if path in self.recent:
            self.recent.remove(path)
        self.recent.insert(0, path)
        del self.recent[limit:]

    # ------------------------------------------------------------------- I/O
    def load(self, path: str | os.PathLike[str] | None = None) -> "AppSettings":
        target = Path(path) if path is not None else self.default_path()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self
        if not isinstance(raw, dict):
            return self
        known = {f.name for f in fields(self)}
        for key, value in raw.items():
            # Lenient by design: a hand-edited or older settings file must
            # never stop the app from starting.
            if key in known:
                try:
                    setattr(self, key, value)
                except (AttributeError, TypeError):  # pragma: no cover - defensive
                    continue
        # Sanity clamp the numeric knobs.
        self.target_lufs = _clamp(self.target_lufs, -40.0, -5.0)
        self.true_peak_db = _clamp(self.true_peak_db, -9.0, 0.0)
        self.lra = _clamp(self.lra, 1.0, 20.0)
        self.sample_rate = int(self.sample_rate) if self.sample_rate in (22050, 44100, 48000, 96000) else 44100
        self.rounds = int(self.rounds) if self.rounds in ROUND_CHOICES else 10
        self.volume = _clamp(float(self.volume), 0.0, 1.0)
        self.segment_start = max(0.0, float(self.segment_start))
        self.segment_duration = _clamp(float(self.segment_duration), 1.0, 3600.0)
        if not isinstance(self.recent, list):
            self.recent = []
        self.recent = [str(item) for item in self.recent][:8]
        if self.backend not in {key for key, _ in PLAYBACK_BACKENDS}:
            self.backend = "auto"
        if self.language not in ("en", "ru"):
            self.language = "en"
        return self

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        target = Path(path) if path is not None else self.default_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = asdict(self)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(target)
        except OSError:  # pragma: no cover - read-only home directory
            pass
        return target


def _clamp(value: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low
