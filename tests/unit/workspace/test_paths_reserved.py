"""Reserved-name guard for the global-tier ``_registry`` directory.

The generic ``bot.workspace`` package reserves ``_registry`` for global-tier
state (workspace registry + conversation map). No per-workspace
``WorkspacePaths`` accessor may produce that segment.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.workspace.paths import (
    RESERVED_GLOBAL_DIR,
    WorkspacePaths,
    is_reserved_segment,
)


def test_registry_segment_is_reserved() -> None:
    assert is_reserved_segment(RESERVED_GLOBAL_DIR)
    assert not is_reserved_segment("memory")
    assert not is_reserved_segment("inbox")


def test_workspace_level_accessors_do_not_collide_with_registry(tmp_path: Path) -> None:
    p = WorkspacePaths(root=tmp_path / ".modex")
    names = {
        p.inbox_dir.name,
        p.overflow_dir.name,
        p.sessions_dir.name,
        p.session_index_dir.name,
        p.pool_sessions_dir.name,
    }
    assert RESERVED_GLOBAL_DIR not in names
