"""Thin, testable wrapper around the ``ffmpeg`` / ``ffprobe`` binaries.

Design notes
------------
* **Command building is pure.**  :func:`build_measure_cmd`,
  :func:`build_render_cmd` and :func:`build_probe_cmd` only return argument
  lists, which makes the (easy to get subtly wrong) filter graphs unit
  testable without spawning a process.
* **Running is cancellable and reports progress.**  ``ffmpeg -progress`` is
  parsed for ``out_time_us=`` so the UI can show a real progress bar; the
  caller thread owns stdout (progress) and a helper thread owns stderr (log).
* **stderr is parsed, never swallowed.**  When a conversion fails the tail of
  ffmpeg's log is attached to the exception, which is what makes "this file is
  DRM protected" comprehensible to a human.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .errors import DependencyError, MediaError, PreparationCancelled, UnsupportedAudioError
from .models import (
    AudioConfig,
    AudioFormat,
    LoudnessMeasurement,
    ProbeInfo,
    SegmentSpec,
    build_filter,
)

__all__ = [
    "FFmpeg",
    "FFmpegLocation",
    "ProcessResult",
    "build_probe_cmd",
    "build_measure_cmd",
    "build_render_cmd",
    "parse_loudnorm_json",
    "parse_duration",
    "parse_bitrate",
    "parse_progress",
    "format_from_codec",
    "tail",
    "INSTALL_HINT",
]

ProgressCallback = Callable[[float], None]
LogCallback = Callable[[str], None]

#: ffmpeg path -> accepted filter-option separator (see ``FFmpeg.filter_sep``).
_FILTER_SEP_CACHE: dict[str, str] = {}

INSTALL_HINT = (
    "ffmpeg was not found on your PATH.\n"
    "Install it with:\n"
    "  • Debian/Ubuntu:  sudo apt install ffmpeg\n"
    "  • Arch Linux:     sudo pacman -S ffmpeg\n"
    "  • macOS:          brew install ffmpeg\n"
    "  • Windows:        winget install Gyan.FFmpeg  (or choco install ffmpeg)"
)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d{1,6})")
_STREAM_RE = re.compile(r"Stream\s+#\d+:\d+.*?:\s*Audio:\s*(?P<codec>[A-Za-z0-9_\-]+)")
_RATE_RE = re.compile(r"(\d+)\s*Hz")
_LAYOUT_RE = re.compile(r"Hz,\s*([a-z0-9.]+)")
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)

#: Codec name from ``ffprobe`` -> our format enum.
_CODEC_MAP: dict[str, AudioFormat] = {
    "flac": AudioFormat.FLAC,
    "pcm_s16le": AudioFormat.WAV,
    "pcm_s24le": AudioFormat.WAV,
    "pcm_s32le": AudioFormat.WAV,
    "pcm_f32le": AudioFormat.WAV,
    "pcm_u8": AudioFormat.WAV,
    "pcm_mulaw": AudioFormat.WAV,
    "pcm_alaw": AudioFormat.WAV,
    "aiff": AudioFormat.AIFF,
    "alac": AudioFormat.ALAC,
    "wavpack": AudioFormat.WAVPACK,
    "ape": AudioFormat.APE,
    "mp3": AudioFormat.MP3,
    "mp3float": AudioFormat.MP3,
    "aac": AudioFormat.AAC,
    "libfdk_aac": AudioFormat.AAC,
    "vorbis": AudioFormat.OGG,
    "opus": AudioFormat.OPUS,
    "wmav2": AudioFormat.WMA,
    "wmav1": AudioFormat.WMA,
}

#: Extension fallbacks when the codec string is unhelpful.
_EXT_MAP: dict[str, AudioFormat] = {
    ".flac": AudioFormat.FLAC,
    ".wav": AudioFormat.WAV,
    ".wave": AudioFormat.WAV,
    ".aif": AudioFormat.AIFF,
    ".aiff": AudioFormat.AIFF,
    ".m4a": AudioFormat.ALAC,
    ".mp4": AudioFormat.AAC,
    ".mp3": AudioFormat.MP3,
    ".ogg": AudioFormat.OGG,
    ".oga": AudioFormat.OGG,
    ".opus": AudioFormat.OPUS,
    ".wv": AudioFormat.WAVPACK,
    ".ape": AudioFormat.APE,
    ".wma": AudioFormat.WMA,
}


@dataclass(frozen=True, slots=True)
class FFmpegLocation:
    """Resolved paths of the external tools."""

    ffmpeg: str
    ffprobe: str

    @property
    def has_ffprobe(self) -> bool:
        return bool(self.ffprobe)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of a finished ffmpeg invocation."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def format_from_codec(codec: str, path: Path | None = None) -> AudioFormat:
    """Map an ffmpeg codec name (or a filename) onto an :class:`AudioFormat`."""
    key = (codec or "").strip().lower()
    if key in _CODEC_MAP:
        return _CODEC_MAP[key]
    if path is not None:
        return _EXT_MAP.get(path.suffix.lower(), AudioFormat.OTHER)
    return AudioFormat.OTHER


def parse_duration(text: str) -> float:
    """Extract the ``Duration:`` value from ffmpeg's banner output."""
    m = _DURATION_RE.search(text)
    if not m:
        return 0.0
    hours, minutes, seconds, frac = m.groups()
    frac = (frac + "000000")[:6]
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(frac) / 1_000_000


