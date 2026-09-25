"""ffmpeg layer: pure command builders, log parsers, and the real pipeline."""

from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import HAS_FFMPEG, ROOT, make_pair, make_tone_wav, require_ffmpeg

from flacblind.core.errors import DependencyError, MediaError, PreparationCancelled
from flacblind.core.ffmpeg import (
    FFmpeg,
    build_measure_cmd,
    build_probe_cmd,
    build_render_cmd,
    format_from_codec,
    parse_bitrate,
    parse_duration,
    parse_loudnorm_json,
    parse_progress,
)
from flacblind.core.models import (
    AudioConfig,
    AudioKind,
    AudioFormat,
    LoudnessMeasurement,
    SampleKey,
    SegmentSpec,
    build_filter,
)

LOUDNORM_JSON = """
[Parsed_loudnorm_0 @ 0x55] 
Input Integrated:    -14.31 LUFS
Input True Peak:      -1.20 dBTP
Input LRA:             6.40 LU
Input Threshold:     -25.10 LUFS

Output Integrated:   -16.00 LUFS
Output True Peak:     -2.90 dBTP
Output LRA:             5.20 LU
Output Threshold:     -26.50 LUFS
Normalization Type:   Linear
Target Offset:       +0.05 LU

{
	"input_i" : "-14.31",
	"input_tp" : "-1.20",
	"input_lra" : "6.40",
	"input_thresh" : "-25.10",
	"output_i" : "-16.00",
	"target_offset" : "0.05"
}
"""


class TestParsers(unittest.TestCase):
    def test_parse_duration(self) -> None:
        text = "  Duration: 00:03:21.45, start: 0.000057, bitrate: 1411 kb/s"
        self.assertAlmostEqual(parse_duration(text), 201.45, places=3)
        self.assertAlmostEqual(parse_duration("Duration: 01:00:00.00"), 3600.0)
        self.assertEqual(parse_duration("no duration here"), 0.0)
        self.assertAlmostEqual(parse_duration("Duration: 00:00:09.999999"), 10.0, places=5)

    def test_parse_bitrate(self) -> None:
        self.assertEqual(parse_bitrate("bitrate: 1411 kb/s"), 1411)
        self.assertEqual(parse_bitrate("nope"), 0)

    def test_parse_loudnorm_json(self) -> None:
        values = parse_loudnorm_json(LOUDNORM_JSON)
        self.assertAlmostEqual(values["input_i"], -14.31)
        self.assertAlmostEqual(values["input_tp"], -1.20)
        self.assertAlmostEqual(values["input_lra"], 6.40)
        self.assertAlmostEqual(values["input_thresh"], -25.10)
        self.assertAlmostEqual(values["target_offset"], 0.05)

    def test_parse_loudnorm_json_failure_is_explained(self) -> None:
        with self.assertRaises(MediaError) as ctx:
            parse_loudnorm_json("nothing useful here")
        self.assertTrue(ctx.exception.hint)

    def test_parse_progress(self) -> None:
        self.assertAlmostEqual(parse_progress("out_time_us=1500000"), 1.5)
        self.assertAlmostEqual(parse_progress("out_time_ms=2500000"), 2.5)  # µs, per ffmpeg
        self.assertAlmostEqual(parse_progress("out_time=00:00:12.500000"), 12.5)
        self.assertAlmostEqual(parse_progress("out_time=01:02:03.000000"), 3723.0)
        self.assertIsNone(parse_progress("out_time=N/A"))
        self.assertIsNone(parse_progress("bitrate=320kbits/s"))
        self.assertIsNone(parse_progress("out_time_us=notanumber"))

    def test_format_from_codec(self) -> None:
        self.assertIs(format_from_codec("flac"), AudioFormat.FLAC)
        self.assertIs(format_from_codec("mp3"), AudioFormat.MP3)
        self.assertIs(format_from_codec("libmp3lame"), AudioFormat.OTHER)
        self.assertIs(format_from_codec("", Path("song.opus")), AudioFormat.OPUS)
        self.assertIs(format_from_codec("", Path("song.xyz")), AudioFormat.OTHER)

    def test_format_lossless_classification(self) -> None:
        for fmt in (AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.ALAC, AudioFormat.WAVPACK):
            self.assertTrue(fmt.is_lossless, fmt)
        for fmt in (AudioFormat.MP3, AudioFormat.AAC, AudioFormat.OGG, AudioFormat.OPUS):
            self.assertFalse(fmt.is_lossless, fmt)


