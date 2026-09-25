"""The audio preparation pipeline: probe → normalise → trim → analyse.

This module is deliberately Qt-free and synchronous: the GUI runs it inside a
:class:`~flacblind.ui.workers.PrepareWorker` thread, and the test-suite runs it
directly.  Progress is reported through a callback so both callers can wire it
to whatever they like.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .align import MIN_ALIGN_MS, estimate_offset_samples
from .errors import PreparationCancelled, PreparationError
from .ffmpeg import FFmpeg
from .models import (
    AudioConfig,
    AudioKind,
    PreparedPair,
    PreparedSample,
    ProbeInfo,
    SampleKey,
    SampleStats,
    SegmentSpec,
    guess_kind,
)
from .waveform import (
    DEFAULT_BUCKETS,
    compute_level_stats,
    iter_wav_blocks,
    peaks_from_file,
    silence_ratio,
)

__all__ = ["PrepareRequest", "AudioPreparer", "PrepareResult", "ProgressReporter"]

ProgressCallback = Callable[[float, str], None]
LogCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One input file and the role it was dropped into."""

    key: SampleKey
    path: Path
    kind: AudioKind | None = None  # None = detect from the codec

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class PrepareRequest:
    """Everything the preparer needs for one test session."""

    sources: tuple[SourceSpec, SourceSpec]
    segment: SegmentSpec = SegmentSpec.whole()
    config: AudioConfig = AudioConfig()
    buckets: int = DEFAULT_BUCKETS
    #: Re-render the later file so both samples start on the same sample.
    align: bool = True


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """Success payload: the pair plus warnings worth surfacing in the UI."""

    pair: PreparedPair
    probes: tuple[ProbeInfo, ProbeInfo]
    elapsed_sec: float = 0.0

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.pair.warnings


class ProgressReporter:
    """Weighted aggregation of the pipeline's stages.

    Each stage announces how much of the total bar it owns, then reports its
    own local ``0..1`` fraction.  The bar therefore advances monotonically
    instead of jumping back to 0 % whenever the next stage starts.
    """

    def __init__(self, callback: ProgressCallback | None, total: float = 1.0) -> None:
        self._callback = callback
        self._total = total
        self._base = 0.0
        self._span = 0.0
        self._last = -1.0

    def stage(self, label: str, span: float) -> Callable[[float], None]:
        """Start a stage; the returned callback reports local progress."""
        self._base = min(self._total, self._base + self._span)
        self._span = max(0.0, span)
        self._emit(self._base, label)

        def _report(fraction: float) -> None:
            self._emit(self._base + self._span * min(1.0, max(0.0, fraction)), label)

        return _report

    def _emit(self, value: float, label: str) -> None:
        value = min(self._total, value)
        if self._callback is not None and value - self._last >= 0.002:
            self._last = value
            self._callback(value, label)

    def finish(self) -> None:
        if self._callback is not None and self._last < self._total:
            self._last = self._total
            self._callback(self._total, "done")