def parse_bitrate(text: str) -> int:
    m = _BITRATE_RE.search(text)
    return int(m.group(1)) if m else 0


def parse_loudnorm_json(text: str) -> dict[str, float]:
    """Pull the measurement JSON block out of ``loudnorm=print_format=json``."""
    match = _LOUDNORM_JSON_RE.search(text)
    if not match:
        raise MediaError(
            "Could not read the loudness measurement from ffmpeg.",
            hint="The file may be too short, silent, or use an unsupported channel layout.",
        )
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise MediaError(f"ffmpeg returned malformed loudness data: {exc}") from exc
    needed = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    missing = [key for key in needed if key not in raw]
    if missing:
        raise MediaError(
            "ffmpeg did not report a complete loudness measurement "
            f"(missing: {', '.join(missing)})."
        )
    return {key: float(raw[key]) for key in needed}


def parse_progress(line: str) -> float | None:
    """Convert one ``-progress`` line into a position in seconds."""
    line = line.strip()
    if line.startswith(("out_time_us=", "out_time_ms=")):
        # Note: ffmpeg's `out_time_ms` is really microseconds too.
        try:
            return int(line.split("=", 1)[1]) / 1_000_000.0
        except ValueError:
            return None
    if line.startswith("out_time="):
        raw = line.split("=", 1)[1].strip()
        if raw == "N/A":
            return None
        try:
            numbers = [float(part) for part in raw.split(":")]
        except ValueError:
            return None
        total = 0.0
        for value in numbers:
            total = total * 60 + value
        return total
    return None


def build_probe_cmd(ffprobe: str, path: Path) -> list[str]:
    """``ffprobe`` invocation that emits a single JSON object."""
    return [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-select_streams", "a:0",
        str(path),
    ]


def build_measure_cmd(
    ffmpeg: str,
    path: Path,
    cfg: AudioConfig,
    sep: str = ":",
) -> list[str]:
    """First loudnorm pass: measure integrated loudness, throw the audio away."""
    filt = build_filter(
        "loudnorm",
        (
            ("I", f"{cfg.target_lufs:.1f}"),
            ("TP", f"{cfg.true_peak_db:.1f}"),
            ("LRA", f"{cfg.lra:.1f}"),
            ("print_format", "json"),
        ),
        sep,
    )
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-y",
        "-i", str(path),
        "-map", "a:0",
        "-af", filt,
        "-f", "null", "-",
    ]


def build_render_cmd(
    ffmpeg: str,
    src: Path,
    dst: Path,
    cfg: AudioConfig,
    segment: SegmentSpec,
    measurement: LoudnessMeasurement | None = None,
    sep: str = ":",
) -> list[str]:
    """Second pass: loudness-correct *and* trim, straight to 16 bit PCM.

    ``-ss``/``-t`` go **before** ``-i`` so ffmpeg can seek by keyframe instead
    of decoding everything; the very same arguments are used for both
    sources, so the two segments stay frame-aligned to within a few
    milliseconds.
    """
    args: list[str] = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-y"]
    duration = segment.requested_duration
    if duration is not None:
        if segment.start_sec > 0:
            args += ["-ss", f"{segment.start_sec:.3f}"]
        args += ["-t", f"{duration:.3f}"]
    args += ["-i", str(src)]

    filters: list[str] = []
    if cfg.normalize and measurement is not None:
        filters.append(measurement.as_filter(cfg, sep))
    else:
        filters.append("anull")
    if cfg.channels != 2:
        filters.append(build_filter("aformat", (("channel_layouts", "mono" if cfg.channels == 1 else "stereo"),), sep))
    if cfg.dither:
        filters.append(
            build_filter("aresample", (("dither_method", "triangular_hp"), ("osr", cfg.sample_rate)), sep)
        )

    args += ["-af", ",".join(filters)]
    args += [
        "-ar", str(cfg.sample_rate),
        "-ac", str(cfg.channels),
        "-c:a", "pcm_s16le",
        "-f", "wav",
        str(dst),
    ]
    return args


