"""Playback facade: the position-locked A/B switch, transport and fallbacks.

Uses a recording fake backend, so the seamless-switching contract is verified
without an audio device (the real backends need one).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from time import monotonic

from helpers import ROOT, require_ffmpeg, write_quiet_wav  # noqa: F401

from flacblind.core.models import (
    AudioKind,
    AudioFormat,
    Peaks,
    PreparedSample,
    SampleKey,
    SampleStats,
)
from flacblind.ui.player import (
    BACKEND_LABELS,
    NO_AUDIO_ERROR,
    STARTUP_GRACE,
    CliBackend,
    PlaybackBackend,
    QtMediaBackend,
    available_backends,
)
from flacblind.ui.qtcompat import QApplication, QtCore, media_available


_app = QApplication.instance() or QApplication(sys_argv := ["flacblind-tests"])


def make_sample(key: SampleKey, seconds: float = 10.0, wav_path: Path | None = None) -> PreparedSample:
    rate = 44100
    return PreparedSample(
        key=key,
        kind=AudioKind.LOSSLESS if key is SampleKey.A else AudioKind.LOSSY,
        fmt=AudioFormat.FLAC,
        source_path=Path(f"/music/{key.value}.flac"),
        wav_path=wav_path or Path(f"/tmp/{key.value}.wav"),
        duration_sec=seconds,
        sample_rate=rate,
        channels=2,
        frames=int(seconds * rate),
        peaks=Peaks((-0.5,) * 100, (0.5,) * 100, (0.2,) * 100, int(seconds * rate), rate),
        stats=SampleStats(-6.0, -12.0, 6.0),
    )


class FakeBackend(PlaybackBackend):
    """Records every transport call, like a very obedient speaker."""

    key = "fake"
    label = "Fake"

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self._position = 0
        self._playing = False
        self._duration = 10_000
        self.volume = 1.0
        self.loop = True
        self.rate = 1.0
        self.loaded: list[SampleKey] = []

    def load(self, sample: PreparedSample) -> None:
        self.loaded.append(sample.key)
        self._duration = int(sample.duration_sec * 1000)

    def unload(self) -> None:
        self.calls.append(("unload",))

    def play(self, key: SampleKey, position_ms: int) -> None:
        self.calls.append(("play", key, position_ms))
        self._position = position_ms
        self._playing = True

    def pause(self) -> None:
        self.calls.append(("pause",))
        self._playing = False

    def stop(self) -> None:
        self.calls.append(("stop",))
        self._position = 0
        self._playing = False

    def seek(self, position_ms: int) -> None:
        self.calls.append(("seek", position_ms))
        self._position = position_ms

    @property
    def position_ms(self) -> int:
        return self._position

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def duration_ms(self) -> int:
        return self._duration

    def set_loop(self, enabled: bool) -> None:
        self.loop = enabled

    def set_volume(self, volume: float) -> None:
        self.volume = volume

    def set_rate(self, rate: float) -> None:
        self.rate = rate

    def advance(self, ms: int) -> None:
        self._position += ms


class FakePair:
    """Minimal PreparedPair stand-in (the Player only uses these members)."""

    def __init__(self, a: PreparedSample, b: PreparedSample) -> None:
        self.sample_a, self.sample_b = a, b
        self.duration_sec = min(a.duration_sec, b.duration_sec)

    def __iter__(self):
        yield self.sample_a
        yield self.sample_b

    def get(self, key: SampleKey) -> PreparedSample:
        return self.sample_a if key is SampleKey.A else self.sample_b


def make_player() -> "Player":
    from flacblind.ui.player import Player

    player = Player("auto")
    backend = FakeBackend()
    player._backend = backend
    player.load_pair(FakePair(make_sample(SampleKey.A), make_sample(SampleKey.B)))
    backend.calls.clear()
    return player, backend


class TestSeamlessSwitch(unittest.TestCase):
    def setUp(self) -> None:
        self.player, self.backend = make_player()

    def test_switch_preserves_position(self) -> None:
        self.player.play(SampleKey.A)
        self.backend.advance(2500)
        self.player._sync_cursor(force=True)
        self.assertEqual(self.player.position_ms, 2500)

        self.player.play(SampleKey.B)
        play_calls = [call for call in self.backend.calls if call[0] == "play"]
        self.assertEqual(play_calls[-1], ("play", SampleKey.B, 2500))
        self.assertIs(self.player.active, SampleKey.B)

    def test_repeated_switches_stay_aligned(self) -> None:
        self.player.play(SampleKey.A)
        self.backend.advance(1000)
        self.player._sync_cursor(force=True)
        for _ in range(6):
            other = SampleKey.B if self.player.active is SampleKey.A else SampleKey.A
            self.player.play(other)
            self.backend.advance(300)
            self.player._sync_cursor(force=True)
        # Every switch resumes exactly where the previous sample stopped:
        # 1000, 1300, 1600, … — no drift, and the cursor ends at 2800.
        positions = [call[2] for call in self.backend.calls if call[0] == "play"][1:]
        self.assertEqual(positions, [1000 + 300 * i for i in range(6)])
        self.assertEqual(self.player.position_ms, 2800)

    def test_replaying_the_same_sample_does_not_restart(self) -> None:
        self.player.play(SampleKey.A)
        self.backend.advance(4000)
        self.player._sync_cursor(force=True)
        self.player.play(SampleKey.A)
        self.assertEqual(len([c for c in self.backend.calls if c[0] == "play"]), 1)
        self.assertEqual(self.player.position_ms, 4000)

    def test_active_changed_signal(self) -> None:
        seen: list[object] = []
        self.player.activeChanged.connect(seen.append)
        self.player.play(SampleKey.A)
        self.player.play(SampleKey.B)
        self.player.play(SampleKey.B)  # no change
        self.assertEqual(seen, [SampleKey.A, SampleKey.B])

    def test_both_samples_are_preloaded(self) -> None:
        self.assertEqual(self.backend.loaded, [SampleKey.A, SampleKey.B])


@unittest.skipUnless(media_available(), "Qt Multimedia is not available")
class TestQtMediaBackendReal(unittest.TestCase):
    """Exercise the real Qt backend.

    The fake-backend tests above cannot catch a removed Qt 6 API (QMediaPlayer
    lost setVolume()/setNotificationMode()), which is exactly how playback
    silently died once already.  These tests load real WAV files, so any such
    call fails here instead of in the user's face.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-qtmedia-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.samples = [make_sample(SampleKey.A, 0.5), make_sample(SampleKey.B, 0.5)]
        for index, sample in enumerate(self.samples):
            wav = write_quiet_wav(self.tmp / f"{index}.wav", seconds=0.5)
            self.samples[index] = replace(sample, wav_path=wav)

    def test_load_populates_both_players(self) -> None:
        backend = QtMediaBackend()
        for sample in self.samples:
            backend.load(sample)
        self.assertEqual(set(backend._players), {SampleKey.A, SampleKey.B})
        self.assertEqual(backend.last_error, "")
        self.assertEqual(backend._durations[SampleKey.A], 500)
        backend.play(SampleKey.A, 0)
        self.assertGreater(backend.duration_ms, 0)

    def test_transport_calls_do_not_raise(self) -> None:
        backend = QtMediaBackend()
        for sample in self.samples:
            backend.load(sample)
        backend.set_volume(0.5)      # used to raise AttributeError
        backend.set_muted(True)
        backend.set_muted(False)
        backend.set_rate(1.0)
        backend.set_loop(True)
        backend.play(SampleKey.A, 0)
        self.assertIn(backend.playback_state(), ("playing", "buffering", "stopped"))
        backend.seek(200)
        self.assertLessEqual(backend.position_ms, backend.duration_ms)
        backend.pause()
        backend.play(SampleKey.B, 200)   # seamless switch
        self.assertIsNotNone(backend.position_ms)
        backend.stop()
        backend.unload()
        self.assertEqual(backend._players, {})

    def test_missing_file_is_reported_not_raised(self) -> None:
        backend = QtMediaBackend()
        broken = replace(self.samples[0], wav_path=self.tmp / "ghost.wav")
        backend.load(broken)
        backend.play(SampleKey.A, 0)
        backend.pause()
        # Whatever the platform does, it must not raise out of the backend.
        self.assertIsInstance(backend.last_error, str)