class AudioPreparer:
    """Turn two arbitrary audio files into a fair, ready-to-test pair."""

    def __init__(
        self,
        ffmpeg: FFmpeg,
        output_dir: str | Path,
        *,
        on_log: LogCallback | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._on_log = on_log

    # ------------------------------------------------------------------ main
    def prepare(
        self,
        request: PrepareRequest,
        *,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> PrepareResult:
        import time

        started = time.monotonic()
        self.ffmpeg.ensure_available()
        # Normalise ordering so the result is always (A, B) regardless of the
        # order in which the user dropped the files.
        sources = tuple(sorted(request.sources, key=lambda spec: spec.key.value))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        reporter = ProgressReporter(progress)

        # -- 1. probe both inputs ------------------------------------------------
        report = reporter.stage("Reading file headers", 0.06)
        report(0.0)
        probes: dict[SampleKey, ProbeInfo] = {}
        for index, source in enumerate(sources):
            _check_cancel(cancel)
            probes[source.key] = self.ffmpeg.probe(source.path)
            report((index + 1) / len(request.sources))
        self._validate_pair(sources, probes)

        # -- 2. work out the shared segment -------------------------------------
        base = probes[sources[0].key].duration_sec
        segment = request.segment.clamp(base)
        if not segment.whole_file and (segment.duration_sec or 0.0) < 1.0:
            raise PreparationError(
                "The selected segment is shorter than one second.",
                hint="Pick at least a second, or test the whole file.",
            )

        # -- 2b. one length for both renders ------------------------------------
        # An MP3 decodes to a slightly longer stream than the FLAC (encoder
        # padding, ~800 samples here).  Rendering both with the same explicit
        # ``-t`` makes the two samples frame-identical in length, which is what
        # the A/B overlay and the difference view need.
        render_segment = self._shared_render_segment(segment, probes)

        # -- 3. measure + render ------------------------------------------------
        prepared: list[PreparedSample] = []
        measure_span = 0.13 if request.config.normalize else 0.0
        render_span = 0.30
        analysis_span = 0.06
        align_span = 0.08 if request.align else 0.0

        for source in sources:
            _check_cancel(cancel)
            probe = probes[source.key]
            # Identical arguments for both files: same start, same length.
            local_segment = render_segment
            out_duration = local_segment.duration_sec or probe.duration_sec

            measurement = None
            measurement_error = ""
            if request.config.normalize:
                report = reporter.stage(
                    f"Measuring loudness · {source.name}", measure_span
                )
                try:
                    measurement = self.ffmpeg.measure_loudness(
                        probe.path,
                        request.config,
                        cancel=cancel,
                        progress=report,
                    )
                except PreparationCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - never abort for this
                    measurement_error = str(exc)
                    if self._on_log is not None:
                        self._on_log(f"loudness measurement failed for {source.name}: {exc}")

            _check_cancel(cancel)
            report = reporter.stage(f"Rendering {source.name}", render_span)
            wav_path = self.output_dir / f"{source.key.value.lower()}.wav"
            self.ffmpeg.render(
                probe.path,
                wav_path,
                request.config,
                local_segment,
                measurement,
                total_duration=out_duration,
                progress=report,
                cancel=cancel,
            )

            _check_cancel(cancel)
            report = reporter.stage(f"Analysing {source.name}", analysis_span)
            sample = self._analyse(
                source=source,
                probe=probe,
                wav_path=wav_path,
                measurement=measurement,
                measurement_error=measurement_error,
                buckets=request.buckets,
                cancel=cancel,
            )
            report(1.0)
            prepared.append(sample)

        # -- 4. sample alignment ------------------------------------------------
        sample_a, sample_b = prepared
        alignment_note = ""
        if request.align:
            report = reporter.stage("Aligning the two samples", align_span)
            _check_cancel(cancel)
            sample_a, sample_b, alignment_note = self._align(
                sample_a,
                sample_b,
                request,
                probes,
                segment,
                cancel=cancel,
            )
            report(1.0)

        reporter.finish()
        pair = PreparedPair(
            sample_a=sample_a,
            sample_b=sample_b,
            segment=segment,
            warnings=self._warnings(request, (sample_a, sample_b), probes, alignment_note),
        )
        return PrepareResult(
            pair=pair,
            probes=(probes[sources[0].key], probes[sources[1].key]),
            elapsed_sec=time.monotonic() - started,
        )

    @staticmethod
    def _shared_render_segment(
        segment: SegmentSpec,
        probes: dict[SampleKey, ProbeInfo],
    ) -> SegmentSpec:
        """The segment both files are rendered with (same start *and* length)."""
        available = min(probe.duration_sec for probe in probes.values()) - segment.start_sec
        available = max(0.5, available)
        requested = segment.requested_duration
        length = min(requested, available) if requested else available
        return SegmentSpec.slice_of(segment.start_sec, max(0.5, length))

    # ------------------------------------------------------------------ align
    def _align(
        self,
        sample_a: PreparedSample,
        sample_b: PreparedSample,
        request: PrepareRequest,
        probes: dict[SampleKey, ProbeInfo],
        segment: SegmentSpec,
        *,
        cancel: threading.Event | None,
    ) -> tuple[PreparedSample, PreparedSample, str]:
        """Line the two renders up sample by sample.

        An MP3 usually starts a few milliseconds late (encoder priming), which
        would make the A/B comparison unfair and shift the waveform overlay.
        When the offset is measurable, the later file is re-rendered from
        ``start + offset`` with the exact length of the earlier one.
        """
        if sample_a.frames == sample_b.frames and not request.align:
            return sample_a, sample_b, ""
        offset_ms = estimate_offset_samples(sample_a.wav_path, sample_b.wav_path)
        if offset_ms is None:
            detail = ""
            if sample_a.frames != sample_b.frames:
                detail = (
                    "The two renders still differ in length and could not be aligned "
                    "automatically (install numpy for sample alignment)."
                )
            return sample_a, sample_b, detail
        offset_sec = offset_ms / max(1, sample_a.sample_rate)
        if abs(offset_sec) * 1000 < MIN_ALIGN_MS:
            return sample_a, sample_b, ""

        # B is late by `offset` samples -> start reading it earlier.
        late_key, early_key = (SampleKey.B, SampleKey.A)
        if offset_sec < 0:
            late_key, early_key = early_key, late_key
        early = sample_a if early_key is SampleKey.A else sample_b
        late = sample_a if late_key is SampleKey.A else sample_b
        seconds = abs(offset_sec)
        probe = probes[late_key]
        # Same arguments as the first pass, plus the measured offset and an
        # exact output length so both files end up frame-identical in length.
        # Re-render the late file from `start + lag` with the early file's exact
        # length, so both are aligned *and* identical in length.
        length = SegmentSpec.slice_of(
            max(0.0, segment.start_sec + seconds),
            max(0.5, early.duration_sec),
        )
        self.ffmpeg.render(
            probe.path,
            late.wav_path,
            request.config,
            length,
            late.loudness,
            total_duration=length.duration_sec or early.duration_sec,
            cancel=cancel,
        )
        reanalysed = self._analyse(
            source=SourceSpec(late_key, probe.path, late.kind),
            probe=probe,
            wav_path=late.wav_path,
            measurement=late.loudness,
            measurement_error=late.measurement_error,
            buckets=request.buckets,
            cancel=cancel,
        )
        pair = {SampleKey.A: sample_a, SampleKey.B: sample_b}
        pair[late_key] = reanalysed
        return pair[SampleKey.A], pair[SampleKey.B], (
            f"Aligned the two samples by {seconds * 1000:.0f} ms "
            f"({late_key.value} started late — typical MP3 encoder padding)."
        )

    # ------------------------------------------------------------- internals
    def _analyse(
        self,
        *,
        source: SourceSpec,
        probe: ProbeInfo,
        wav_path: Path,
        measurement,
        measurement_error: str,
        buckets: int,
        cancel: threading.Event | None,
    ) -> PreparedSample:
        blocks = list(iter_wav_blocks(wav_path))
        _check_cancel(cancel)
        peaks, info = peaks_from_file(wav_path, buckets, blocks=blocks)
        peak_dbfs, rms_dbfs, crest = compute_level_stats(peaks)
        kind = source.kind or guess_kind(probe.fmt)
        return PreparedSample(
            key=source.key,
            kind=kind,
            fmt=probe.fmt,
            source_path=source.path,
            wav_path=wav_path,
            duration_sec=info.duration_sec,
            sample_rate=info.sample_rate,
            channels=info.channels,
            frames=info.frames,
            peaks=peaks,
            stats=SampleStats(
                peak_dbfs=peak_dbfs,
                rms_dbfs=rms_dbfs,
                crest_factor_db=crest,
                mostly_silent=silence_ratio(peaks) > 0.5,
            ),
            loudness=measurement,
            probe=probe,
            measurement_error=measurement_error,
        )

    @staticmethod
    def _validate_pair(sources: Sequence[SourceSpec], probes: dict[SampleKey, ProbeInfo]) -> None:
        paths = [spec.path for spec in sources]
        if len(set(paths)) < 2:
            raise PreparationError(
                "Please choose two different files.",
                hint="An ABX test needs a lossless and a lossy version of the same track.",
            )
        for spec in sources:
            probe = probes[spec.key]
            if probe.duration_sec < 1.0:
                raise PreparationError(
                    f"{spec.name} is too short to test ({probe.duration_sec:.2f} s).",
                    hint="Use a track of at least a couple of seconds.",
                )

    @staticmethod
    def _warnings(
        request: PrepareRequest,
        samples: Sequence[PreparedSample],
        probes: dict[SampleKey, ProbeInfo],
        alignment_note: str = "",
    ) -> tuple[str, ...]:
        sample_a, sample_b = samples
        notes: list[str] = []
        if alignment_note:
            notes.append(alignment_note)
        duration_delta = abs(probes[sample_a.key].duration_sec - probes[sample_b.key].duration_sec)
        if duration_delta > 0.75:
            notes.append(
                f"The two files differ in length by {duration_delta:.1f} s — they may be "
                "different edits or masters, which makes the comparison unfair."
            )
        if probes[sample_a.key].fmt.is_lossless and probes[sample_b.key].fmt.is_lossless:
            notes.append("Both files look lossless — there may be nothing to detect here.")
        if not probes[sample_a.key].fmt.is_lossless and not probes[sample_b.key].fmt.is_lossless:
            notes.append("Both files look lossy — the test is still valid, but no source is lossless.")
        if sample_a.kind is sample_b.kind and request.sources[0].kind is None:
            notes.append("Both files were classified as the same kind of audio.")
        for sample in (sample_a, sample_b):
            if sample.stats.mostly_silent:
                notes.append(f"{sample.key.value} is mostly silence — pick a busier segment.")
            if sample.measurement_error:
                notes.append(
                    f"Loudness matching failed for {sample.key.value} "
                    f"({sample.measurement_error.splitlines()[0]}); volumes may differ."
                )
        return tuple(notes)


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise PreparationCancelled()
