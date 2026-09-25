"""Session controller — the "presenter" of the MVP split.

It owns every piece of state the game needs (settings, ffmpeg handle, temp
workspace, the :class:`~flacblind.core.abx.ABXSession` and the player) and
talks to the view purely through signals.  The window is then a dumb renderer:
it wires widgets to controller signals and forwards user intents back.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from .. import __version__
from ..core.abx import ABXSession, TestConfig
from ..core.errors import SessionError
from ..core.ffmpeg import INSTALL_HINT, FFmpeg
from ..core.models import (
    AudioKind,
    PreparedPair,
    RoundRecord,
    SampleKey,
    SessionReport,
)
from ..core.prepare import PrepareRequest, SourceSpec
from ..core.settings import AppSettings
from ..core.stats import fmt_p_value, verdict
from ..core.tempstore import TempWorkspace, sweep_stale_workspaces
from .player import Player
from .workers import PrepareWorker

__all__ = ["SessionController", "AppState", "export_report", "format_summary"]


class AppState:
    """Coarse application state, mirrored by the window's enabled/disabled sets."""

    IDLE = "idle"            # no usable pair yet
    PREPARING = "preparing"  # ffmpeg running
    READY = "ready"          # pair loaded, no round voted
    ROUND = "round"          # round in progress
    VOTED = "voted"          # waiting for "next"
    FINISHED = "finished"


class SessionController:
    """Glue between the core model and the widgets.

    Deliberately *not* a QObject: the window owns it and re-emits its signals,
    which keeps the controller testable and free of widget references.
    """

    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings()
        self.ffmpeg = FFmpeg()
        self.player = Player(self.settings.backend)
        self.workspace: TempWorkspace | None = None
        self.session: ABXSession | None = None
        self.pair: PreparedPair | None = None
        self.state: str = AppState.IDLE
        self.worker: PrepareWorker | None = None
        self._preparing = False
        self.last_report: SessionReport | None = None
        self.last_error: tuple[str, str] = ("", "")

    # ------------------------------------------------------------------ setup
    def sweep_temporaries(self) -> list[Path]:
        """Delete temp folders left behind by a crashed run."""
        try:
            return sweep_stale_workspaces()
        except OSError:  # pragma: no cover - defensive
            return []

    def ensure_workspace(self) -> TempWorkspace:
        if self.workspace is None or self.workspace.cleaned or not self.workspace.path.exists():
            self.workspace = TempWorkspace().register()
        return self.workspace

    def test_config(self) -> TestConfig:
        s = self.settings
        return TestConfig(
            rounds=s.rounds,
            seed=s.seed,
            reveal_after_vote=s.reveal_after_vote,
            require_both_heard=s.require_both_heard,
            min_plays=s.min_plays,
        )

    # -------------------------------------------------------------- sources
    def set_source(self, key: SampleKey, path: str | os.PathLike[str] | None) -> None:
        if key is SampleKey.A:
            self.settings.lossless_path = str(path or "")
        else:
            self.settings.lossy_path = str(path or "")
        if path:
            self.settings.remember(str(path))
        self.pair = None
        self.set_state(AppState.IDLE)

    def swap_sources(self) -> None:
        self.settings.lossless_path, self.settings.lossy_path = (
            self.settings.lossy_path,
            self.settings.lossless_path,
        )

    def sources_ready(self) -> bool:
        lossless = self.settings.lossless_path
        lossy = self.settings.lossy_path
        if not lossless or not lossy:
            return False
        return Path(lossless) != Path(lossy)

    # ------------------------------------------------------------- preparing
    def start_preparation(self) -> tuple[bool, str]:
        """Kick off a :class:`PrepareWorker`; returns ``(started, message)``."""
        if self._preparing:
            # QThread.isRunning() is still False in the same turn start() was
            # called, so the explicit flag is what actually guards this.
            return False, "already running"
        if not self.ffmpeg.available:
            return False, INSTALL_HINT
        if not self.sources_ready():
            return False, "sources"

        workspace = self.ensure_workspace()
        request = PrepareRequest(
            sources=(
                SourceSpec(SampleKey.A, Path(self.settings.lossless_path), AudioKind.LOSSLESS),
                SourceSpec(SampleKey.B, Path(self.settings.lossy_path), AudioKind.LOSSY),
            ),
            segment=self.settings.segment(),
            config=self.settings.audio_config(),
        )
        self.worker = PrepareWorker(self.ffmpeg, request, workspace)
        # A plain flag rather than QThread.isRunning(): the latter is still
        # False in the same event-loop turn in which start() was called, which
        # would leave the UI briefly in a "nothing is happening" state.
        self._preparing = True
        self.set_state(AppState.PREPARING)
        return True, ""

    def cancel_preparation(self) -> None:
        if self.worker is not None and self._preparing:
            self.worker.cancel()

    @property
    def preparing(self) -> bool:
        return self._preparing

    def on_prepared(self, result: object) -> None:
        self._preparing = False
        pair = getattr(result, "pair", result)
        self.pair = pair
        self.player.load_pair(pair)
        self.start_session()
        self.set_state(AppState.ROUND if not self.session_finished() else AppState.FINISHED)

    def on_prepare_failed(self, message: str, hint: str = "") -> None:
        """Worker failure: remember it and drop back to the idle state.

        ``message``/``hint`` match the worker's ``failed`` signal; the window
        shows them, the controller only tracks the state.
        """
        self.last_error = (message, hint)
        self._preparing = False
        self.set_state(AppState.IDLE)

    def on_prepare_cancelled(self) -> None:
        self._preparing = False
        self.set_state(AppState.IDLE if self.pair is None else AppState.ROUND)

    # -------------------------------------------------------------- session
    def start_session(self) -> None:
        self.session = ABXSession(self.test_config())
        self.session.start()
        self.last_report = None

    def new_session(self) -> None:
        """Restart the game with the current settings (keeps the audio)."""
        if self.pair is None:
            self.set_state(AppState.IDLE)
            return
        self.player.stop()
        self.start_session()
        self.set_state(AppState.ROUND)

    def session_finished(self) -> bool:
        return bool(self.session and self.session.finished)

    def can_vote(self) -> tuple[bool, str]:
        if self.session is None:
            return False, "no-round"
        return self.session.can_vote()

    def vote(self, guess: SampleKey) -> RoundRecord:
        if self.session is None:
            raise SessionError("No session is running.")
        record = self.session.vote(guess)
        self.player.pause()
        self.set_state(AppState.FINISHED if self.session_finished() else AppState.VOTED)
        return record

    def next_round(self) -> None:
        if self.session is None:
            return
        if self.session_finished():
            self.finish()
            return
        try:
            self.session.next_round()
        except SessionError:
            self.finish()
            return
        self.player.seek_ms(0)
        self.set_state(AppState.ROUND)

    def finish(self) -> None:
        if self.session is None:
            return
        if self.session.config.endless:
            self.session.end_early()
        self.player.pause()
        self.last_report = self.build_report()
        self.set_state(AppState.FINISHED)

    def skip_round(self) -> None:
        if self.session is None:
            return
        if self.session.skip_round() is None and self.session.current is not None:
            self.set_state(AppState.ROUND)

    def build_report(self) -> SessionReport | None:
        if self.session is None:
            return None
        segment = self.settings.segment()
        source_duration = max(
            (s.duration_sec for s in (self.pair or ())), default=segment.start_sec
        )
        rate = self.settings.audio_config().sample_rate
        return self.session.report(
            segment=segment.describe(max(source_duration, segment.start_sec + 1)),
            sample_rate=rate,
        )

    def stats(self):  # noqa: ANN201 - SessionStats
        from ..core.models import SessionStats

        if self.session is None:
            return SessionStats(0, 0, 1.0, 1.0, 0.0, 1.0, 1)
        return self.session.stats()

    def records(self) -> tuple[RoundRecord, ...]:
        return self.session.records if self.session else ()

    def verdict(self):  # noqa: ANN201 - stats.Verdict
        n = len(self.records())
        k = sum(1 for record in self.records() if record.correct)
        return verdict(n, k)

    # ------------------------------------------------------------- playback
    def play(self, key: SampleKey) -> None:
        if self.pair is None:
            return
        if self.session is not None:
            self.session.mark_played(key)
        self.player.play(key)

    def toggle(self) -> None:
        if self.pair is None:
            return
        key = self.player.active or SampleKey.A
        if self.player.is_playing:
            self.player.pause()
        else:
            self.play(key)

    def stop_playback(self) -> None:
        self.player.stop()

    # -------------------------------------------------------------- state
    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state

    # ------------------------------------------------------------- shutdown
    def shutdown(self) -> None:
        """Stop everything and clean the temp directory if requested."""
        if self.worker is not None and self._preparing:
            self.worker.cancel()
            self.worker.wait(3000)
        self.player.shutdown()
        if self.workspace is not None:
            if self.settings.delete_temp_on_exit:
                self.workspace.cleanup()
            else:  # pragma: no cover - only when the user opts out
                self.workspace.keep()


