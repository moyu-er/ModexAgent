"""Tests for bot.service.workspace.paths — path primitives layer.

TDD: written BEFORE implementation. Expected to fail (RED) until
paths.py exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.workspace.paths import (
    SUBDIR_COMMANDS,
    SUBDIR_EXPERIENCES,
    SUBDIR_INBOX,
    SUBDIR_MEDIA,
    SUBDIR_MEMORY,
    SUBDIR_OUTPUT,
    SUBDIR_OVERFLOW,
    SUBDIR_POOL_SESSIONS,
    SUBDIR_PRUNED,
    SUBDIR_RUNTIME,
    SUBDIR_SESSION_INDEX,
    SUBDIR_SESSIONS,
    SUBDIR_TRACE,
    SUBDIR_TURNS,
    WorkspacePaths,
    safe_segment,
)

# ---------------------------------------------------------------------------
# safe_segment
# ---------------------------------------------------------------------------


class TestSafeSegment:
    def test_plain_unchanged(self) -> None:
        assert safe_segment("pool_alpha") == "pool_alpha"

    def test_dotdot_neutralized(self) -> None:
        result = safe_segment("../evil")
        assert ".." not in result
        assert "/" not in result
        assert "\\" not in result

    def test_slashes_neutralized(self) -> None:
        result = safe_segment("a/b\\c")
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result

    def test_empty_returns_underscore(self) -> None:
        assert safe_segment("") == "_"

    def test_whitespace_only_returns_underscore(self) -> None:
        assert safe_segment("   ") == "_"

    def test_only_allowed_chars_preserved(self) -> None:
        # letters, digits, underscore, hyphen are all in the allowed set
        assert safe_segment("A1-2_3") == "A1-2_3"

    def test_dot_is_neutralized(self) -> None:
        # Dots are excluded from the allowed set (converges with
        # JsonFileTurnStateStore._SAFE_RE), so dotted input loses its dots.
        result = safe_segment("pool.v2")
        assert "." not in result

    def test_special_chars_replaced(self) -> None:
        result = safe_segment("a:b*c?")
        assert ":" not in result
        assert "*" not in result
        assert "?" not in result


# ---------------------------------------------------------------------------
# WorkspacePaths accessors
# ---------------------------------------------------------------------------


class TestWorkspacePaths:
    def test_root_resolved_absolute(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        assert wp.root.is_absolute()

    def test_memory_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.memory_dir("pool_a")
        assert result.is_relative_to(wp.root)

    def test_media_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.media_dir("pool_a")
        assert result.is_relative_to(wp.root)

    def test_media_dir_two_pools_distinct(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        a = wp.media_dir("pool_a")
        b = wp.media_dir("pool_b")
        assert a != b

    def test_media_dir_malicious_pool_cannot_escape(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.media_dir("../../etc")
        assert result.is_relative_to(wp.root)
        rel = result.relative_to(wp.root)
        assert ".." not in rel.parts

    def test_pruned_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.pruned_dir("pool_a")
        assert result.is_relative_to(wp.root)

    def test_runtime_dir_turns_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.runtime_dir("pool_a", SUBDIR_TURNS)
        assert result.is_relative_to(wp.root)

    def test_runtime_dir_commands_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.runtime_dir("pool_a", SUBDIR_COMMANDS)
        assert result.is_relative_to(wp.root)

    def test_runtime_dir_trace_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.runtime_dir("pool_a", SUBDIR_TRACE)
        assert result.is_relative_to(wp.root)

    def test_runtime_dir_output_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.runtime_dir("pool_a", SUBDIR_OUTPUT)
        assert result.is_relative_to(wp.root)

    def test_runtime_dir_rejects_unknown_leaf(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        with pytest.raises(ValueError):
            wp.runtime_dir("main", "bogus")

    def test_experience_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.experience_dir("pool_a", "agent_x")
        assert result.is_relative_to(wp.root)

    def test_inbox_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        assert wp.inbox_dir.is_relative_to(wp.root)

    def test_state_db_is_workspace_level(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        assert wp.state_db == wp.root / "state.db"

    def test_pool_sessions_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        assert wp.pool_sessions_dir.is_relative_to(wp.root)

    def test_sessions_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        assert wp.sessions_dir.is_relative_to(wp.root)

    def test_session_index_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        assert wp.session_index_dir.is_relative_to(wp.root)

    def test_overflow_dir_under_root(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        assert wp.overflow_dir.is_relative_to(wp.root)

    def test_malicious_pool_name_cannot_escape(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.memory_dir("../../etc")
        assert result.is_relative_to(wp.root)
        # No ".." should appear in the path below root
        rel = result.relative_to(wp.root)
        assert ".." not in rel.parts

    def test_malicious_pool_runtime_cannot_escape(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        result = wp.runtime_dir("../../etc", SUBDIR_TURNS)
        assert result.is_relative_to(wp.root)
        rel = result.relative_to(wp.root)
        assert ".." not in rel.parts

    def test_frozen(self, tmp_path: Path) -> None:
        from dataclasses import FrozenInstanceError

        wp = WorkspacePaths(root=tmp_path)
        with pytest.raises(FrozenInstanceError):
            wp.root = tmp_path / "other"  # type: ignore[misc]


class TestMkdirSkeleton:
    def test_creates_five_workspace_dirs(self, tmp_path: Path) -> None:
        wp = WorkspacePaths(root=tmp_path)
        wp.mkdir_skeleton()
        assert wp.inbox_dir.is_dir()
        assert wp.pool_sessions_dir.is_dir()
        assert wp.sessions_dir.is_dir()
        assert wp.session_index_dir.is_dir()
        assert wp.overflow_dir.is_dir()


class TestLayoutConstants:
    def test_constants_have_expected_values(self) -> None:
        assert SUBDIR_MEMORY == "memory"
        assert SUBDIR_MEDIA == "media"
        assert SUBDIR_RUNTIME == "runtime_state"
        assert SUBDIR_INBOX == "inbox"
        assert SUBDIR_EXPERIENCES == "experiences"
        assert SUBDIR_POOL_SESSIONS == "pool_sessions"
        assert SUBDIR_SESSIONS == "sessions"
        assert SUBDIR_SESSION_INDEX == "session_index"
        assert SUBDIR_OVERFLOW == "overflow"
        assert SUBDIR_TURNS == "turns"
        assert SUBDIR_COMMANDS == "commands"
        assert SUBDIR_TRACE == "trace"
        assert SUBDIR_OUTPUT == "output"
        assert SUBDIR_PRUNED == "pruned"
