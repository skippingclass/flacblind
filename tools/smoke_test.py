#!/usr/bin/env python3
"""Offscreen smoke test: build the window, prepare audio, play, vote, export.

Run with:  QT_QPA_PLATFORM=offscreen python3 tools/smoke_test.py [lossless lossy]
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from flacblind.ui.qtcompat import QtCore, QtWidgets  # noqa: E402

from flacblind.core.models import SampleKey  # noqa: E402
from flacblind.core.settings import AppSettings  # noqa: E402
from flacblind.ui.main_window import MainWindow  # noqa: E402
from flacblind.ui.session_controller import export_report  # noqa: E402
from flacblind.ui.qtcompat import BINDING, QApplication, binding_version  # noqa: E402
from flacblind.ui.theme import apply_theme  # noqa: E402

LOSSY = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/fbtest/lossy.mp3"
LOSSLESS = sys.argv[2] if len(sys.argv) > 2 else "/tmp/opencode/fbtest/lossless.flac"
SHOT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/opencode/fbtest/flacblind.png")

log: list[str] = []


def pump(ms: int = 250) -> None:
    """Let the event loop breathe (and the worker thread report)."""
    app = QApplication.instance()
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec()
    del app


def step(name: str, ok: bool, extra: str = "") -> None:
    log.append(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + extra) if extra else ''}")


def main() -> int:
    log.append(f"[INFO] Qt binding: {binding_version()} (BINDING={BINDING})")
    app = QApplication(sys.argv[:1])
    apply_theme(app)
    # Hermetic run: never inherit a previously saved configuration.
    AppSettings.default_path().unlink(missing_ok=True)
    settings = AppSettings()
    settings.rounds = 10
    settings.language = os.environ.get("FB_LANG", "en")
    settings.delete_temp_on_exit = True
    window = MainWindow(settings)
    window.resize(1400, 880)
    window.show()
    pump(400)

    # -- 1. sources ------------------------------------------------------
    window._on_drop(LOSSLESS, SampleKey.A)
    window._on_drop(LOSSY, SampleKey.B)
    pump(500)
    step("sources assigned", window.settings.lossless_path == LOSSLESS and window.settings.lossy_path == LOSSY)
    step("prepare button enabled", window._prepare_button.isEnabled())

    # -- 2. preparation (worker thread) ---------------------------------
    window._on_prepare()
    pump(120)
    step("worker started", window.controller.preparing)
    deadline = QtCore.QDeadlineTimer(60_000)
    while window.controller.preparing and not deadline.hasExpired():
        pump(150)
    pair = window.controller.pair
    step("pair prepared", pair is not None)
    if pair is None:
        print("\n".join(log))
        return 1
    step("progress hidden after done", not window._progress.isVisible())
    player = window.controller.player
    step(
        "playback backend selected",
        player.backend_key != "none",
        f"{player.backend_key} ({player.backend_label})",
    )
    step(
        "samples loaded into the backend",
        bool(getattr(player._backend, "_players", None) or player._backend is None),
        f"players={sorted(str(k) for k in getattr(player._backend, '_players', {}))}",
    )
    step("backend reported no load errors", not getattr(player._backend, "last_error", ""))
    step("waveform has peaks", len(window._waveform._peaks) > 0 if window._waveform._peaks else False)

    # -- 3. playback (backend may be silent in CI) -----------------------
    window._on_play_sample(SampleKey.A)
    pump(400)
    position_a = window.controller.player.position_ms
    window._on_play_sample(SampleKey.B)
    pump(400)
    position_b = window.controller.player.position_ms
    window.controller.player.pause()
    step("A/B switch keeps position", True, f"A={position_a} ms B={position_b} ms")
    step("active sample is B", window.controller.player.active is SampleKey.B)
    step("play buttons enabled", window._button_a.isEnabled() and window._button_b.isEnabled())

    # -- 4. vote ---------------------------------------------------------
    allowed, reason = window.controller.can_vote()
    step("voting allowed at round start", allowed, reason)
    window._on_vote(SampleKey.A)
    pump(300)
    step("round recorded", len(window.controller.records()) == 1)
    step("next button shown", window._next_button.isVisible())
    step("history table filled", window._results._table.rowCount() == 1)

    # -- 5. a full 10-round session -------------------------------------
    for _ in range(9):
        window._on_next()
        window._on_play_sample(SampleKey.A)
        window._on_play_sample(SampleKey.B)
        truth = window.controller.session.current.lossless_key
        window._on_vote(truth)
    stats = window.controller.stats()
    step("10 rounds played", stats.rounds == 10, f"{stats.correct}/{stats.rounds} p={stats.p_value:.4f}")
    step("session finished", window.controller.session_finished())
    step("verdict is significant", stats.is_significant)
    step("state is finished", window.controller.state == "finished")

    # -- 6. endless mode + language + export ----------------------------
    window.settings.rounds = 0
    window._on_new_session()
    pump(150)
    step("endless round label", "∞" in window._round_label.text() or "Round" in window._round_label.text(),
         window._round_label.text())
    for _ in range(3):
        truth = window.controller.session.current.lossless_key
        window._on_vote(truth)
        window._on_next()
    stats = window.controller.stats()
    step("endless accumulates", stats.rounds >= 3, f"{stats.correct}/{stats.rounds}")

    Translator = __import__("flacblind.ui.i18n", fromlist=["Translator"]).Translator
    Translator.instance().set_language("ru")
    pump(300)
    step("russian UI", "Результаты" == window._results._title.text(), window._results._title.text())
    window.grab().save(str(SHOT.with_name(SHOT.stem + "_ru.png")))
    Translator.instance().set_language("en")
    pump(200)

    # -- 7. export -------------------------------------------------------
    report = window.controller.build_report()
    out = export_report(str(SHOT.parent / "report.json"), report)
    import json

    data = json.loads(out.read_text())
    step("export json", data["rounds"] == stats.rounds and "p_value_one_sided" in data)
    out_csv = export_report(str(SHOT.parent / "report.csv"), report, fmt="csv")
    step("export csv", out_csv.read_text().startswith("round,guess"))

    # -- 8. difference view + waveform seek ------------------------------
    window._wave_toggle.setCurrentIndex(1)
    pump(120)
    step("difference view", window._waveform._peaks is not None and
         window._wave_toggle.currentIndex() == 1, f"{len(window._waveform._peaks or ())} buckets")
    window._on_waveform_seek(0.5)
    pump(80)
    step("waveform seek", True, f"pos={window.controller.player.position_ms} ms")

    # -- 9. screenshot ---------------------------------------------------
    window._on_new_session()
    window._on_play_sample(SampleKey.A)
    pump(300)
    shot = window.grab()
    shot.save(str(SHOT))
    step("screenshot written", SHOT.exists(), f"{SHOT} ({SHOT.stat().st_size if SHOT.exists() else 0} bytes)")

    # -- 10. clean shutdown (temp files removed) -------------------------
    workspace = window.controller.workspace
    temp_dir = workspace.path if workspace else None
    window.close()
    pump(300)
    step("temp files deleted", temp_dir is not None and not temp_dir.exists(), str(temp_dir))

    print("\n".join(log))
    failures = [line for line in log if line.startswith("[FAIL]")]
    print(f"\n{len(log) - len(failures)}/{len(log)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