class TestStartupGrace(unittest.TestCase):
    """A backend that is still buffering must not be mistaken for "finished"."""

    class SlowBackend(FakeBackend):
        def __init__(self, state: str = "buffering") -> None:
            super().__init__()
            self.state = state
            self.plays = 0

        def play(self, key, position_ms):  # noqa: ANN001, ANN202
            self.plays += 1
            super().play(key, position_ms)

        def playback_state(self) -> str:
            return self.state

    def _player(self, backend) -> "Player":  # noqa: ANN001
        from flacblind.ui.player import Player

        player = Player("auto")
        player._backend = backend
        player.load_pair(FakePair(self.samples[0], self.samples[1]))
        return player

    def setUp(self) -> None:
        self.samples = [make_sample(SampleKey.A, 10.0), make_sample(SampleKey.B, 10.0)]
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-grace-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_buffering_is_not_an_end_of_track(self) -> None:
        backend = self.SlowBackend("buffering")
        player = self._player(backend)
        errors: list[str] = []
        player.errorOccurred.connect(errors.append)
        player.play(SampleKey.A)
        for _ in range(20):      # ~800 ms of polling
            player._tick()
        self.assertTrue(player.is_playing, "buffering must not stop playback")
        self.assertEqual(errors, [])
        self.assertEqual(backend.plays, 1, "playback must not be restarted")

    def test_backend_that_never_starts_reports_an_error(self) -> None:
        backend = self.SlowBackend("stopped")
        player = self._player(backend)
        errors: list[str] = []
        player.errorOccurred.connect(errors.append)
        player.play(SampleKey.A)
        player._play_requested_at = monotonic() - (STARTUP_GRACE + 1)
        player._tick()
        self.assertFalse(player.is_playing)
        self.assertTrue(any(NO_AUDIO_ERROR in message for message in errors), errors)

    def test_loop_wraps_instead_of_stopping(self) -> None:
        """Reaching the end of a looping segment restarts it, not stops it."""
        backend = self.SlowBackend("stopped")
        player = self._player(backend)
        player.set_loop(True)
        player.play(SampleKey.A)
        player._started = True                     # it really was playing
        backend._position = player.duration_ms - 10  # the track ran out
        player._tick()
        self.assertTrue(player.is_playing)
        self.assertEqual(player.position_ms, 0)
        self.assertEqual(backend.plays, 2)

    def test_stopped_backend_ends_playback_after_a_real_start(self) -> None:
        backend = self.SlowBackend("stopped")
        player = self._player(backend)
        player.play(SampleKey.A)
        player._started = True          # it really was playing before
        player._tick()
        self.assertFalse(player.is_playing)