def tail(text: str, lines: int = 6) -> str:
    """Last meaningful lines of a process log."""
    kept = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


class FFmpeg:
    """Facade for the external tools.

    ``FFmpeg`` is stateless and safe to use from worker threads; pass a
    :class:`threading.Event` as ``cancel`` to abort a long conversion.
    """

    def __init__(
        self,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        *,
        on_log: LogCallback | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self.ffprobe = (ffprobe if ffprobe is not None else shutil.which("ffprobe")) or ""
        self._on_log = on_log
        self._last_stderr = ""

    # ------------------------------------------------------------------ setup
    @property
    def available(self) -> bool:
        return bool(self.ffmpeg) and os.path.isfile(self.ffmpeg) and os.access(self.ffmpeg, os.X_OK)

    @property
    def has_ffprobe(self) -> bool:
        return bool(self.ffprobe) and os.path.isfile(self.ffprobe)

    def ensure_available(self) -> None:
        if not self.available:
            raise DependencyError("ffmpeg is not available.", hint=INSTALL_HINT)

    @property
    def location(self) -> FFmpegLocation:
        return FFmpegLocation(self.ffmpeg, self.ffprobe)

    @property
    def version(self) -> str:
        if not self.available:
            return "not found"
        try:
            out = subprocess.run(  # noqa: S603
                [self.ffmpeg, "-version"], capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
            return "unknown"
        return out.splitlines()[0].strip() if out else "unknown"

    def describe(self) -> str:
        probe = "✓" if self.has_ffprobe else "✗"
        return f"ffmpeg {'✓' if self.available else '✗'} · ffprobe {probe}"

    @property
    def filter_sep(self) -> str:
        """Which filter-option separator this ffmpeg build understands.

        ffmpeg 9 dropped the classic ``loudnorm:I=-16`` spelling in favour of
        ``loudnorm=I=-16``; rather than parse the version number we run a 50 ms
        probe render once per binary and remember what worked, so the tool
        works on ffmpeg 4 through 9.  Cached class-wide: the answer cannot
        change while the binary stays installed.
        """
        if not self.available:
            return ":"
        cached = _FILTER_SEP_CACHE.get(self.ffmpeg)
        if cached is not None:
            return cached
        sep = ":"
        for candidate in (":", "="):
            if self._probe_filter_sep(candidate):
                sep = candidate
                break
        _FILTER_SEP_CACHE[self.ffmpeg] = sep
        return sep

    def _probe_filter_sep(self, sep: str) -> bool:
        """Can this build parse a filter written with ``sep``?"""
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "quiet",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
            "-t", "0.05",
            "-af", build_filter("volume", (("volume", "-70.0"),), sep),
            "-f", "null", "-",
        ]
        try:
            return subprocess.run(  # noqa: S603 - fixed argument list
                cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=20
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
            return sep == ":"

    # ---------------------------------------------------------------- probing
    def probe(self, path: str | os.PathLike[str]) -> ProbeInfo:
        """Read duration / rate / channels / codec of an audio file."""
        path = Path(path)
        if not path.exists():
            raise MediaError(f"File not found: {path}")
        if path.is_dir():
            raise MediaError(f"Not a file: {path}")
        if not os.access(path, os.R_OK):
            raise MediaError(
                f"No permission to read {path.name}.",
                hint="Check the file permissions, or move the file somewhere readable.",
            )
        if path.stat().st_size == 0:
            raise UnsupportedAudioError(f"{path.name} is empty (0 bytes).")

        if self.has_ffprobe:
            try:
                return self._probe_with_ffprobe(path)
            except MediaError:
                raise
            except Exception:  # noqa: BLE001 - fall through to the ffmpeg parser
                pass
        return self._probe_with_ffmpeg(path)

    def _probe_with_ffprobe(self, path: Path) -> ProbeInfo:
        result = self._run([*build_probe_cmd(self.ffprobe, path)], timeout=90)
        if not result.ok:
            raise self._error_from(result, path)
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MediaError(f"ffprobe returned invalid JSON for {path.name}: {exc}") from exc

        streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise UnsupportedAudioError(
                f"{path.name} contains no audio stream.",
                hint="Video-only files cannot be used in a listening test.",
            )
        stream = streams[0]
        fmt_info = data.get("format", {})
        duration = _as_float(stream.get("duration")) or _as_float(fmt_info.get("duration"))
        if duration <= 0:
            duration = self._duration_via_ffmpeg(path)
        codec = str(stream.get("codec_name", "")).lower()
        channels = int(stream.get("channels") or 0)
        sample_rate = int(stream.get("sample_rate") or 0)
        size = int(_as_float(fmt_info.get("size")) or path.stat().st_size)
        bitrate = int(_as_float(fmt_info.get("bit_rate")) or 0) // 1000
        if not bitrate and duration > 0:
            bitrate = int(size * 8 / (duration * 1000))
        fmt = format_from_codec(codec, path)
        return ProbeInfo(
            path=path,
            duration_sec=round(duration, 6),
            sample_rate=sample_rate,
            channels=channels,
            codec=codec or "unknown",
            fmt=fmt,
            bitrate_kbps=bitrate,
            size_bytes=size,
            lossless=fmt.is_lossless,
        )

    def _probe_with_ffmpeg(self, path: Path) -> ProbeInfo:
        result = self._run([self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], timeout=90)
        text = result.stderr or result.stdout
        if "Invalid data found" in text or "does not contain any stream" in text:
            raise UnsupportedAudioError(
                f"{path.name} is not a decodable audio file.",
                hint="Supported inputs: FLAC, WAV, AIFF, MP3, M4A/AAC, Ogg, Opus, WavPack.",
            )
        duration = parse_duration(text)
        if duration <= 0:
            raise UnsupportedAudioError(
                f"Could not determine the duration of {path.name}.",
                hint="The file may be corrupt, empty, or still being written.",
            )
        stream = _STREAM_RE.search(text)
        codec = stream.group("codec") if stream else ""
        after = text[stream.end():] if stream else ""
        rate_m = _RATE_RE.search(after)
        layout_m = _LAYOUT_RE.search(after)
        layout = layout_m.group(1) if layout_m else ""
        channels = {"mono": 1, "stereo": 2}.get(layout, 0)
        fmt = format_from_codec(codec, path)
        return ProbeInfo(
            path=path,
            duration_sec=duration,
            sample_rate=int(rate_m.group(1)) if rate_m else 0,
            channels=channels,
            codec=codec or "unknown",
            fmt=fmt,
            bitrate_kbps=parse_bitrate(text),
            size_bytes=path.stat().st_size,
            lossless=fmt.is_lossless,
        )

    def _duration_via_ffmpeg(self, path: Path) -> float:
        result = self._run([self.ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], timeout=90)
        return parse_duration(result.stderr or result.stdout)

    # --------------------------------------------------------------- loudness
    def measure_loudness(
        self,
        path: str | os.PathLike[str],
        cfg: AudioConfig,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> LoudnessMeasurement:
        """Run loudnorm's measuring pass and return its values."""
        self.ensure_available()
        result = self._run(
            build_measure_cmd(self.ffmpeg, Path(path), cfg, self.filter_sep),
            progress=progress,
            cancel=cancel,
        )
        if not result.ok:
            raise self._error_from(result, Path(path))
        values = parse_loudnorm_json(result.stderr)
        return LoudnessMeasurement(
            input_i=values["input_i"],
            input_tp=values["input_tp"],
            input_lra=values["input_lra"],
            input_thresh=values["input_thresh"],
            target_offset=values["target_offset"],
        )

    # ----------------------------------------------------------------- render
    def render(
        self,
        src: str | os.PathLike[str],
        dst: str | os.PathLike[str],
        cfg: AudioConfig,
        segment: SegmentSpec,
        measurement: LoudnessMeasurement | None = None,
        *,
        total_duration: float = 0.0,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """Normalise + trim + resample ``src`` into the 16 bit WAV at ``dst``."""
        self.ensure_available()
        args = build_render_cmd(
            self.ffmpeg, Path(src), Path(dst), cfg, segment, measurement, self.filter_sep
        )
        dst_path = Path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            args,
            total_duration=total_duration or None,
            progress=progress,
            cancel=cancel,
        )
        if not dst_path.exists() or dst_path.stat().st_size <= 44:
            detail = tail(self._last_stderr, 4)
            raise MediaError(
                f"ffmpeg produced no audio for {Path(src).name}.",
                hint=detail or "Try a different segment, or disable loudness normalisation.",
            )

    # ----------------------------------------------------------------- runner
    def _run(
        self,
        args: Sequence[str],
        *,
        total_duration: float | None = None,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        timeout: float | None = None,
    ) -> ProcessResult:
        """Spawn a child process, streaming progress on the caller's thread."""
        if not args:
            raise ValueError("empty command")
        if cancel is not None and cancel.is_set():
            raise PreparationCancelled()

        cmd = [str(a) for a in args]
        wants_progress = cmd[0] == self.ffmpeg and "-progress" not in cmd
        if wants_progress:
            # Both tokens go together: a trailing `pipe:1` would be parsed as a
            # second output file and ffmpeg would refuse to start.
            cmd[1:1] = ["-progress", "pipe:1"]

        try:
            proc = subprocess.Popen(  # noqa: S603 - arguments are built internally
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise DependencyError("ffmpeg is not available.", hint=INSTALL_HINT) from exc
        except PermissionError as exc:
            raise DependencyError(
                f"{cmd[0]} is not executable.",
                hint="Check the permissions of the ffmpeg binary.",
            ) from exc
        except OSError as exc:
            raise MediaError(f"Could not start ffmpeg: {exc}") from exc

        stderr_chunks: list[str] = []
        self._last_stderr = ""

        def _drain(stream: Iterable[str]) -> None:
            try:
                for line in stream:
                    stderr_chunks.append(line)
                    if len(stderr_chunks) > 400:
                        del stderr_chunks[:200]
                    if self._on_log is not None:
                        self._on_log(line.rstrip())
            except (ValueError, OSError):  # pragma: no cover - stream closed early
                pass

        err_thread = threading.Thread(target=_drain, args=(proc.stderr,), daemon=True)
        err_thread.start()

        stdout_chunks: list[str] = []
        last_fraction = -1.0
        cancelled = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                seconds = parse_progress(line)
                if seconds is None:
                    if line.strip():
                        stdout_chunks.append(line)
                    continue
                if cancel is not None and cancel.is_set():
                    self._terminate(proc)
                    cancelled = True
                    break
                if progress is None or not total_duration:
                    continue
                fraction = min(1.0, max(0.0, seconds / total_duration))
                if fraction - last_fraction >= 0.004 or fraction >= 1.0:
                    last_fraction = fraction
                    progress(fraction)
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except Exception:  # noqa: BLE001  # pragma: no cover
                    pass
        if cancelled:
            raise PreparationCancelled()

        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
            self._terminate(proc)
            raise MediaError(
                f"ffmpeg timed out after {timeout:.0f} s.",
                hint="The file may be extremely long, or on a slow network share.",
            ) from exc
        err_thread.join(timeout=3)
        stderr_text = "".join(stderr_chunks)
        self._last_stderr = stderr_text
        return ProcessResult(tuple(cmd), returncode, "".join(stdout_chunks), stderr_text)

    @staticmethod
    def _terminate(proc: "subprocess.Popen[str]") -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:  # noqa: BLE001  # pragma: no cover - best effort
            pass

    def _error_from(self, result: ProcessResult, path: Path) -> MediaError:
        """Translate ffmpeg's exit code + log into a typed, explained error."""
        text = result.stderr or ""
        if "Permission denied" in text:
            return MediaError(
                f"Permission denied while reading {path.name}.",
                hint="The file may still be downloading, locked, or owned by another user.",
            )
        if "Invalid data found" in text or "moov atom not found" in text:
            return UnsupportedAudioError(
                f"{path.name} is corrupt or is not an audio file.",
                hint="The container could not be parsed — try re-downloading it.",
            )
        if "Invalid argument" in text and "-ss" in text:
            return MediaError(
                f"Could not seek to the requested segment in {path.name}.",
                hint="Move the segment start earlier, or switch back to the whole file.",
            )
        return MediaError(
            f"ffmpeg failed on {path.name} (exit code {result.returncode}).",
            hint=tail(text, 5) or "Run the command manually to see the full log.",
        )


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
