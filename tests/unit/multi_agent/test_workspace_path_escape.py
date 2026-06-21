"""Subagent path resolution must never escape to project_dir or process CWD.

When workspace pool_data is unresolved, the communication service must surface
the problem rather than silently writing under ``<project_dir>/data/memory`` or
the process CWD (``Path('.')``) — both would leak data outside the owning
workspace.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from framework.multi_agent.communication import AgentCommunicationService


def _service(
    *,
    project_dir: Path,
    memory_dir: Path | None = None,
    runtime_dir: Path | None = None,
    pool_name: str = "main",
) -> AgentCommunicationService:
    return AgentCommunicationService(
        source=MagicMock(),
        broker=MagicMock(),
        registry=MagicMock(),
        pool_name=pool_name,
        project_dir=project_dir,
        memory_dir=memory_dir,
        runtime_dir=runtime_dir,
        workspace_manager=None,  # pool_data unresolved
    )


def test_fork_workspace_returns_none_not_project_dir(tmp_path: Path) -> None:
    svc = _service(project_dir=tmp_path, memory_dir=None)
    # Must NOT synthesize tmp_path/data/memory/main (escape outside the workspace).
    assert svc._fork_workspace() is None


def test_fork_workspace_uses_workspace_memory_dir(tmp_path: Path) -> None:
    mem = tmp_path / "ws_memory"
    svc = _service(project_dir=tmp_path, memory_dir=mem)
    assert svc._fork_workspace() == mem


def test_resolve_output_root_returns_workspace_dir(tmp_path: Path) -> None:
    rt = tmp_path / "ws_runtime"
    svc = _service(project_dir=tmp_path, runtime_dir=rt)
    assert svc._resolve_output_root() == rt


def test_resolve_output_root_never_process_cwd(tmp_path: Path) -> None:
    """When unresolved, OUTPUT root is an isolated temp dir — never Path('.') / CWD."""
    import os

    svc = _service(project_dir=tmp_path, runtime_dir=None)
    root = svc._resolve_output_root()
    assert root.is_absolute()  # a real dir, not a relative "."
    assert root != Path(".") and root != Path(os.getcwd()).resolve()
    # Same instance reused on subsequent calls (one isolated dir per service).
    assert svc._resolve_output_root() is root

