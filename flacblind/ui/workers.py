"""Background workers.

Nothing in this module touches a widget: a :class:`QThread` subclass runs the
blocking core pipeline and reports back through signals, which the GUI thread
receives as ordinary queued slot calls.  That is what keeps the window
responsive (and the progress bar alive) while ffmpeg grinds.
"""

from __future__ import annotations

import threading
from typing import Any

from ..core.errors import FlacBlindError, PreparationCancelled
from ..core.ffmpeg import FFmpeg
from ..core.prepare import AudioPreparer, PrepareRequest, PrepareResult
from ..core.tempstore import TempWorkspace
from .qtcompat import QThread, Signal

__all__ = ["PrepareWorker", "ProbeWorker"]


class PrepareWorker(QThread):
    """Runs :meth:`AudioPreparer.prepare` off the GUI thread.

    Signals
        ``progress(float, str)``  fraction in ``0..1`` plus a stage description
        ``log(str)``              a line of ffmpeg output (only when verbose)
        ``prepared(object)``      a :class:`~flacblind.core.models.PreparedPair`
        ``failed(str, str)``      message and remediation hint
        ``cancelled()``           the user aborted
    """

    progress = Signal(float, str)
    log = Signal(str)
    prepared = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(
        self,
        ffmpeg: FFmpeg,
        request: PrepareRequest,
        workspace: TempWorkspace,
        *,
        verbose: bool = False,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._request = request
        self._workspace = workspace
        self._verbose = verbose
        self._cancel = threading.Event()
        self._ffmpeg = FFmpeg(ffmpeg.ffmpeg, ffmpeg.ffprobe, on_log=self.log.emit if verbose else None)

    # -- control ---------------------------------------------------------
    def cancel(self) -> None:
        """Ask ffmpeg to stop; the thread finishes within a frame or two."""
        self._cancel.set()

    @property
    def cancelling(self) -> bool:
        return self._cancel.is_set()

    # -- thread body -----------------------------------------------------
    def run(self) -> None:  # noqa: PLR0912 - a linear pipeline, splitting hurts
        try:
            preparer = AudioPreparer(self._ffmpeg, self._workspace.path)
            result: PrepareResult = preparer.prepare(
                self._request,
                progress=lambda fraction, label: self.progress.emit(fraction, label),
                cancel=self._cancel,
            )
        except PreparationCancelled:
            self.cancelled.emit()
        except FlacBlindError as exc:
            self.failed.emit(exc.message, exc.hint)
        except Exception as exc:  # noqa: BLE001 - a worker must never kill the app
            self.failed.emit(f"Unexpected error: {exc}", "")
        else:
            self.prepared.emit(result)


class ProbeWorker(QThread):
    """Reads a file's metadata so the drop cards can update instantly."""

    probed = Signal(object, object)   # path, ProbeInfo | error message
    failed = Signal(str, str)

    def __init__(self, ffmpeg: FFmpeg, paths: list[str], parent: Any = None) -> None:
        super().__init__(parent)
        self._ffmpeg = ffmpeg
        self._paths = list(paths)

    def run(self) -> None:
        for path in self._paths:
            try:
                info = self._ffmpeg.probe(path)
            except FlacBlindError as exc:
                self.failed.emit(path, exc.message)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(path, str(exc))
            else:
                self.probed.emit(path, info)
