"""Tests for ContextForkBuilder — fork XML build/cleanup + registry ownership."""
from __future__ import annotations

from pathlib import Path

from modex_agent.multi_agent.context_fork import ContextForkBuilder


def test_cleanup_removes_registered_file(tmp_path: Path) -> None:
    fork_file = tmp_path / "fork_contexts" / "scout_abc.xml"
    fork_file.parent.mkdir(parents=True, exist_ok=True)
    fork_file.write_text("<forked_context/>", encoding="utf-8")
    builder = ContextForkBuilder()
    builder.register_for_cleanup(
        session_id="abc.main", fork_workspace=tmp_path,
        agent_type="scout", invocation_id="abc",
    )
    assert fork_file.exists()
    builder.cleanup("abc.main")
    assert not fork_file.exists()


def test_cleanup_missing_session_is_noop(tmp_path: Path) -> None:
    builder = ContextForkBuilder()
    # Must not raise.
    builder.cleanup("never-registered.main")


def test_register_for_cleanup_ignores_missing_file(tmp_path: Path) -> None:
    """register_for_cleanup only registers a file that exists on disk.

    A non-existent fork file is not registered, so cleanup is a safe no-op
    (the builder never tracks a path it cannot compute from a real file).
    """
    builder = ContextForkBuilder()
    builder.register_for_cleanup(
        session_id="abc.main", fork_workspace=tmp_path,
        agent_type="scout", invocation_id="abc",
    )
    builder.cleanup("abc.main")  # must not raise