class TestTransport(unittest.TestCase):
    def setUp(self) -> None:
        self.player, self.backend = make_player()

    def test_toggle_play_pause(self) -> None:
        states: list[bool] = []
        self.player.stateChanged.connect(states.append)
        self.player.toggle(SampleKey.A)
        self.assertTrue(self.player.is_playing)
        self.player.toggle()
        self.assertFalse(self.player.is_playing)
        self.player.toggle()
        self.assertTrue(self.player.is_playing)
        self.assertEqual(states, [True, False, True])

    def test_stop_resets_the_cursor(self) -> None:
        self.player.play(SampleKey.A)
        self.backend.advance(3000)
        self.player._sync_cursor(force=True)
        self.player.stop()
        self.assertEqual(self.player.position_ms, 0)
        self.assertFalse(self.player.is_playing)

    def test_seek_is_clamped(self) -> None:
        self.player.seek_ms(999_999)
        self.assertLessEqual(self.player.position_ms, self.player.duration_ms)
        self.player.seek_ms(-500)
        self.assertEqual(self.player.position_ms, 0)

    def test_seek_fraction(self) -> None:
        self.player.seek_fraction(0.5)
        self.assertAlmostEqual(self.player.position_ms, self.player.duration_ms / 2, delta=1)

    def test_nudge(self) -> None:
        self.player.seek_ms(1000)
        self.player.nudge(-400)
        self.assertEqual(self.player.position_ms, 600)
        self.player.nudge(400)
        self.assertEqual(self.player.position_ms, 1000)

    def test_volume_and_mute_reach_the_backend(self) -> None:
        self.player.set_volume(0.42)
        self.assertAlmostEqual(self.backend.volume, 0.42)
        self.player.set_volume(5.0)
        self.assertEqual(self.backend.volume, 1.0)
        self.player.set_muted(True)
        self.assertEqual(self.backend.volume, 0.0)
        self.player.set_muted(False)
        self.assertAlmostEqual(self.backend.volume, 1.0)

    def test_loop_and_rate(self) -> None:
        self.player.set_loop(False)
        self.assertFalse(self.backend.loop)
        self.player.set_rate(1.5)
        self.assertEqual(self.backend.rate, 1.5)
        self.player.set_rate(99.0)
        self.assertEqual(self.backend.rate, 4.0)

    def test_unload(self) -> None:
        self.player.unload()
        self.assertEqual(self.player.duration_ms, 0)
        self.assertIn(("unload",), self.backend.calls)

    def test_playing_without_a_pair_is_reported(self) -> None:
        player, backend = make_player()
        player.unload()
        errors: list[str] = []
        player.errorOccurred.connect(errors.append)
        player.play(SampleKey.A)
        self.assertTrue(errors)


