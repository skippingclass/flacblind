"""End-to-end preparation pipeline (integration; needs ffmpeg)."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import HAS_FFMPEG, ROOT, make_pair, make_tone_wav, require_ffmpeg  # noqa: F401

from flacblind.core.errors import PreparationCancelled, PreparationError
from flacblind.core.ffmpeg import FFmpeg
from flacblind.core.models import (
    AudioConfig,
    AudioKind,
    SampleKey,
    SegmentSpec,
)
from flacblind.core.prepare import AudioPreparer, PrepareRequest, ProgressReporter, SourceSpec
from flacblind.core.tempstore import TempWorkspace


class TestProgressReporter(unittest.TestCase):
    def test_monotonic_and_bounded(self) -> None:
        seen: list[tuple[float, str]] = []
        reporter = ProgressReporter(lambda value, label: seen.append((value, label)))
        report = reporter.stage("one", 0.5)
        for i in range(11):
            report(i / 10)
        report2 = reporter.stage("two", 0.5)
        report2(1.0)
        reporter.finish()
        values = [value for value, _label in seen]
        self.assertEqual(values, sorted(values), "progress must never go backwards")
        self.assertAlmostEqual(values[0], 0.0)
        self.assertAlmostEqual(values[-1], 1.0)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in values))

    def test_cancelling_callback_is_optional(self) -> None:
        reporter = ProgressReporter(None)
        reporter.stage("x", 1.0)(0.5)
        reporter.finish()


@require_ffmpeg
class TestPreparePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="fb-test-prepare-"))
        cls.lossless, cls.lossy = make_pair(cls.tmp)
        cls.ffmpeg = FFmpeg()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.workspace = TempWorkspace(root=self.tmp, suffix="-ws").register()
        self.preparer = AudioPreparer(self.ffmpeg, self.workspace.path)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def _request(self, **kwargs) -> PrepareRequest:
        defaults = dict(
            sources=(
                SourceSpec(SampleKey.A, self.lossless, AudioKind.LOSSLESS),
                SourceSpec(SampleKey.B, self.lossy, AudioKind.LOSSY),
            ),
            segment=SegmentSpec.whole(),
            config=AudioConfig(),
        )
        defaults.update(kwargs)
        return PrepareRequest(**defaults)  # type: ignore[arg-type]

    def test_full_pipeline(self) -> None:
        seen: list[tuple[float, str]] = []
        result = self.preparer.prepare(self._request(), progress=lambda v, l: seen.append((v, l)))
        pair = result.pair
        self.assertIsInstance(pair.sample_a.kind, AudioKind)
        self.assertIs(pair.sample_a.kind, AudioKind.LOSSLESS)
        self.assertIs(pair.sample_b.kind, AudioKind.LOSSY)
        for sample in pair:
            self.assertTrue(sample.wav_path.exists())
            self.assertGreater(sample.wav_path.stat().st_size, 1000)
            self.assertEqual(sample.sample_rate, 44100)
            self.assertEqual(sample.channels, 2)
            self.assertGreater(sample.frames, 0)
            self.assertEqual(len(sample.peaks), 2400)
            self.assertIsNotNone(sample.loudness)
        values = [v for v, _ in seen]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(values[-1], 1.0)
        self.assertGreater(result.elapsed_sec, 0.0)
        self.assertEqual(len(result.warnings), 0)

    def test_sources_are_ordered_regardless_of_input_order(self) -> None:
        request = self._request(
            sources=(
                SourceSpec(SampleKey.B, self.lossy, AudioKind.LOSSY),
                SourceSpec(SampleKey.A, self.lossless, AudioKind.LOSSLESS),
            )
        )
        pair = self.preparer.prepare(request).pair
        self.assertIs(pair.sample_a.kind, AudioKind.LOSSLESS)
        self.assertEqual(pair.sample_a.source_path, self.lossless)

    def test_loudness_is_matched(self) -> None:
        """The whole point: both files must end up at the same level."""
        pair = self.preparer.prepare(self._request()).pair
        self.assertLess(pair.loudness_delta_db, 1.0, "loudness matching failed")

    def test_segment_trimming(self) -> None:
        pair = self.preparer.prepare(
            self._request(segment=SegmentSpec.slice_of(1.5, 2.0))
        ).pair
        for sample in pair:
            self.assertAlmostEqual(sample.duration_sec, 2.0, delta=0.15)
        self.assertAlmostEqual(pair.duration_mismatch_sec, 0.0, delta=0.15)

    def test_segment_is_clamped_to_source(self) -> None:
        pair = self.preparer.prepare(
            self._request(segment=SegmentSpec.slice_of(3.0, 600.0))
        ).pair
        for sample in pair:
            self.assertLess(sample.duration_sec, 10.0)

    def test_custom_sample_rate_and_mono(self) -> None:
        pair = self.preparer.prepare(
            self._request(config=AudioConfig(sample_rate=48000, channels=1))
        ).pair
        for sample in pair:
            self.assertEqual(sample.sample_rate, 48000)
            self.assertEqual(sample.channels, 1)

    def test_normalisation_can_be_disabled(self) -> None:
        pair = self.preparer.prepare(
            self._request(config=AudioConfig(normalize=False))
        ).pair
        for sample in pair:
            self.assertIsNone(sample.loudness)

    def test_difference_peaks_available(self) -> None:
        pair = self.preparer.prepare(self._request()).pair
        diff = pair.difference_peaks()
        self.assertIsNotNone(diff)
        assert diff is not None
        self.assertEqual(len(diff), len(pair.sample_a.peaks))
        self.assertGreater(max(diff.maxs), 0.0)

    def test_cancellation(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(PreparationCancelled):
            self.preparer.prepare(self._request(), cancel=cancel)

    def test_same_file_twice_is_rejected(self) -> None:
        request = self._request(
            sources=(
                SourceSpec(SampleKey.A, self.lossless, AudioKind.LOSSLESS),
                SourceSpec(SampleKey.B, self.lossless, AudioKind.LOSSY),
            )
        )
        with self.assertRaises(PreparationError) as ctx:
            self.preparer.prepare(request)
        self.assertIn("different", str(ctx.exception))

    def test_missing_file_is_rejected(self) -> None:
        request = self._request(
            sources=(
                SourceSpec(SampleKey.A, self.tmp / "ghost.flac", AudioKind.LOSSLESS),
                SourceSpec(SampleKey.B, self.lossy, AudioKind.LOSSY),
            )
        )
        with self.assertRaises(Exception) as ctx:
            self.preparer.prepare(request)
        self.assertIn("not found", str(ctx.exception).lower())

    def test_tiny_file_is_rejected(self) -> None:
        tiny = make_tone_wav(self.tmp / "tiny.wav", seconds=0.2)
        request = self._request(
            sources=(
                SourceSpec(SampleKey.A, tiny, AudioKind.LOSSLESS),
                SourceSpec(SampleKey.B, self.lossy, AudioKind.LOSSY),
            )
        )
        with self.assertRaises(PreparationError) as ctx:
            self.preparer.prepare(request)
        self.assertIn("short", str(ctx.exception))

    def test_two_lossy_files_warn(self) -> None:
        request = self._request(
            sources=(
                SourceSpec(SampleKey.A, self.lossy, None),
                SourceSpec(SampleKey.B, self.lossy, None),
            )
        )
        with self.assertRaises(PreparationError):
            self.preparer.prepare(request)

    def test_level_statistics_are_sane(self) -> None:
        sample = self.preparer.prepare(self._request()).pair.sample_a
        self.assertLessEqual(sample.stats.peak_dbfs, 0.5)
        self.assertLess(sample.stats.rms_dbfs, sample.stats.peak_dbfs)
        self.assertGreaterEqual(sample.stats.crest_factor_db, 0.0)
        self.assertFalse(sample.stats.mostly_silent)
        self.assertIn("dBFS", sample.stats.summary)


if __name__ == "__main__":
    unittest.main()
