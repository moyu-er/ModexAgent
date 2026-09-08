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

from modex_agent.tools.standard.file_tool import ListDirTool, ReadFileTool, WriteFileTool
from modex_agent.tools.standard.glob_tool import GlobTool
from modex_agent.tools.standard.search_tool import SearchFilesTool
from modex_agent.tools.terminal import SubprocessTool
from modex_agent.tools.workspace_scoped import (
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
    """``~`` expansions match the permission resolver, without a workspace prefix."""
    _, ws = home_and_ws
    monkeypatch.chdir(ws)

    tool = WorkspaceScopedFileTool(ListDirTool(), _StaticProvider(ws))
    args = tool._scoped_args({"path": "~/anything"})
    assert args["path"] == str((Path.home() / "anything").resolve())


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
    """search/glob default path ``.`` must scope to the workspace."""
    home, ws = home_and_ws
    monkeypatch.chdir(home)

    for tool_cls in (SearchFilesTool, GlobTool):
        wrapped = WorkspaceScopedFileTool(tool_cls(), _StaticProvider(ws))
        # Both accept path="." default; assert the rewrite targets the ws.
        rewritten = wrapped._scoped_args({"path": "."})
        assert rewritten["path"] == str(ws)
        # Names: SearchFilesTool→grep, GlobTool→glob, both routed as file.
        assert wrapped.name in ("grep", "glob")


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
        GlobTool(),
    ]
    wrapped = wrap_standard_tools(tools, provider)

    by_name = {t.name: t for t in wrapped}
    assert isinstance(by_name["ls"], WorkspaceScopedFileTool)
    assert isinstance(by_name["read"], WorkspaceScopedFileTool)
    assert isinstance(by_name["grep"], WorkspaceScopedFileTool)
    assert isinstance(by_name["glob"], WorkspaceScopedFileTool)
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


# ---------------------------------------------------------------------------
# Regression: LLM omits ``path`` — workspace root must be injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapped_glob_omitted_path_searches_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """glob called WITHOUT ``path`` arg must search workspace, not process CWD."""
    home, ws = home_and_ws
    monkeypatch.chdir(home)  # process CWD is home, not ws

    # Create files ONLY in ws, not in home
    (ws / "ws_only.py").write_text("x", encoding="utf-8")
    (home / "home_only.py").write_text("x", encoding="utf-8")

    wrapped = WorkspaceScopedFileTool(GlobTool(), _StaticProvider(ws))
    # LLM calls glob(pattern="*.py") — no path key at all
    result = await wrapped.execute(pattern="*.py")

    assert "ws_only.py" in result
    assert "home_only.py" not in result


@pytest.mark.asyncio
async def test_wrapped_grep_omitted_path_searches_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """grep called WITHOUT ``path`` arg must search workspace, not process CWD."""
    home, ws = home_and_ws
    monkeypatch.chdir(home)

    (ws / "ws_file.py").write_text("TARGET_MARKER\n", encoding="utf-8")
    (home / "home_file.py").write_text("TARGET_MARKER\n", encoding="utf-8")

    wrapped = WorkspaceScopedFileTool(SearchFilesTool(), _StaticProvider(ws))
    # LLM calls grep(pattern="TARGET_MARKER") — no path key at all
    result = await wrapped.execute(pattern="TARGET_MARKER", regex=False)

    assert "ws_file.py" in result
    assert "home_file.py" not in result


@pytest.mark.asyncio
async def test_wrapped_ls_omitted_path_searches_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ls called WITHOUT ``path`` arg must list workspace, not process CWD."""
    home, ws = home_and_ws
    monkeypatch.chdir(home)

    (ws / "ws_dir_file.py").write_text("x", encoding="utf-8")
    (home / "home_dir_file.py").write_text("x", encoding="utf-8")

    wrapped = WorkspaceScopedFileTool(ListDirTool(), _StaticProvider(ws))
    # LLM calls ls() — no path key at all
    result = await wrapped.execute()

    assert "ws_dir_file.py" in result
    assert "home_dir_file.py" not in result


# ---------------------------------------------------------------------------
# Regression: workspace switch — same tool instance targets new workspace
# ---------------------------------------------------------------------------


class _MutableProvider(WorkspaceRootProvider):
    """Provider whose root can be swapped at runtime — simulates /cd switch."""

    def __init__(self) -> None:
        self._root: Path | None = None

    def set_root(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        if self._root is None:
            raise RuntimeError("root not set")
        return self._root


@pytest.mark.asyncio
async def test_wrapped_glob_workspace_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After workspace switch, same tool must search the NEW workspace."""
    home = tmp_path / "home"
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    home.mkdir()
    ws_a.mkdir()
    ws_b.mkdir()
    monkeypatch.chdir(home)

    (ws_a / "file_a.py").write_text("x", encoding="utf-8")
    (ws_b / "file_b.py").write_text("x", encoding="utf-8")

    provider = _MutableProvider()
    provider.set_root(ws_a)
    wrapped = WorkspaceScopedFileTool(GlobTool(), provider)

    # Phase 1: workspace A — path omitted
    result_a = await wrapped.execute(pattern="*.py")
    assert "file_a.py" in result_a
    assert "file_b.py" not in result_a

    # Phase 2: switch to workspace B — same tool instance, path still omitted
    provider.set_root(ws_b)
    result_b = await wrapped.execute(pattern="*.py")
    assert "file_b.py" in result_b
    assert "file_a.py" not in result_b


def test_scoped_path_preserves_the_approved_spelling(tmp_path: Path) -> None:
    from modex_agent.sandbox.tool_matrix import approval_anchor

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    raw = " ../outside.txt"
    wrapped = WorkspaceScopedFileTool(WriteFileTool(), _StaticProvider(workspace))

    rewritten = wrapped._scoped_args({"path": raw, "content": "changed"})

    assert rewritten["path"] == approval_anchor("write", {"path": raw}, workspace)
    assert Path(rewritten["path"]) != tmp_path / "outside.txt"


def test_explicit_relative_shell_cwd_tracks_workspace_switch(tmp_path: Path) -> None:
    provider = _MutableProvider()
    provider.set_root(tmp_path / "old")
    wrapped = wrap_standard_tools([SubprocessTool(working_dir=str(tmp_path / "old"))], provider)[0]
    assert isinstance(wrapped, WorkspaceScopedShellTool)
    provider.set_root(tmp_path / "current")

    rewritten = wrapped._scoped_args({"command": "pwd", "working_dir": "sub"})

    assert rewritten["working_dir"] == str(tmp_path / "current" / "sub")


async def test_shared_scoping_keeps_ast_writes_in_checked_workspace(
    home_and_ws: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from modex_agent.tools.ast.ast_replace import AstGrepReplaceTool

    home, workspace = home_and_ws
    source = "x = 1\n"
    (home / "sample.py").write_text(source, encoding="utf-8")
    (workspace / "sample.py").write_text(source, encoding="utf-8")
    monkeypatch.chdir(home)
    wrapped = wrap_standard_tools([AstGrepReplaceTool()], _StaticProvider(workspace))[0]

    result = await wrapped.execute(
        pattern="(integer) @value", replacement="2", language="python",
        path="sample.py", dry_run=False,
    )

    assert (workspace / "sample.py").read_text(encoding="utf-8") == "x = 2\n", result
    assert (home / "sample.py").read_text(encoding="utf-8") == source