class TestBackendSelection(unittest.TestCase):
    def test_discovery_lists_all_three(self) -> None:
        keys = {info.key for info in available_backends()}
        self.assertEqual(keys, {"sounddevice", "qt", "cli"})

    def test_every_backend_has_a_label(self) -> None:
        for info in available_backends():
            self.assertIn(info.key, BACKEND_LABELS)
            self.assertTrue(info.label)

    def test_auto_selection_never_crashes(self) -> None:
        from flacblind.ui.player import Player

        player = Player("auto")
        chosen = player.select_backend("auto")
        self.assertIn(chosen, {"sounddevice", "qt", "cli", "none"})
        if chosen != "none":
            self.assertTrue(player.supports_seek or player.backend_key == "cli")
        player.shutdown()

    def test_unknown_preference_falls_back(self) -> None:
        from flacblind.ui.player import Player

        player = Player("nonsense")
        chosen = player.select_backend("nonsense")
        self.assertIn(chosen, {"qt", "cli", "none"})
        player.shutdown()


@require_ffmpeg
class TestCliBackend(unittest.TestCase):
    def test_cli_backend_plays_and_terminates(self) -> None:
        from helpers import make_tone_wav

        tmp = Path(tempfile.mkdtemp(prefix="fb-test-cli-"))
        try:
            wav = make_tone_wav(tmp / "tone.wav", seconds=0.2)
            backend = CliBackend()
            if not backend.available:
                self.skipTest("no external player installed")
            backend.load(make_sample(SampleKey.A, seconds=0.2))
            backend.play(SampleKey.A, 0)
            self.assertTrue(backend.is_playing)
            backend.pause()
            self.assertFalse(backend.is_playing)
            self.assertFalse(backend.supports_seek)
            self.assertFalse(backend.supports_loop)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
