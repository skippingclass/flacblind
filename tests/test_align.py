"""Sample alignment: the offset detector and the shared-length rendering."""

from __future__ import annotations

import math
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

from helpers import HAS_FFMPEG, ROOT, make_pair, require_ffmpeg  # noqa: F401

from flacblind.core import FFmpeg, AudioPreparer, PrepareRequest, SourceSpec
from flacblind.core.align import (
    HAVE_NUMPY,
    MAX_OFFSET_SEC,
    MIN_ALIGN_MS,
    estimate_offset_samples,
    needs_alignment,
)
from flacblind.core.models import (
    AudioConfig,
    AudioKind,
    PreparedSample,
    SampleKey,
    SegmentSpec,
)


def write_shifted_wav(path: Path, *, shift: int, seconds: float = 4.0, rate: int = 44100) -> Path:
    """A deterministic tone/noise mix, optionally delayed by ``shift`` samples."""
    frames = int(seconds * rate)
    payload = bytearray()
    noise = 0
    for i in range(frames + shift):
        if i < shift:
            payload += b"\x00\x00" * 2      # silence before the signal starts
            continue
        n = i - shift
        noise = (1103515245 * noise + 12345) & 0x7FFFFFFF   # cheap LCG
        value = int(9000 * math.sin(2 * math.pi * 220 * n / rate) + (noise % 2000) - 1000)
        value = max(-32768, min(32767, value))
        payload += value.to_bytes(2, "little", signed=True)
        payload += value.to_bytes(2, "little", signed=True)  # same signal both channels
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(payload))
    return path


def make_sample(key: SampleKey, wav: Path, frames: int) -> PreparedSample:
    from flacblind.core.models import AudioFormat, Peaks, SampleStats

    return PreparedSample(
        key=key,
        kind=AudioKind.LOSSLESS,
        fmt=AudioFormat.WAV,
        source_path=wav,
        wav_path=wav,
        duration_sec=frames / 44100,
        sample_rate=44100,
        channels=2,
        frames=frames,
        peaks=Peaks((0.0,), (0.0,), (0.0,), frames, 44100),
        stats=SampleStats(-6, -12, 6),
    )


