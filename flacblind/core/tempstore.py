"""Temporary WAV files: creation, tracking and guaranteed cleanup.

The original CLI deleted its temp directory with ``shutil.rmtree`` in a
``finally`` block, which leaks a directory full of WAVs whenever the process is
killed.  Here every workspace is *registered* in a small JSON file, so the next
launch of the app can find and delete leftovers from a crashed session —
"search and remove temporary files" in the literal sense.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

__all__ = [
    "WORKSPACE_PREFIX",
    "TempWorkspace",
    "TempRegistry",
    "sweep_stale_workspaces",
    "default_workspace_root",
    "process_alive",
]

WORKSPACE_PREFIX = "flacblind-"
REGISTRY_NAME = "temp-workspaces.json"
#: Leftovers older than this are removed on startup (unless still in use).
STALE_AFTER_SEC = 24 * 3600


def default_workspace_root() -> Path:
    """Where the workspaces live (``$TMPDIR/flacblind-XXXX`` by default)."""
    return Path(tempfile.gettempdir())


def process_alive(pid: int) -> bool:
    """``True`` when a process with this id exists (best effort, cross-platform)."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows only
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:  # pragma: no cover - exotic platforms
        return exc.errno != errno.ESRCH
    return True


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One tracked workspace directory."""

    path: str
    pid: int
    created: float

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - self.created)


class TempRegistry:
    """Tiny JSON registry of live workspaces, safe against concurrent writers."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_workspace_root() / REGISTRY_NAME

    def load(self) -> list[RegistryEntry]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        entries: list[RegistryEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path:
                continue
            try:
                entries.append(
                    RegistryEntry(
                        path=path,
                        pid=int(item.get("pid", 0)),
                        created=float(item.get("created", 0.0)),
                    )
                )
            except (TypeError, ValueError):
                continue
        return entries

    def save(self, entries: list[RegistryEntry]) -> None:
        """Persist ``entries`` verbatim (callers merge with :meth:`load`)."""
        payload = [{"path": e.path, "pid": e.pid, "created": e.created} for e in entries]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:  # pragma: no cover - read-only home directory
            pass

    def register(self, path: str | os.PathLike[str]) -> None:
        resolved = str(Path(path))
        entries = [e for e in self.load() if e.path != resolved]
        entries.append(RegistryEntry(path=resolved, pid=os.getpid(), created=time.time()))
        self.save(entries)

    def unregister(self, path: str | os.PathLike[str]) -> None:
        resolved = str(Path(path))
        entries = [e for e in self.load() if e.path != resolved]
        self.save(entries)

    def prune(self) -> None:
        """Drop entries for directories that no longer exist."""
        entries = [e for e in self.load() if Path(e.path).exists()]
        self.save(entries)


class TempWorkspace:
    """A per-session scratch directory holding the generated WAV files."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        registry: TempRegistry | None = None,
        *,
        suffix: str = "",
    ) -> None:
        self.root = Path(root) if root is not None else default_workspace_root()
        self.registry = registry or TempRegistry()
        self.path = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, suffix=suffix, dir=str(self.root)))
        self.created = time.time()
        self._cleaned = False
        self._atexit_installed = False

    # ----------------------------------------------------------------- files
    def allocate(self, name: str) -> Path:
        """Return a path inside the workspace (file not created)."""
        return self.path / name

    def wav(self, key: str) -> Path:
        return self.allocate(f"{key.lower()}.wav")

    def files(self) -> list[Path]:
        if not self.path.exists():
            return []
        return sorted(p for p in self.path.rglob("*") if p.is_file())

    @property
    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.files() if p.exists())

    @property
    def cleaned(self) -> bool:
        return self._cleaned

    # -------------------------------------------------------------- lifetime
    def register(self) -> "TempWorkspace":
        self.registry.register(self.path)
        self._atexit_installed = True
        atexit.register(self.cleanup)
        return self

    def cleanup(self, *, unregister: bool = True) -> None:
        """Delete the directory.  Safe to call twice."""
        if self._cleaned:
            return
        self._cleaned = True
        if unregister:
            with contextlib.suppress(OSError):
                self.registry.unregister(self.path)
        if self._atexit_installed:
            with contextlib.suppress(Exception):
                atexit.unregister(self.cleanup)
            self._atexit_installed = False
        shutil.rmtree(self.path, ignore_errors=True)

    def keep(self) -> Path:
        """Stop tracking the directory and return its path (debugging aid)."""
        self._cleaned = True
        with contextlib.suppress(OSError):
            self.registry.unregister(self.path)
        if self._atexit_installed:
            with contextlib.suppress(Exception):
                atexit.unregister(self.cleanup)
            self._atexit_installed = False
        return self.path

    # ------------------------------------------------------------- dunder
    def __enter__(self) -> "TempWorkspace":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cleanup()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TempWorkspace({str(self.path)!r}, cleaned={self._cleaned})"

    def __iter__(self) -> Iterator[Path]:  # pragma: no cover - convenience
        return iter(self.files())


def sweep_stale_workspaces(
    registry: TempRegistry | None = None,
    *,
    root: str | os.PathLike[str] | None = None,
    max_age_sec: float = STALE_AFTER_SEC,
) -> list[Path]:
    """Delete workspaces left behind by crashed runs.

    Two different rules, because the two cases carry different information:

    * **Registered** directory whose owner process is gone → delete right away.
      This is the crash case, and it is exactly what the user wants cleaned up.
      The liveness check alone is enough to keep us from touching a running
      instance (a recycled pid simply looks alive, so it is left alone).
    * **Unregistered** directory older than ``max_age_sec`` → delete. With no
      owner to ask, age is the only safe signal.
    """
    registry = registry or TempRegistry()
    base = Path(root) if root is not None else default_workspace_root()
    removed: list[Path] = []
    now = time.time()
    protected: set[Path] = set()

    for entry in registry.load():
        path = Path(entry.path)
        if not path.exists():
            registry.unregister(path)
            continue
        if entry.pid == os.getpid() or process_alive(entry.pid):
            # Ours, or another running instance: never touch it.
            protected.add(path)
            continue
        # The owner is gone — this is a crashed session, clean it up now.
        shutil.rmtree(path, ignore_errors=True)
        registry.unregister(path)
        removed.append(path)

    if base.is_dir():
        for candidate in base.glob(f"{WORKSPACE_PREFIX}*"):
            if not candidate.is_dir() or candidate in protected or candidate in removed:
                continue
            try:
                stat = candidate.stat()
            except OSError:  # pragma: no cover - race with another instance
                continue
            if (now - stat.st_mtime) >= max_age_sec:
                shutil.rmtree(candidate, ignore_errors=True)
                removed.append(candidate)

    if removed:
        registry.prune()
    return removed
