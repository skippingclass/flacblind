"""Shared test helpers."""

from __future__ import annotations

import shutil
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for entry in (str(HERE), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

FFMPEG = shutil.which("ffmpeg")
HAS_FFMPEG = FFMPEG is not None


def require_ffmpeg(test):  # noqa: ANN001, ANN201 - unittest decorator
    """Skip a test when ffmpeg is not installed."""
    return unittest_skip_if(not HAS_FFMPEG, "ffmpeg is not installed")(test)


unittest_skip_if = None  # set below to avoid an import at module import time

import unittest  # noqa: E402

unittest_skip_if = unittest.skipIf


def write_wav(path: Path, samples: list[int], *, rate: int = 44100, channels: int = 2) -> Path:
    """Write a 16 bit PCM WAV from a list of interleaved samples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return path


def make_tone_wav(
    path: Path,
    seconds: float = 1.0,
    rate: int = 44100,
    amplitude: int = 12000,
    decay: bool = True,
) -> Path:
    """A stereo tone file — deterministic, no ffmpeg needed.

    The two channels deliberately differ (220 Hz + 330 Hz with a slow tremolo)
    so that the mono mixdown used for the waveform is not silent, and the
    amplitude decays so the envelope has visible dynamics.
    """
    import math

    frames = int(seconds * rate)
    samples: list[int] = []
    for i in range(frames):
        t = i / rate
        left = math.sin(2 * math.pi * 220 * t)
        right = 0.8 * math.sin(2 * math.pi * 330 * t)
        tremolo = 0.5 * (1.0 + math.sin(2 * math.pi * 1.5 * t)) if decay else 1.0
        envelope = max(0.0, 1.0 - 0.4 * t / max(seconds, 1e-6))
        gain = amplitude * tremolo * envelope
        samples.extend((int(gain * left), int(gain * right)))
    return write_wav(path, samples, rate=rate, channels=2)


def make_mp3(wav: Path, mp3: Path, bitrate: str = "96k") -> Path:
    """Transcode a WAV to MP3 (requires ffmpeg)."""
    subprocess.run(  # noqa: S603
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
         "-c:a", "libmp3lame", "-b:a", bitrate, str(mp3)],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return mp3


def write_quiet_wav(path: Path, seconds: float = 0.5, rate: int = 44100) -> Path:
    """A real, tiny, valid WAV file (stereo sine, no ffmpeg needed)."""
    import math

    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    payload = bytearray()
    for i in range(frames):
        value = int(8000 * math.sin(2 * math.pi * 220 * i / rate))
        payload += value.to_bytes(2, "little", signed=True)
        payload += (-value).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(payload))
    return path


def make_pair(directory: Path) -> tuple[Path, Path]:
    """Create a lossless/lossy pair in ``directory``."""
    master = make_tone_wav(directory / "master.wav", seconds=6.0)
    lossy = make_mp3(master, directory / "lossy.mp3")
    lossless = shutil.copy(master, directory / "lossless.wav")
    return lossless, lossy
