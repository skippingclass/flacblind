"""Playback: a backend chain with sample-accurate, position-locked A/B switching.

Three backends, tried in this order (or forced from the settings):

``sounddevice``
    Decodes both prepared WAVs into memory and swaps a pointer in the audio
    callback — a true zero-cost switch, and the only backend whose position
    report is exact.  Optional dependency.
``qt``
    :class:`QMediaPlayer` + :class:`QAudioOutput`.  Zero extra dependencies and
    available on every platform, at the price of a device-latency-sized gap
    (typically 20–40 ms) when switching.
``cli``
    ``ffplay`` / ``paplay`` / ``aplay`` / ``afplay``.  Last resort: no seeking,
    so the position readout is disabled.

All three share the same interface and the same *shared cursor*: whichever
sample is playing, switching the other one in resumes at the same millisecond,
which is what makes an A/B comparison meaningful.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from abc import ABC, abstractmethod
from time import monotonic
from pathlib import Path
from typing import Callable, Sequence

from ..core.errors import PlaybackError
from ..core.models import PreparedSample, SampleKey
from ..core.waveform import read_wav_float32
from .qtcompat import QObject, QtCore, QtMultimedia, Signal, media_available

__all__ = [
    "Player",
    "BackendInfo",
    "available_backends",
    "BACKEND_LABELS",
    "NO_AUDIO_ERROR",
    "STARTUP_GRACE",
]

#: Fallback English labels; the UI prefers the translated ones (see i18n).
BACKEND_LABELS: dict[str, str] = {
    "sounddevice": "Sample-accurate (sounddevice)",
    "qt": "Qt Multimedia",
    "cli": "External player",
    "none": "Unavailable",
}

#: How often the facade polls the backend for the playhead position.
POLL_MS = 40
#: How long a backend may take to actually start before we complain.  Opening
#: an audio device and filling the first buffers is not instantaneous.
STARTUP_GRACE = 2.5
#: When the playhead is within this distance of the end, treat it as a wrap.
LOOP_TOLERANCE_MS = 120
NO_AUDIO_ERROR = (
    "Playback did not start.\n"
    "The audio output device may be busy, muted or unavailable — check the "
    "system volume mixer, or pick another playback backend in the Playback selector."
)


class BackendInfo:
    """Discovery record for one backend."""

    __slots__ = ("key", "label", "available", "detail")

    def __init__(self, key: str, label: str, available: bool, detail: str = "") -> None:
        self.key = key
        self.label = label
        self.available = available
        self.detail = detail


def _sounddevice_available() -> tuple[bool, str]:
    try:
        import sounddevice  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - PortAudio may be missing
        return False, str(exc)
    return True, "sample-accurate"


def _cli_player() -> tuple[bool, str]:
    for name, args in _CLI_PLAYERS:
        path = shutil.which(name)
        if path:
            return True, f"{name} ({path})"
    return False, "no external player found"


#: External players, best first.  Each entry is (binary, argv template).
_CLI_PLAYERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet", "-vn", "{path}")),
    ("paplay", ("{path}",)),
    ("aplay", ("-q", "{path}")),
    ("afplay", ("{path}",)),
    ("mpv", ("--no-video", "--really-quiet", "{path}")),
    ("cvlc", ("--play-and-exit", "--intf", "dummy", "{path}")),
)


# --------------------------------------------------------------------------- #
#  Backends
# --------------------------------------------------------------------------- #
class PlaybackBackend(ABC):
    """Interface every backend implements."""

    key: str = "none"
    label: str = "Unavailable"
    supports_seek: bool = True
    supports_loop: bool = True

    def __init__(self) -> None:
        self.last_error: str = ""
        self.on_event: Callable[[str], None] | None = None

    # -- lifecycle -------------------------------------------------------
    @abstractmethod
    def load(self, sample: PreparedSample) -> None:
        """Prepare a sample for playback (decode if necessary)."""

    @abstractmethod
    def unload(self) -> None:
        """Release resources."""

    # -- transport -------------------------------------------------------
    @abstractmethod
    def play(self, key: SampleKey, position_ms: int) -> None:
        """Start (or restart) ``key`` at ``position_ms``."""

    @abstractmethod
    def pause(self) -> None:
        """Pause, keeping the position."""

    @abstractmethod
    def stop(self) -> None:
        """Stop and rewind to the start."""

    @abstractmethod
    def seek(self, position_ms: int) -> None:
        """Move the playhead."""

    @property
    @abstractmethod
    def position_ms(self) -> int:
        """Current playhead position in milliseconds."""

    @property
    @abstractmethod
    def is_playing(self) -> bool:
        """Is audio currently being produced?"""

    def playback_state(self) -> str:
        """``"playing"``, ``"buffering"`` or ``"stopped"``.

        The distinction matters: a freshly started :class:`QMediaPlayer` needs
        a moment to open the device and fill its buffers, and treating that
        window as "the track ended" is what silently kills playback.
        """
        return "playing" if self.is_playing else "stopped"

    @property
    @abstractmethod
    def duration_ms(self) -> int:
        """Length of the loaded sample in milliseconds."""

    # -- parameters ------------------------------------------------------
    def set_loop(self, enabled: bool) -> None:
        """Loop the sample when it reaches the end."""

    def set_volume(self, volume: float) -> None:
        """Set output volume (0..1)."""

    def set_muted(self, muted: bool) -> None:
        """Mute without losing the volume setting."""

    def set_rate(self, rate: float) -> None:
        """Set playback speed multiplier."""

    def _fail(self, message: str) -> None:
        self.last_error = message
        if self.on_event is not None:
            self.on_event(message)


class QtMediaBackend(PlaybackBackend):
    """Two pre-loaded :class:`QMediaPlayer`s sharing one cursor."""

    key = "qt"
    label = BACKEND_LABELS["qt"]

    def __init__(self) -> None:
        super().__init__()
        if not media_available():
            raise PlaybackError(
                "Qt Multimedia is not available in this PyQt6/PySide6 build.",
                hint="Install the full Qt package (pip install PyQt6-Desktop / PySide6-Essentials).",
            )
        self._players: dict[SampleKey, "QtMultimedia.QMediaPlayer"] = {}
        self._outputs: dict[SampleKey, "QtMultimedia.QAudioOutput"] = {}
        self._durations: dict[SampleKey, int] = {}
        self._active: SampleKey | None = None
        self._loop = True
        self._volume = 0.8

    def load(self, sample: PreparedSample) -> None:
        try:
            player = self._players.get(sample.key)
            if player is None:
                output = QtMultimedia.QAudioOutput()
                player = QtMultimedia.QMediaPlayer()
                player.setAudioOutput(output)
                # NB: QMediaPlayer.setNotificationMode() no longer exists in
                # Qt 6; calling it raised an AttributeError that silently left
                # this backend empty — i.e. no sound at all.
                player.errorOccurred.connect(self._on_error)
                self._players[sample.key] = player
                self._outputs[sample.key] = output
            player.setSource(_url(sample.wav_path))
            # Volume lives on QAudioOutput in Qt 6; QMediaPlayer has no
            # setVolume() (calling it raised an AttributeError that crashed the
            # app the first time a sample was played).
            self._outputs[sample.key].setVolume(self._volume)
            self._durations[sample.key] = int(sample.duration_sec * 1000)
            player.durationChanged.connect(lambda d, k=sample.key: self._on_duration(k, d))
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill playback
            self._fail(f"{self.label}: could not load {sample.wav_path.name} ({exc})")

    def _on_duration(self, key: SampleKey, duration: int) -> None:
        if duration > 0:
            self._durations[key] = duration

    def _on_error(self, *_args: object) -> None:
        player = self._players.get(self._active) if self._active else None
        text = player.errorString() if player is not None else ""
        if text:
            self._fail(text)

    def unload(self) -> None:
        for player in self._players.values():
            try:
                player.stop()
                player.setSource(QtCore.QUrl())
            except Exception:  # noqa: BLE001  # pragma: no cover - best effort
                pass
        self._players.clear()
        self._outputs.clear()
        self._durations.clear()
        self._active = None

    def play(self, key: SampleKey, position_ms: int) -> None:
        player = self._players.get(key)
        if player is None:
            self._fail("Sample is not loaded.")
            return
        if self._active is not None and self._active is not key:
            other = self._players[self._active]
            if other.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState:
                other.pause()
        target = self._clamp_position(key, position_ms)
        if player.playbackState() != QtMultimedia.QMediaPlayer.PlaybackState.PlayingState:
            if self._active is key or player.position() != target:
                player.setPosition(target)
            player.play()
        self._active = key

    def pause(self) -> None:
        if self._active is None:
            return
        player = self._players.get(self._active)
        if player is not None and player.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState:
            player.pause()

    def stop(self) -> None:
        for player in self._players.values():
            if player.playbackState() != QtMultimedia.QMediaPlayer.PlaybackState.StoppedState:
                player.stop()
        if self._active is not None:
            self.seek(0)

    def seek(self, position_ms: int) -> None:
        if self._active is None:
            return
        player = self._players.get(self._active)
        if player is not None:
            player.setPosition(self._clamp_position(self._active, position_ms))

    def _clamp_position(self, key: SampleKey, position_ms: int) -> int:
        duration = self._durations.get(key, 0)
        position = max(0, int(position_ms))
        if duration > 0:
            position = min(position, max(0, duration - 60))
        return position

    @property
    def position_ms(self) -> int:
        if self._active is None:
            return 0
        player = self._players.get(self._active)
        if player is None:
            return 0
        position = int(player.position())
        duration = self._durations.get(self._active, 0)
        if self._loop and duration > 0 and position >= duration - 40:
            # Loop by seeking back; QMediaPlayer.Loops is only available from
            # Qt 6.7 and we support 6.5.
            player.setPosition(0)
            return 0
        return position

    @property
    def is_playing(self) -> bool:
        if self._active is None:
            return False
        player = self._players.get(self._active)
        return bool(
            player is not None
            and player.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState
        )

    def playback_state(self) -> str:
        if self._active is None:
            return "stopped"
        player = self._players.get(self._active)
        if player is None:
            return "stopped"
        if player.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState:
            return "playing"
        if player.mediaStatus() in (
            QtMultimedia.QMediaPlayer.MediaStatus.BufferingMedia,
            QtMultimedia.QMediaPlayer.MediaStatus.LoadedMedia,
            QtMultimedia.QMediaPlayer.MediaStatus.LoadingMedia,
        ):
            return "buffering"
        return "stopped"

    @property
    def duration_ms(self) -> int:
        return self._durations.get(self._active, 0) if self._active else 0

    def set_loop(self, enabled: bool) -> None:
        self._loop = bool(enabled)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        for output in self._outputs.values():
            output.setVolume(self._volume)

    def set_muted(self, muted: bool) -> None:
        for output in self._outputs.values():
            output.setMuted(bool(muted))

    def set_rate(self, rate: float) -> None:
        for player in self._players.values():
            player.setPlaybackRate(max(0.25, min(4.0, rate)))


class SoundDeviceBackend(PlaybackBackend):
    """Sample-accurate switching straight from memory.

    Both segments are decoded once into ``(frames, channels)`` float arrays.  A
    single :class:`sounddevice.RawOutputStream` callback reads from whichever
    buffer is active, so pressing B mid-bar is an atomic pointer swap with zero
    gap — the reference implementation for an ABX tester.
    """

    key = "sounddevice"
    label = BACKEND_LABELS["sounddevice"]

    def __init__(self) -> None:
        super().__init__()
        import sounddevice as sd  # noqa: PLC0415 - optional dependency

        self._sd = sd
        self._arrays: dict[SampleKey, object] = {}
        self._rates: dict[SampleKey, int] = {}
        self._frames: dict[SampleKey, int] = {}
        self._stream = None
        self._active: SampleKey | None = None
        self._cursor = 0  # in frames
        self._lock = threading.Lock()
        self._loop = True
        self._volume = 0.8
        self._rate = 1.0
        self._channels = 2
        self._muted = False

    def load(self, sample: PreparedSample) -> None:
        import numpy as np  # noqa: PLC0415

        data, rate = read_wav_float32(sample.wav_path)
        del np  # only needed for the reshape inside read_wav_float32
        self._arrays[sample.key] = data
        self._rates[sample.key] = rate
        self._frames[sample.key] = int(data.shape[0])
        self._channels = int(data.shape[1])

    def unload(self) -> None:
        self._close_stream()
        self._arrays.clear()
        self._rates.clear()
        self._frames.clear()
        self._active = None
        self._cursor = 0

    # -- stream ----------------------------------------------------------
    def _open_stream(self) -> None:
        if self._stream is not None:
            return
        rate = self._rates[self._active] if self._active else 44100
        try:
            self._stream = self._sd.RawOutputStream(
                samplerate=int(rate * max(0.25, min(4.0, self._rate))),
                channels=min(self._channels, 2),
                dtype="float32",
                blocksize=0,
                latency="low",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - no device, busy, etc.
            self._stream = None
            self._fail(f"sounddevice could not open an output stream: {exc}")

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001, S110  # pragma: no cover - best effort
            pass

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001, ARG002
        """Fill ``outdata`` from the active buffer, wrapping when looping."""
        import numpy as np  # noqa: PLC0415

        if status and hasattr(self._sd, "post_callback_errors"):  # pragma: no cover
            self._sd.post_callback_errors(status)
        channels = outdata.shape[1]
        with self._lock:
            key = self._active
            cursor = self._cursor
        buffer = self._arrays.get(key) if key is not None else None
        if buffer is None:
            outdata[:] = 0.0
            return
        total = buffer.shape[0]
        if total == 0:
            outdata[:] = 0.0
            return
        end = cursor + frames
        if end <= total:
            chunk = buffer[cursor:end]
        elif self._loop:
            # Fill from the end, then wrap around to the start.
            repeats = -(-end // total)  # ceil division
            chunk = np.resize(buffer, (total * repeats, buffer.shape[1]))[cursor:end]
        else:
            chunk = np.zeros((frames, buffer.shape[1]), dtype=np.float32)
            take = max(0, total - cursor)
            if take:
                chunk[:take] = buffer[cursor:total]
        with self._lock:
            self._cursor = cursor + frames
        if channels != chunk.shape[1]:
            if chunk.shape[1] == 1:
                chunk = np.repeat(chunk, channels, axis=1)
            else:
                chunk = chunk[:, :channels]
        if self._muted:
            chunk = chunk * 0.0
        outdata[:] = chunk.astype(np.float32, copy=False)

    # -- transport -------------------------------------------------------
    def play(self, key: SampleKey, position_ms: int) -> None:
        if key not in self._arrays:
            self._fail("Sample is not loaded.")
            return
        with self._lock:
            self._active = key
            rate = self._rates[key] or 44100
            self._cursor = max(0, int(position_ms * rate / 1000))
            if self._cursor >= self._frames.get(key, 0):
                self._cursor = 0
        self._open_stream()
        if self._stream is not None and self._stream.samplerate != int(rate * self._rate):
            self._close_stream()  # sample rate changed: reopen
            self._open_stream()

    def pause(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:  # noqa: BLE001, S110  # pragma: no cover
                pass

    def stop(self) -> None:
        self.pause()
        with self._lock:
            self._cursor = 0

    def seek(self, position_ms: int) -> None:
        with self._lock:
            key = self._active
            rate = self._rates.get(key, 44100) if key else 44100
            self._cursor = max(0, int(position_ms * rate / 1000))

    @property
    def position_ms(self) -> int:
        with self._lock:
            key, cursor = self._active, self._cursor
        if key is None:
            return 0
        rate = self._rates.get(key, 44100) or 44100
        return int(cursor * 1000 / rate)

    @property
    def is_playing(self) -> bool:
        return self._stream is not None and self._stream.active

    @property
    def duration_ms(self) -> int:
        with self._lock:
            key = self._active
        if key is None:
            return 0
        rate = self._rates.get(key, 44100) or 44100
        return int(self._frames.get(key, 0) * 1000 / rate)

    def set_loop(self, enabled: bool) -> None:
        self._loop = bool(enabled)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))

    def set_rate(self, rate: float) -> None:
        self._rate = max(0.25, min(4.0, rate))
        if self._stream is not None:
            self._close_stream()  # reopen at the new rate when playing again

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)


class CliBackend(PlaybackBackend):
    """External player fallback (``ffplay``/``paplay``/``aplay``/``afplay``)."""

    key = "cli"
    label = BACKEND_LABELS["cli"]
    supports_seek = False
    supports_loop = False

    def __init__(self, binary: str | None = None, template: Sequence[str] = ()) -> None:
        super().__init__()
        self._paths: dict[SampleKey, Path] = {}
        self._process: subprocess.Popen[bytes] | None = None
        self._active: SampleKey | None = None
        self._duration = 0
        self._loop = False
        chosen = (binary, tuple(template)) if binary else next(
            ((name, tpl) for name, tpl in _CLI_PLAYERS if shutil.which(name)), ("", ())
        )
        self._binary, self._template = chosen[0], tuple(chosen[1])
        if not self._binary:  # pragma: no cover - depends on the system
            raise PlaybackError(
                "No external audio player was found.",
                hint="Install ffplay (part of ffmpeg), PulseAudio's paplay, or ALSA's aplay.",
            )

    @property
    def available(self) -> bool:
        return bool(self._binary)

    def load(self, sample: PreparedSample) -> None:
        self._paths[sample.key] = sample.wav_path
        self._duration = int(sample.duration_sec * 1000)

    def unload(self) -> None:
        self._terminate()
        self._paths.clear()
        self._active = None

    def play(self, key: SampleKey, position_ms: int) -> None:  # noqa: ARG002 - cannot seek
        path = self._paths.get(key)
        if path is None:
            self._fail("Sample is not loaded.")
            return
        self._terminate()
        argv = [self._binary, *[arg.format(path=str(path)) for arg in self._template]]
        try:
            self._process = subprocess.Popen(  # noqa: S603 - fixed argv from _CLI_PLAYERS
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self._process = None
            self._fail(f"Could not start {self._binary}: {exc}")
            return
        self._active = key

    def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        except Exception:  # noqa: BLE001  # pragma: no cover - best effort
            pass

    def pause(self) -> None:
        self._terminate()

    def stop(self) -> None:
        self._terminate()

    def seek(self, position_ms: int) -> None:
        """Not supported: external players are started from the beginning."""

    @property
    def position_ms(self) -> int:
        return 0

    @property
    def is_playing(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def duration_ms(self) -> int:
        return self._duration if self._active else 0

    def set_loop(self, enabled: bool) -> None:
        self._loop = bool(enabled)


def available_backends() -> list[BackendInfo]:
    """What this machine can actually do, best first."""
    infos: list[BackendInfo] = []
    ok, detail = _sounddevice_available()
    infos.append(BackendInfo("sounddevice", BACKEND_LABELS["sounddevice"], ok, detail))
    infos.append(
        BackendInfo(
            "qt",
            BACKEND_LABELS["qt"],
            media_available(),
            "built into Qt" if media_available() else "Qt Multimedia missing",
        )
    )
    ok, detail = _cli_player()
    infos.append(BackendInfo("cli", BACKEND_LABELS["cli"], ok, detail))
    return infos


def _url(path: Path) -> "QtCore.QUrl":
    return QtCore.QUrl.fromLocalFile(str(path))


# --------------------------------------------------------------------------- #
#  Facade
# --------------------------------------------------------------------------- #
class Player(QObject):
    """Backend-agnostic transport with a single shared position cursor.

    The window only ever talks to this object: ``play(SampleKey.A)`` keeps the
    current millisecond, so switching mid-bar compares the *same* moment of the
    song on both sides.
    """

    positionChanged = Signal(int)     # milliseconds
    durationChanged = Signal(int)
    stateChanged = Signal(bool)       # is_playing
    activeChanged = Signal(object)    # SampleKey | None
    errorOccurred = Signal(str)
    loaded = Signal(object)           # PreparedPair
    backendChanged = Signal(str)

    def __init__(self, preference: str = "auto", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pair = None
        self._backend: PlaybackBackend | None = None
        self._preference = preference
        self._active: SampleKey | None = None
        self._cursor_ms = 0
        self._duration_ms = 0
        self._playing = False
        self._muted = False
        self._volume = 0.8
        self._rate = 1.0
        self._loop = True
        self._poll = None
        # Playback start bookkeeping: a backend needs a moment before it is
        # actually producing sound (see STARTUP_GRACE).
        self._started = False
        self._play_requested_at = 0.0

    # -- backend selection ----------------------------------------------
    def select_backend(self, preference: str = "auto") -> str:
        """(Re)create the backend; returns the key actually chosen."""
        preference = preference or "auto"
        order = (
            ["sounddevice", "qt", "cli"]
            if preference == "auto"
            else [preference, "qt", "cli"]
        )
        infos = {info.key: info for info in available_backends()}
        chosen = ""
        backend: PlaybackBackend | None = None
        for key in order:
            info = infos.get(key)
            if info is None or not info.available:
                continue
            try:
                backend = self._make(key)
            except Exception:  # noqa: BLE001 - try the next backend
                continue
            chosen = key
            break
        if backend is None:
            self._backend = None
            self.errorOccurred.emit(_no_backend_message(infos))
            return "none"
        backend.on_event = lambda message: self.errorOccurred.emit(message)
        self._backend = backend
        backend.set_loop(self._loop)
        backend.set_volume(self._volume)
        backend.set_rate(self._rate)
        self._apply_mute()
        self.backendChanged.emit(chosen)
        if self._pair is not None:
            for sample in self._pair:
                try:
                    backend.load(sample)
                except Exception as exc:  # noqa: BLE001 - surface, do not crash
                    self.errorOccurred.emit(f"{self._backend.label}: {exc}")
            self._duration_ms = int(self._pair.duration_sec * 1000)
            self.durationChanged.emit(self._duration_ms)
        return chosen

    @staticmethod
    def _make(key: str) -> PlaybackBackend:
        if key == "sounddevice":
            return SoundDeviceBackend()
        if key == "qt":
            return QtMediaBackend()
        if key == "cli":
            return CliBackend()
        raise PlaybackError(f"Unknown playback backend: {key}")

    def _apply_mute(self) -> None:
        if self._backend is None:
            return
        if isinstance(self._backend, (SoundDeviceBackend, QtMediaBackend)):
            # Both support true muting, which keeps the volume slider intact.
            self._backend.set_muted(self._muted)
        else:
            self._backend.set_volume(0.0 if self._muted else self._volume)

    # -- loading ---------------------------------------------------------
    def load_pair(self, pair: object) -> None:
        """Load a :class:`~flacblind.core.models.PreparedPair`."""
        self.stop()
        self._pair = pair
        self._cursor_ms = 0
        self._active = None
        if self._backend is None:
            self.select_backend(self._preference)
        if self._backend is None:
            return
        for sample in pair:  # type: ignore[attr-defined]
            try:
                self._backend.load(sample)
            except Exception as exc:  # noqa: BLE001
                self.errorOccurred.emit(f"{self._backend.label}: {exc}")
        self._duration_ms = int(getattr(pair, "duration_sec", 0.0) * 1000)
        self.durationChanged.emit(self._duration_ms)
        self.positionChanged.emit(0)
        self.loaded.emit(pair)

    def unload(self) -> None:
        self.stop()
        if self._backend is not None:
            self._backend.unload()
        self._pair = None
        self._duration_ms = 0
        self._cursor_ms = 0
        self.durationChanged.emit(0)
        self.positionChanged.emit(0)

    # -- transport -------------------------------------------------------
    def play(self, key: SampleKey | None = None) -> None:
        """Play ``key`` (default: the active one) keeping the position."""
        if self._backend is None:
            self.errorOccurred.emit("No playback backend is available.")
            return
        key = key or self._active or SampleKey.A
        if self._playing and self._active is key:
            return
        previous = self._active
        self._active = key
        if self._pair is None or self._pair.get(key).wav_path is None:  # type: ignore[attr-defined]
            self._fail_missing(key)
            return
        self._apply_mute()
        self._backend.set_loop(self._loop)
        self._play(self._cursor_ms, key=key)
        if self._active is not previous:
            self.activeChanged.emit(key)

    def toggle(self, key: SampleKey | None = None) -> None:
        """Space-bar behaviour: resume, or pause if this sample is playing."""
        key = key or self._active
        if self._playing and (key is None or self._active is key):
            self.pause()
        else:
            self.play(key)

    def pause(self) -> None:
        if self._backend is None or not self._playing:
            return
        self._backend.pause()
        self._started = False
        self._set_playing(False)
        self._stop_polling()
        self._sync_cursor(force=True)
        self.positionChanged.emit(self._cursor_ms)

    def stop(self) -> None:
        if self._backend is not None:
            self._backend.stop()
        self._cursor_ms = 0
        self._started = False
        self._set_playing(False)
        self._stop_polling()
        self.positionChanged.emit(0)

    def seek_ms(self, position_ms: int) -> None:
        if self._backend is None or not self._backend.supports_seek:
            return
        self._cursor_ms = max(0, int(position_ms))
        if self._duration_ms:
            self._cursor_ms = min(self._cursor_ms, max(0, self._duration_ms - 40))
        self._backend.seek(self._cursor_ms)
        self.positionChanged.emit(self._cursor_ms)

    def seek_fraction(self, fraction: float) -> None:
        if not self._duration_ms:
            return
        self.seek_ms(int(max(0.0, min(1.0, fraction)) * self._duration_ms))

    def nudge(self, delta_ms: int) -> None:
        self.seek_ms(self._cursor_ms + delta_ms)

    # -- parameters ------------------------------------------------------
    def set_loop(self, enabled: bool) -> None:
        self._loop = bool(enabled)
        if self._backend is not None:
            self._backend.set_loop(self._loop)

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self._apply_mute()

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._apply_mute()

    def set_rate(self, rate: float) -> None:
        self._rate = max(0.25, min(4.0, rate))
        if self._backend is not None:
            self._backend.set_rate(self._rate)

    # -- polling ---------------------------------------------------------
    def _start_polling(self) -> None:
        if self._poll is None:
            self._poll = QtCore.QTimer(self)
            self._poll.setInterval(POLL_MS)
            self._poll.timeout.connect(self._tick)
        if not self._poll.isActive():
            self._poll.start()

    def _stop_polling(self) -> None:
        if self._poll is not None and self._poll.isActive():
            self._poll.stop()

    def _tick(self) -> None:
        """Poll the backend: keep the playhead fresh, detect the end of a track."""
        backend = self._backend
        if backend is None:
            self._set_playing(False)
            self._stop_polling()
            return
        self._sync_cursor()
        error = backend.last_error
        if error:
            backend.last_error = ""
            self.errorOccurred.emit(error)
        if not self._playing:
            return

        state = backend.playback_state()
        if state == "playing":
            self._started = True
            return
        if state == "buffering":
            # Still opening the device / filling buffers: not an end of track.
            return
        if not self._started and (monotonic() - self._play_requested_at) < STARTUP_GRACE:
            return

        # A definite stop: either the track wrapped to its end (loop mode) or
        # playback could not be started at all.
        self._sync_cursor(force=True)
        if self._loop and self._duration_ms and self._cursor_ms >= self._duration_ms - LOOP_TOLERANCE_MS:
            self._play(0, key=self._active or SampleKey.A)
            return
        if not self._started:
            self._started = False
            self._set_playing(False)
            self._stop_polling()
            self.errorOccurred.emit(NO_AUDIO_ERROR)
            return
        self._set_playing(False)
        self._stop_polling()

    def _play(self, position_ms: int, key: SampleKey | None = None) -> None:
        """(Re)start the backend at ``position_ms`` and arm the start timer."""
        assert self._backend is not None
        self._cursor_ms = max(0, int(position_ms))
        self._backend.play(key or self._active or SampleKey.A, self._cursor_ms)
        self._play_requested_at = monotonic()
        self._started = False
        self._set_playing(True)
        self._start_polling()

    def _sync_cursor(self, force: bool = False) -> None:
        if self._backend is None:
            return
        position = self._backend.position_ms
        if force or position != self._cursor_ms:
            self._cursor_ms = position
            self.positionChanged.emit(position)

    def _set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.stateChanged.emit(playing)

    def _fail_missing(self, key: SampleKey) -> None:
        self._set_playing(False)
        self.errorOccurred.emit(f"Sample {key.value} is not loaded.")

    # -- introspection ---------------------------------------------------
    @property
    def active(self) -> SampleKey | None:
        return self._active

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def position_ms(self) -> int:
        return self._cursor_ms

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def backend_key(self) -> str:
        return self._backend.key if self._backend is not None else "none"

    @property
    def backend_label(self) -> str:
        return self._backend.label if self._backend is not None else BACKEND_LABELS["none"]

    @property
    def supports_seek(self) -> bool:
        return bool(self._backend and self._backend.supports_seek)

    @property
    def supports_loop(self) -> bool:
        return bool(self._backend and self._backend.supports_loop)

    def capabilities(self) -> str:
        """Short quality note shown next to the backend selector."""
        if self._backend is None:
            return ""
        if self._backend.key == "sounddevice":
            return "sample-accurate"
        if self._backend.key == "cli":
            return "no seek"
        return ""

    def shutdown(self) -> None:
        self._stop_polling()
        if self._backend is not None:
            self._backend.unload()
            self._backend = None


def _no_backend_message(infos: dict[str, BackendInfo]) -> str:
    details = "; ".join(f"{info.label}: {info.detail}" for info in infos.values() if not info.available)
    return (
        "No playback backend is available.\n"
        + (f"Unavailable: {details}\n" if details else "")
        + "Install sounddevice (pip install sounddevice) or ffmpeg's ffplay, or check your audio device."
    )