class TestCommandBuilders(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = AudioConfig(target_lufs=-16.0, true_peak_db=-1.5, lra=11.0, sample_rate=48000)
        self.segment = SegmentSpec.slice_of(12.5, 30.0)

    def test_probe_command(self) -> None:
        cmd = build_probe_cmd("/usr/bin/ffprobe", Path("/tmp/a.flac"))
        self.assertEqual(cmd[0], "/usr/bin/ffprobe")
        self.assertIn("-show_streams", cmd)
        self.assertEqual(cmd[-1], "/tmp/a.flac")

    def test_measure_command_uses_json(self) -> None:
        cmd = build_measure_cmd("ffmpeg", Path("in.mp3"), self.cfg)
        self.assertIn("-af", cmd)
        self.assertIn("loudnorm", cmd[cmd.index("-af") + 1])
        self.assertIn("print_format=json", cmd[cmd.index("-af") + 1])
        self.assertEqual(cmd[-3:], ["-f", "null", "-"])  # the audio is thrown away

    def test_render_command_seeks_before_input(self) -> None:
        cmd = build_render_cmd("ffmpeg", Path("in.flac"), Path("out.wav"), self.cfg, self.segment)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertLess(cmd.index("-t"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-ss") + 1], "12.500")
        self.assertEqual(cmd[cmd.index("-t") + 1], "30.000")
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "pcm_s16le")
        self.assertEqual(cmd[-1], "out.wav")

    def test_render_command_whole_file_has_no_seek(self) -> None:
        cmd = build_render_cmd("ffmpeg", Path("in.flac"), Path("out.wav"), self.cfg, SegmentSpec.whole())
        self.assertNotIn("-ss", cmd)
        self.assertNotIn("-t", cmd)

    def test_render_applies_measured_loudness(self) -> None:
        measurement = LoudnessMeasurement(-14.31, -1.2, 6.4, -25.1, 0.05)
        cmd = build_render_cmd(
            "ffmpeg", Path("in.flac"), Path("out.wav"), self.cfg, self.segment, measurement
        )
        filt = cmd[cmd.index("-af") + 1]
        self.assertIn("measured_I=-14.31", filt)
        self.assertIn("measured_TP=-1.20", filt)
        self.assertIn("linear=true", filt)

    def test_render_without_normalisation_uses_anull(self) -> None:
        cfg = AudioConfig(normalize=False)
        cmd = build_render_cmd("ffmpeg", Path("a"), Path("b.wav"), cfg, SegmentSpec.whole())
        self.assertEqual(cmd[cmd.index("-af") + 1], "anull")

    def test_render_mono_downmix(self) -> None:
        cfg = AudioConfig(channels=1)
        cmd = build_render_cmd("ffmpeg", Path("a"), Path("b.wav"), cfg, SegmentSpec.whole())
        self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
        self.assertIn("channel_layouts=mono", cmd[cmd.index("-af") + 1])

    def test_both_filter_syntaxes(self) -> None:
        self.assertEqual(build_filter("loudnorm", (("I", -16), ("TP", -1.5)), ":"), "loudnorm:I=-16:TP=-1.5")
        self.assertEqual(build_filter("loudnorm", (("I", -16), ("TP", -1.5)), "="), "loudnorm=I=-16:TP=-1.5")
        self.assertEqual(build_filter("anull", ()), "anull")


@require_ffmpeg
class TestFFmpegRuntime(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="fb-test-ffmpeg-"))
        cls.lossless, cls.lossy = make_pair(cls.tmp)
        cls.ffmpeg = FFmpeg()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_available_and_sep_probe(self) -> None:
        self.assertTrue(self.ffmpeg.available)
        self.assertIn(self.ffmpeg.filter_sep, (":", "="))
        # The probed separator must produce a filter this build accepts.
        self.assertTrue(
            self.ffmpeg._probe_filter_sep(self.ffmpeg.filter_sep),
            "probed separator does not work",
        )

    def test_probe_reports_metadata(self) -> None:
        info = self.ffmpeg.probe(self.lossless)
        self.assertGreater(info.duration_sec, 5.0)
        self.assertEqual(info.sample_rate, 44100)
        self.assertEqual(info.channels, 2)
        self.assertIs(info.fmt, AudioFormat.WAV)

    def test_probe_uses_ffprobe(self) -> None:
        self.assertTrue(self.ffmpeg.has_ffprobe)
        info = self.ffmpeg.probe(self.lossy)
        self.assertIs(info.fmt, AudioFormat.MP3)
        self.assertGreater(info.bitrate_kbps, 0)
        self.assertIn("kHz", info.detail_line)

    def test_probe_errors_are_explained(self) -> None:
        with self.assertRaises(MediaError):
            self.ffmpeg.probe(self.tmp / "missing.flac")
        empty = self.tmp / "empty.flac"
        empty.write_bytes(b"")
        with self.assertRaises(MediaError) as ctx:
            self.ffmpeg.probe(empty)
        self.assertIn("empty", str(ctx.exception).lower())
        broken = self.tmp / "broken.flac"
        broken.write_bytes(b"fLaC not really a flac file at all" * 8)
        with self.assertRaises(MediaError):
            self.ffmpeg.probe(broken)

    def test_probe_directory_is_rejected(self) -> None:
        with self.assertRaises(MediaError):
            self.ffmpeg.probe(self.tmp)

    def test_measure_loudness(self) -> None:
        measurement = self.ffmpeg.measure_loudness(self.lossless, AudioConfig())
        self.assertLess(measurement.input_i, 0.0)
        self.assertLessEqual(measurement.input_tp, 0.0)
        self.assertIsInstance(measurement, LoudnessMeasurement)

    def test_render_and_progress(self) -> None:
        destination = self.tmp / "rendered.wav"
        measurement = self.ffmpeg.measure_loudness(self.lossless, AudioConfig())
        seen: list[float] = []
        self.ffmpeg.render(
            self.lossless,
            destination,
            AudioConfig(),
            SegmentSpec.slice_of(1.0, 2.0),
            measurement,
            total_duration=2.0,
            progress=seen.append,
        )
        self.assertTrue(destination.exists())
        self.assertGreater(destination.stat().st_size, 1000)
        self.assertTrue(seen, "no progress was reported")
        self.assertAlmostEqual(seen[-1], 1.0, delta=0.05)

    def test_render_can_be_cancelled(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(PreparationCancelled):
            self.ffmpeg.render(
                self.lossless,
                self.tmp / "cancelled.wav",
                AudioConfig(),
                SegmentSpec.whole(),
                None,
                cancel=cancel,
            )

    def test_missing_binary_raises_dependency_error(self) -> None:
        broken = FFmpeg(ffmpeg="/nonexistent/ffmpeg", ffprobe="")
        self.assertFalse(broken.available)
        with self.assertRaises(DependencyError) as ctx:
            broken.ensure_available()
        self.assertIn("apt install", ctx.exception.hint)
        with self.assertRaises(DependencyError):
            broken.measure_loudness(self.lossless, AudioConfig())


if __name__ == "__main__":
    unittest.main()
