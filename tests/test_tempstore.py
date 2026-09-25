"""Temporary workspace lifecycle, registry and crash-leftover sweeping."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401

from flacblind.core.tempstore import (
    WORKSPACE_PREFIX,
    TempRegistry,
    TempWorkspace,
    process_alive,
    sweep_stale_workspaces,
)


class TestTempWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-test-temp-"))
        self.registry = TempRegistry(self.tmp / "registry.json")

    def test_creation_and_naming(self) -> None:
        with TempWorkspace(root=self.tmp, registry=self.registry) as workspace:
            self.assertTrue(workspace.path.exists())
            self.assertTrue(workspace.path.name.startswith(WORKSPACE_PREFIX))
            self.assertTrue(workspace.path.is_dir())

    def test_allocate_paths(self) -> None:
        with TempWorkspace(root=self.tmp, registry=self.registry) as workspace:
            self.assertEqual(workspace.wav("A").name, "a.wav")
            self.assertEqual(workspace.allocate("x/y.wav").parent.name, "x")
            self.assertEqual(workspace.files(), [])

    def test_cleanup_removes_everything(self) -> None:
        workspace = TempWorkspace(root=self.tmp, registry=self.registry)
        path = workspace.path
        (path / "a.wav").write_bytes(b"x" * 100)
        workspace.register()
        self.assertEqual(len(workspace.files()), 1)
        self.assertGreater(workspace.size_bytes, 0)
        workspace.cleanup()
        self.assertFalse(path.exists())
        self.assertTrue(workspace.cleaned)
        workspace.cleanup()  # idempotent
        self.assertFalse(path.exists())

    def test_registration_round_trip(self) -> None:
        workspace = TempWorkspace(root=self.tmp, registry=self.registry).register()
        entries = self.registry.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, str(workspace.path))
        self.assertEqual(entries[0].pid, os.getpid())
        self.assertLess(entries[0].age_sec, 5.0)
        workspace.cleanup()
        self.assertEqual(self.registry.load(), [])

    def test_keep_leaves_the_files(self) -> None:
        workspace = TempWorkspace(root=self.tmp, registry=self.registry).register()
        path = workspace.path
        (path / "a.wav").write_bytes(b"x")
        workspace.keep()
        self.assertTrue(path.exists())
        self.assertEqual(self.registry.load(), [])
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def test_registry_survives_corruption(self) -> None:
        self.registry.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.registry.load(), [])
        self.registry.register(self.tmp / "x")
        self.assertEqual(len(self.registry.load()), 1)

    def test_registry_ignores_garbage_entries(self) -> None:
        self.registry.path.write_text(
            json.dumps([{"path": 5}, "nope", {"path": "/tmp/x", "pid": "bad"}]),
            encoding="utf-8",
        )
        self.assertEqual(self.registry.load(), [])

    def test_sweep_removes_dead_owners(self) -> None:
        dead = TempWorkspace(root=self.tmp, registry=self.registry)
        dead.register()
        entries = self.registry.load()
        # Pretend the owner process is long gone and the folder is old.
        self.registry.save(
            [type(entries[0])(path=entries[0].path, pid=999_999, created=time.time() - 10**6)]
        )
        # A dead owner is enough: the crash case is cleaned up immediately.
        removed = sweep_stale_workspaces(self.registry, root=self.tmp)
        self.assertIn(dead.path, removed)
        self.assertFalse(dead.path.exists())

    def test_sweep_keeps_our_own_workspace(self) -> None:
        alive = TempWorkspace(root=self.tmp, registry=self.registry).register()
        removed = sweep_stale_workspaces(self.registry, root=self.tmp, max_age_sec=0)
        self.assertNotIn(alive.path, removed)
        self.assertTrue(alive.path.exists())
        alive.cleanup()

    def test_sweep_ignores_age_for_a_dead_owner(self) -> None:
        """A crashed session's files go now, not in 24 hours."""
        dead = TempWorkspace(root=self.tmp, registry=self.registry).register()
        entries = self.registry.load()
        self.registry.save(
            [type(entries[0])(path=entries[0].path, pid=999_999, created=time.time())]
        )
        removed = sweep_stale_workspaces(self.registry, root=self.tmp, max_age_sec=10**9)
        self.assertIn(dead.path, removed)

    def test_sweep_keeps_fresh_untracked_directories(self) -> None:
        fresh = self.tmp / f"{WORKSPACE_PREFIX}fresh"
        fresh.mkdir()
        (fresh / "a.wav").write_bytes(b"x")
        sweep_stale_workspaces(self.registry, root=self.tmp, max_age_sec=3600)
        self.assertTrue(fresh.exists(), "a directory younger than max_age must survive")

    def test_sweep_removes_old_untracked_directories(self) -> None:
        old = self.tmp / f"{WORKSPACE_PREFIX}old"
        old.mkdir()
        (old / "a.wav").write_bytes(b"x")
        import os as _os

        ancient = time.time() - 10**6
        _os.utime(old, (ancient, ancient))
        removed = sweep_stale_workspaces(self.registry, root=self.tmp, max_age_sec=3600)
        self.assertIn(old, removed)
        self.assertFalse(old.exists())

    def test_sweep_prunes_missing_entries(self) -> None:
        self.registry.register(self.tmp / "never-existed")
        sweep_stale_workspaces(self.registry, root=self.tmp, max_age_sec=0)
        self.assertEqual(self.registry.load(), [])

    def test_process_alive(self) -> None:
        self.assertTrue(process_alive(os.getpid()))
        self.assertFalse(process_alive(0))
        self.assertFalse(process_alive(-1))
        self.assertFalse(process_alive(2_000_000))


if __name__ == "__main__":
    unittest.main()
