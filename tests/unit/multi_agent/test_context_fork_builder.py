"""Tests for ContextForkBuilder — fork XML build/cleanup + registry ownership."""
from __future__ import annotations

from pathlib import Path

from modex_agent.multi_agent.context_fork import ContextForkBuilder


def test_cleanup_removes_registered_file(tmp_path: Path) -> None:
    fork_file = tmp_path / "fork_contexts" / "scout_abc.xml"
    fork_file.parent.mkdir(parents=True, exist_ok=True)
    fork_file.write_text("<forked_context/>", encoding="utf-8")
    builder = ContextForkBuilder()
    builder._register(session_id="abc.main", path=fork_file)
    assert fork_file.exists()
    builder.cleanup("abc.main")
    assert not fork_file.exists()


def test_cleanup_missing_session_is_noop(tmp_path: Path) -> None:
    builder = ContextForkBuilder()
    # Must not raise.
    builder.cleanup("never-registered.main")


def test_cleanup_missing_file_is_noop(tmp_path: Path) -> None:
    fork_file = tmp_path / "missing.xml"
    builder = ContextForkBuilder()
    builder._register(session_id="abc.main", path=fork_file)
    # File was registered but never created on disk — cleanup must not raise.
    builder.cleanup("abc.main")