# --------------------------------------------------------------------------- #
#  Export helpers
# --------------------------------------------------------------------------- #
def export_report(
    path: str | os.PathLike[str],
    report: SessionReport,
    *,
    fmt: str = "json",
) -> Path:
    """Write a session report as JSON or CSV; returns the written path."""
    target = Path(path)
    if fmt == "csv":
        lines = ["round,guess,truth,correct,listened_sec,switched"]
        for record in report.records:
            lines.append(
                f"{record.index},{record.guess.value},{record.lossless_key.value},"
                f"{int(record.correct)},{record.played_sec},{int(record.switched)}"
            )
        summary = report.to_dict()
        lines.append("")
        lines.append(f"# {summary['accuracy']} correct, p={summary['p_value_one_sided']}")
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        payload = report.to_dict()
        payload["version"] = __version__
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def format_summary(report: SessionReport) -> str:
    """One-line, copy-pasteable summary of a finished session."""
    stats = report.stats
    reading = verdict(stats.rounds, stats.correct)
    return (
        f"flacblind: {stats.correct}/{stats.rounds} correct "
        f"(p = {fmt_p_value(stats.p_value)}, 95 % CI "
        f"{stats.ci_low * 100:.0f}–{stats.ci_high * 100:.0f} %) — {reading.label}"
    )


def format_summary(report: SessionReport) -> str:
    """One-line, copy-pasteable summary of a finished session."""
    stats = report.stats
    reading = verdict(stats.rounds, stats.correct)
    return (
        f"flacblind: {stats.correct}/{stats.rounds} correct "
        f"(p = {fmt_p_value(stats.p_value)}, 95 % CI "
        f"{stats.ci_low * 100:.0f}–{stats.ci_high * 100:.0f} %) — {reading.label}"
    )