@unittest.skipUnless(HAVE_NUMPY, "numpy is not installed")
class TestOffsetEstimation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-align-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_detects_a_known_delay(self) -> None:
        reference = write_shifted_wav(self.tmp / "ref.wav", shift=0)
        for shift in (441, 1105, 2646):        # 10, 25 and 60 ms at 44.1 kHz
            delayed = write_shifted_wav(self.tmp / f"delay{shift}.wav", shift=shift)
            lag = estimate_offset_samples(reference, delayed)
            self.assertIsNotNone(lag, f"no lag found for {shift}")
            assert lag is not None
            self.assertGreater(lag, 0, "a delayed second file must report a positive lag")
            # The correlation runs on a signal decimated to 8 kHz, so allow one
            # decimation step of tolerance (44100/8000 = 5.5 samples).
            self.assertAlmostEqual(lag, shift, delta=8, msg=f"shift={shift}")

    def test_zero_for_identical_files(self) -> None:
        path = write_shifted_wav(self.tmp / "same.wav", shift=0)
        self.assertEqual(estimate_offset_samples(path, path), 0)

    def test_detects_a_negative_lag(self) -> None:
        early = write_shifted_wav(self.tmp / "early.wav", shift=0)
        late = write_shifted_wav(self.tmp / "late.wav", shift=2000)
        # A is early, B is late -> positive lag for (A, B)
        self.assertGreater(estimate_offset_samples(early, late) or 0, 0)
        # … and the same pair reversed gives a negative lag.
        self.assertLess(estimate_offset_samples(late, early) or 0, 0)

    def test_unrelated_audio_gives_no_answer(self) -> None:
        first = write_shifted_wav(self.tmp / "a.wav", shift=0)
        second = write_shifted_wav(self.tmp / "b.wav", shift=0, seconds=4.0)
        # Rewrite the second file with a completely different (uncorrelated) tone.
        with wave.open(str(second), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            payload = bytearray()
            for i in range(44100 * 4):
                value = int(9000 * math.sin(2 * math.pi * 913.0 * i / 44100))
                payload += value.to_bytes(2, "little", signed=True)
                payload += value.to_bytes(2, "little", signed=True)
            handle.writeframes(bytes(payload))
        self.assertIsNone(estimate_offset_samples(first, second))

    def test_broken_files_return_none(self) -> None:
        good = write_shifted_wav(self.tmp / "good.wav", shift=0)
        bad = self.tmp / "bad.wav"
        bad.write_bytes(b"not a wav")
        self.assertIsNone(estimate_offset_samples(good, bad))
        self.assertIsNone(estimate_offset_samples(self.tmp / "ghost.wav", good))

    def test_needs_alignment_gate(self) -> None:
        a = make_sample(SampleKey.A, write_shifted_wav(self.tmp / "n1.wav", shift=0), 44100)
        same = make_sample(SampleKey.B, a.wav_path, a.frames)
        self.assertFalse(needs_alignment(a, same), "identical lengths need no correlation")
        longer = make_sample(SampleKey.B, a.wav_path, a.frames + 800)
        self.assertTrue(needs_alignment(a, longer))
        empty = make_sample(SampleKey.A, a.wav_path, 0)
        self.assertFalse(needs_alignment(empty, empty))

    def test_constants(self) -> None:
        self.assertGreater(MAX_OFFSET_SEC, 0.1)
        self.assertGreaterEqual(MIN_ALIGN_MS, 1.0)


@require_ffmpeg
class TestSharedRenderLength(unittest.TestCase):
    """Both renders must be frame-identical in length, whatever the sources."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="fb-test-shared-"))
        cls.lossless, cls.lossy = make_pair(cls.tmp)
        cls.ffmpeg = FFmpeg()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        from flacblind.core.tempstore import TempWorkspace

        self.workspace = TempWorkspace(root=self.tmp, suffix="-ws").register()
        self.addCleanup(self.workspace.cleanup)
        self.preparer = AudioPreparer(self.ffmpeg, self.workspace.path)

    def _prepare(self, segment: SegmentSpec, **kwargs) -> object:
        return self.preparer.prepare(
            PrepareRequest(
                sources=(
                    SourceSpec(SampleKey.A, self.lossless, AudioKind.LOSSLESS),
                    SourceSpec(SampleKey.B, self.lossy, AudioKind.LOSSY),
                ),
                segment=segment,
                config=AudioConfig(),
                **kwargs,
            )
        )

    def test_whole_file_both_are_the_same_length(self) -> None:
        pair = self._prepare(SegmentSpec.whole()).pair
        self.assertEqual(pair.sample_a.frames, pair.sample_b.frames)
        self.assertEqual(pair.duration_mismatch_sec, 0.0)

    def test_custom_segment_both_are_the_same_length(self) -> None:
        pair = self._prepare(SegmentSpec.slice_of(1.0, 2.0)).pair
        self.assertEqual(pair.sample_a.frames, pair.sample_b.frames)

    def test_difference_peaks_available_after_alignment(self) -> None:
        pair = self._prepare(SegmentSpec.whole()).pair
        self.assertIsNotNone(pair.difference_peaks())

    def test_alignment_can_be_switched_off(self) -> None:
        result = self._prepare(SegmentSpec.whole(), align=False)
        self.assertIsNotNone(result.pair)
        # Lengths still match: that comes from the shared -t, not the correlator.
        self.assertEqual(result.pair.sample_a.frames, result.pair.sample_b.frames)

    def test_shared_segment_helper(self) -> None:
        probes = {
            SampleKey.A: type("P", (), {"duration_sec": 10.0})(),
            SampleKey.B: type("P", (), {"duration_sec": 10.02})(),
        }
        shared = AudioPreparer._shared_render_segment(SegmentSpec.whole(), probes)
        self.assertAlmostEqual(shared.duration_sec or 0.0, 10.0, places=3)
        self.assertEqual(shared.start_sec, 0.0)
        custom = AudioPreparer._shared_render_segment(
            SegmentSpec.slice_of(2.0, 30.0), probes
        )
        self.assertAlmostEqual(custom.start_sec, 2.0)
        self.assertAlmostEqual(custom.duration_sec or 0.0, 8.0, places=3)


if __name__ == "__main__":
    unittest.main()
