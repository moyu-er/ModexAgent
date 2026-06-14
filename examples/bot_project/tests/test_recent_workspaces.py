"""Tests for RecentWorkspaces — recent workspace path tracking."""

from __future__ import annotations

import tempfile
from pathlib import Path

from bot.service.recent_workspaces import RecentWorkspaces


def test_add_and_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RecentWorkspaces(Path(tmp))
        store.load()

        store.add("/home/user/project-a")
        store.add("/home/user/project-b")

        recent = store.list_recent()
        assert len(recent) == 2
        assert recent[0]["path"] == "/home/user/project-b"  # most recent first
        assert recent[1]["path"] == "/home/user/project-a"


def test_dedup_moves_to_front() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RecentWorkspaces(Path(tmp))
        store.load()

        store.add("/a")
        store.add("/b")
        store.add("/a")  # re-add — should move to front

        recent = store.list_recent()
        assert len(recent) == 2
        assert recent[0]["path"] == "/a"
        assert recent[1]["path"] == "/b"


def test_max_20() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RecentWorkspaces(Path(tmp))
        store.load()

        for i in range(25):
            store.add(f"/path/{i}")

        recent = store.list_recent()
        assert len(recent) == 20
        # Most recent should be first
        assert recent[0]["path"] == "/path/24"
        # Oldest should be dropped
        paths = {r["path"] for r in recent}
        assert "/path/0" not in paths


def test_remove() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RecentWorkspaces(Path(tmp))
        store.load()

        store.add("/a")
        store.add("/b")
        store.add("/c")

        assert store.remove("/b") is True
        recent = store.list_recent()
        paths = {r["path"] for r in recent}
        assert "/b" not in paths
        assert "/a" in paths
        assert "/c" in paths

        assert store.remove("/nonexistent") is False


def test_survives_reload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store1 = RecentWorkspaces(base)
        store1.load()
        store1.add("/ws-a")
        store1.add("/ws-b")

        store2 = RecentWorkspaces(base)
        store2.load()
        recent = store2.list_recent()
        assert len(recent) == 2
        assert recent[0]["path"] == "/ws-b"


def test_empty_on_fresh() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = RecentWorkspaces(Path(tmp))
        store.load()
        assert store.list_recent() == []
