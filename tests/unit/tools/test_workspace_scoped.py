"""Unit tests for the workspace-scoped tool wrapper layer.

Verifies that wrapped file/search/shell tools resolve their default/relative
path argument against the active workspace root (provided dynamically),
instead of ``os.getcwd()`` — closing the gap pinned by
``test_workspace_tool_gap.py`` without modifying the default tool
implementations.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from framework.tools.standard.file_tool import ListDirTool, ReadFileTool, WriteFileTool
from framework.tools.standard.search_tool import FindFilesTool, SearchFilesTool
from framework.tools.terminal import SubprocessTool
from framework.tools.workspace_scoped import (
    WorkspaceRootProvider,
    WorkspaceScopedFileTool,
    WorkspaceScopedShellTool,
    WorkspaceScopedTool,
    wrap_standard_tools,
)


class _StaticProvider(WorkspaceRootProvider):
    """Test provider returning a fixed path."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def current(self) -> Path:
        return self._path


HOME_SENTINEL = "HOME_SENTINEL_FILE"
WS_SENTINEL = "WS_SENTINEL_FILE"


@pytest.fixture
def home_and_ws(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    (home / HOME_SENTINEL).write_text("home", encoding="utf-8")
    (ws / WS_SENTINEL).write_text("ws", encoding="utf-8")
    return home, ws


@pytest.mark.asyncio
async def test_wrapped_ls_dot_resolves_against_workspace_not_cwd(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap closed: wrapped ``ls .`` shows the workspace, even when the
    process CWD is frozen at home."""
    home, ws = home_and_ws
    monkeypatch.chdir(home)  # simulate worker-pinned / subprocess CWD

    provider = _StaticProvider(ws)
    tool = WorkspaceScopedFileTool(ListDirTool(), provider)

    listing = await tool.execute(path=".")

    assert WS_SENTINEL in listing
    assert HOME_SENTINEL not in listing


@pytest.mark.asyncio
async def test_wrapped_ls_relative_path_resolves_under_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, ws = home_and_ws
    sub = ws / "sub"
    sub.mkdir()
    (sub / "DEEP").write_text("x", encoding="utf-8")
    monkeypatch.chdir(home)

    tool = WorkspaceScopedFileTool(ListDirTool(), _StaticProvider(ws))
    listing = await tool.execute(path="sub")

    assert "DEEP" in listing


@pytest.mark.asyncio
async def test_wrapped_ls_absolute_path_untouched(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absolute paths must NOT be prefixed with the workspace root."""
    home, ws = home_and_ws
    monkeypatch.chdir(ws)

    tool = WorkspaceScopedFileTool(ListDirTool(), _StaticProvider(ws))
    listing = await tool.execute(path=str(home))

    assert HOME_SENTINEL in listing
    assert WS_SENTINEL not in listing


@pytest.mark.asyncio
async def test_wrapped_ls_home_path_untouched(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``~`` expansions are left for the inner tool to resolve."""
    _, ws = home_and_ws
    monkeypatch.chdir(ws)

    tool = WorkspaceScopedFileTool(ListDirTool(), _StaticProvider(ws))
    args = tool._scoped_args({"path": "~/anything"})
    assert args["path"] == "~/anything"


@pytest.mark.asyncio
async def test_wrapped_read_relative_reads_workspace_file(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, ws = home_and_ws
    monkeypatch.chdir(home)

    tool = WorkspaceScopedFileTool(ReadFileTool(), _StaticProvider(ws))
    result = await tool.execute(path=WS_SENTINEL)

    assert "ws" in result


@pytest.mark.asyncio
async def test_wrapped_write_relative_writes_under_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, ws = home_and_ws
    monkeypatch.chdir(home)

    tool = WorkspaceScopedFileTool(WriteFileTool(), _StaticProvider(ws))
    await tool.execute(path="newfile.txt", content="hello")

    assert (ws / "newfile.txt").read_text(encoding="utf-8") == "hello"
    assert not (home / "newfile.txt").exists()


@pytest.mark.asyncio
async def test_wrapped_search_default_root_is_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """search/find default path ``.`` must scope to the workspace."""
    home, ws = home_and_ws
    monkeypatch.chdir(home)

    for tool_cls in (SearchFilesTool, FindFilesTool):
        wrapped = WorkspaceScopedFileTool(tool_cls(), _StaticProvider(ws))
        # Both accept path="." default; assert the rewrite targets the ws.
        rewritten = wrapped._scoped_args({"path": "."})
        assert rewritten["path"] == str(ws)
        # Names: SearchFilesTool→grep, FindFilesTool→find, both routed as file.
        assert wrapped.name in ("grep", "find")


@pytest.mark.asyncio
async def test_wrapped_shell_defaults_working_dir_to_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bash`` with no ``working_dir`` must run in the workspace root."""
    home, ws = home_and_ws
    monkeypatch.chdir(home)

    tool = WorkspaceScopedShellTool(SubprocessTool(timeout=10), _StaticProvider(ws))
    rewritten = tool._scoped_args({"command": "pwd"})
    assert rewritten["working_dir"] == str(ws)


@pytest.mark.asyncio
async def test_wrapped_shell_explicit_working_dir_kept(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, ws = home_and_ws
    monkeypatch.chdir(ws)

    tool = WorkspaceScopedShellTool(SubprocessTool(timeout=10), _StaticProvider(ws))
    rewritten = tool._scoped_args({"command": "pwd", "working_dir": str(home)})
    assert rewritten["working_dir"] == str(home)


def test_wrap_standard_tools_routes_by_name() -> None:
    provider = _StaticProvider(Path("/tmp/ws"))
    tools = [
        ListDirTool(),
        ReadFileTool(),
        SubprocessTool(timeout=10),
        SearchFilesTool(),
    ]
    wrapped = wrap_standard_tools(tools, provider)

    by_name = {t.name: t for t in wrapped}
    assert isinstance(by_name["ls"], WorkspaceScopedFileTool)
    assert isinstance(by_name["read"], WorkspaceScopedFileTool)
    assert isinstance(by_name["grep"], WorkspaceScopedFileTool)
    assert isinstance(by_name["bash"], WorkspaceScopedShellTool)


def test_wrap_standard_tools_idempotent() -> None:
    provider = _StaticProvider(Path("/tmp/ws"))
    once = wrap_standard_tools([ListDirTool()], provider)
    twice = wrap_standard_tools(once, provider)
    assert len(twice) == 1
    assert twice[0] is once[0]


def test_wrapped_tool_delegates_schema_surface() -> None:
    """The LLM-facing schema must be identical to the inner tool's."""
    inner = ListDirTool()
    provider = _StaticProvider(Path("/tmp/ws"))
    wrapped = WorkspaceScopedFileTool(inner, provider)

    assert wrapped.name == inner.name
    assert wrapped.description == inner.description
    assert wrapped.parameters == inner.parameters
    assert wrapped.get_schema() == inner.get_schema()
    assert wrapped.get_dynamic_schema() == inner.get_dynamic_schema()
    assert wrapped.config is inner.config
    assert isinstance(wrapped, WorkspaceScopedTool)
