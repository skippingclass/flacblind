"""WAV reading and waveform envelope extraction."""

from __future__ import annotations

import math
import tempfile
import unittest
import wave
from pathlib import Path

from helpers import ROOT, make_tone_wav, write_wav  # noqa: F401

from flacblind.core.errors import UnsupportedAudioError
from flacblind.core.models import Peaks
from flacblind.core.waveform import (
    HAVE_NUMPY,
    compute_level_stats,
    iter_wav_blocks,
    peaks_from_file,
    read_wav_float32,
    read_wav_info,
    silence_ratio,
    synthesize_silence,
)


class TestWavIo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-wave-"))

    def test_header(self) -> None:
        path = make_tone_wav(self.tmp / "tone.wav", seconds=1.0, rate=48000)
        info = read_wav_info(path)
        self.assertEqual(info.channels, 2)
        self.assertEqual(info.sample_rate, 48000)
        self.assertEqual(info.sample_width, 2)
        self.assertEqual(info.frames, 48000)
        self.assertAlmostEqual(info.duration_sec, 1.0)

    def test_mono_mixdown_blocks(self) -> None:
        path = write_wav(self.tmp / "stereo.wav", [1000, -1000] * 1000, channels=2)
        blocks = list(iter_wav_blocks(path))
        self.assertEqual(sum(count for _b, _o, count in blocks), 1000)
        total = sum(sum(block) for block, _o, _c in blocks)
        self.assertAlmostEqual(total, 0.0, places=6)  # (1000 + -1000) / 2

    def test_iterates_in_order(self) -> None:
        path = make_tone_wav(self.tmp / "chunks.wav", seconds=0.5)
        offsets = [offset for _b, offset, _c in iter_wav_blocks(path, block_frames=100)]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(offsets[0], 0)

    def test_invalid_file_raises(self) -> None:
        broken = self.tmp / "broken.wav"
        broken.write_bytes(b"not a wav file")
        with self.assertRaises(UnsupportedAudioError):
            read_wav_info(broken)
        with self.assertRaises(UnsupportedAudioError):
            read_wav_info(self.tmp / "ghost.wav")

    def test_empty_wav_has_no_peaks(self) -> None:
        path = self.tmp / "empty.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"")
        peaks, info = peaks_from_file(path)
        self.assertTrue(peaks.is_empty)
        self.assertEqual(info.frames, 0)


class TestPeaks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-peaks-"))

    def test_extremes_are_exact(self) -> None:
        # Loudest sample of the first bucket, quietest of the last.
        samples = [0] * 4000
        samples[10] = 30000
        samples[-10] = -20000
        path = write_wav(self.tmp / "spikes.wav", samples, channels=1)
        peaks, info = peaks_from_file(path, buckets=100)
        self.assertEqual(len(peaks), 100)
        self.assertAlmostEqual(peaks.maxs[0], 30000 / 32768, places=4)
        self.assertAlmostEqual(peaks.mins[99], -20000 / 32768, places=4)
        self.assertEqual(peaks.total_frames, info.frames)
        self.assertEqual(peaks.sample_rate, info.sample_rate)

    def test_bucket_count_is_clamped_to_frames(self) -> None:
        path = write_wav(self.tmp / "tiny.wav", [0] * 10, channels=1)
        peaks, _ = peaks_from_file(path, buckets=2400)
        self.assertEqual(len(peaks), 10)

    def test_duration_and_bucket_frames(self) -> None:
        path = make_tone_wav(self.tmp / "tone.wav", seconds=2.0)
        peaks, _ = peaks_from_file(path, buckets=200)
        self.assertAlmostEqual(peaks.duration_sec, 2.0, places=3)
        self.assertAlmostEqual(peaks.bucket_frames, 2.0 * 44100 / 200, places=1)

    def test_rms_of_constant_signal(self) -> None:
        amplitude = 0.5
        samples = [int(amplitude * 32767)] * 10000
        path = write_wav(self.tmp / "dc.wav", samples, channels=1)
        peaks, _ = peaks_from_file(path, buckets=10)
        for value in peaks.rms:
            self.assertAlmostEqual(value, amplitude, places=2)

    def test_numpy_and_fallback_paths_agree(self) -> None:
        path = make_tone_wav(self.tmp / "chirp.wav", seconds=1.0)
        blocks = list(iter_wav_blocks(path))
        info = read_wav_info(path)
        from flacblind.core import waveform

        fast = waveform.compute_peaks(blocks, info.frames, info.sample_rate, 300)
        slow = waveform._peaks_python(blocks, info.frames, info.sample_rate, 300)
        for a, b in zip(fast.mins, slow.mins):
            self.assertAlmostEqual(a, b, places=6)
        for a, b in zip(fast.maxs, slow.maxs):
            self.assertAlmostEqual(a, b, places=6)
        for a, b in zip(fast.rms, slow.rms):
            self.assertAlmostEqual(a, b, places=6)

    def test_empty_envelope_operations(self) -> None:
        empty = Peaks((), (), (), 0, 44100)
        self.assertTrue(empty.is_empty)
        self.assertEqual(empty.duration_sec, 0.0)
        self.assertEqual(empty.bucket_frames, 0.0)
        self.assertEqual(len(empty), 0)
        self.assertTrue(empty.difference(empty).is_empty)

    def test_difference_envelope(self) -> None:
        a = Peaks((-0.5, 0.5), (0.5, 0.5), (0.1, 0.1), 100, 44100)
        b = Peaks((-0.25, 0.0), (0.25, 0.0), (0.1, 0.1), 100, 44100)
        diff = a.difference(b)
        self.assertAlmostEqual(diff.mins[0], 0.25)
        self.assertAlmostEqual(diff.maxs[0], 0.25)
        self.assertEqual(len(diff), 2)

    @unittest.skipUnless(HAVE_NUMPY, "numpy is not installed")
    def test_float32_reader_shape(self) -> None:
        path = make_tone_wav(self.tmp / "stereo.wav", seconds=0.25)
        data, rate = read_wav_float32(path)
        self.assertEqual(rate, 44100)
        self.assertEqual(data.shape, (int(0.25 * 44100), 2))
        self.assertLessEqual(float(abs(data).max()), 1.0)
        self.assertGreater(float(abs(data).max()), 0.1)


class TestLevels(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-levels-"))

    def test_level_statistics(self) -> None:
        amplitude = 0.5
        samples = [int(amplitude * 32767) if i % 2 == 0 else -int(amplitude * 32767)
                   for i in range(20000)]
        path = write_wav(self.tmp / "square.wav", samples, channels=1)
        peaks, _ = peaks_from_file(path, buckets=100)
        peak_db, rms_db, crest = compute_level_stats(peaks)
        self.assertAlmostEqual(peak_db, -6.02, delta=0.1)
        self.assertAlmostEqual(rms_db, -6.02, delta=0.2)
        self.assertAlmostEqual(crest, 0.0, delta=0.3)

    def test_silence_detection(self) -> None:
        silent = synthesize_silence(self.tmp / "silence.wav", seconds=1.0)
        peaks, _ = peaks_from_file(silent, buckets=50)
        self.assertGreater(silence_ratio(peaks), 0.9)
        tone = make_tone_wav(self.tmp / "tone.wav", seconds=1.0)
        peaks2, _ = peaks_from_file(tone, buckets=50)
        self.assertLess(silence_ratio(peaks2), 0.1)

    def test_empty_peaks_levels(self) -> None:
        self.assertEqual(compute_level_stats(Peaks((), (), (), 0, 44100)), (-120.0, -120.0, 0.0))


if __name__ == "__main__":
    unittest.main()
