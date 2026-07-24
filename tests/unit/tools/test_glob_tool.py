"""Unit tests for GlobTool — covers rg, fd, and Python backends.

Tests are organised by backend:
  - ``TestGlobRgBackend``: real ripgrep (downloaded via conftest ``rg_env``)
  - ``TestGlobPythonFallback``: Python pathlib (``no_rg_env`` hides rg+fd)
  - ``TestGlobFallbackRollback``: verifies rg→fd→python chain
  - ``TestGlobEdgeCases``: path validation, limit clamping, empty dirs

All tests create isolated temp directories — no fixture pollution.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from modex_agent.tools.standard._ripgrep import RipgrepBackend, RipgrepResult, _normalize_rg_path
from modex_agent.tools.standard.glob_tool import GlobTool

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# RipgrepBackend unit tests (no subprocess needed)
# ---------------------------------------------------------------------------


class TestRipgrepResult:
    def test_frozen_model(self) -> None:
        r = RipgrepResult(lines=["a.py"], truncated=False)
        assert r.lines == ["a.py"]
        assert r.error is None
        with pytest.raises(ValidationError):
            r.lines = ["b.py"]  # type: ignore[misc]

    def test_with_error(self):
        r = RipgrepResult(lines=[], truncated=False, error="boom")
        assert r.error == "boom"
        assert r.lines == []


class TestNormalizeRgPath:
    def test_strips_leading_dot_slash(self):
        assert _normalize_rg_path("./src/file.py") == "src/file.py"

    def test_strips_leading_dot_backslash(self):
        assert _normalize_rg_path(".\\src\\file.py") == "src/file.py"

    def test_strips_repeated_dot_slash(self):
        assert _normalize_rg_path("././src/file.py") == "src/file.py"

    def test_normalizes_backslashes(self):
        assert _normalize_rg_path("src\\deep\\file.py") == "src/deep/file.py"

    def test_no_leading_dot(self):
        assert _normalize_rg_path("src/file.py") == "src/file.py"


class TestRipgrepBackendAvailable:
    def test_available_returns_bool(self):
        assert isinstance(RipgrepBackend.available(), bool)

    def test_resolve_returns_str_or_none(self):
        result = RipgrepBackend.resolve()
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# rg backend — real ripgrep (downloaded or system)
# ---------------------------------------------------------------------------


class TestGlobRgBackend:
    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_glob_by_extension(self, tmp_workspace: Path):
        (tmp_workspace / "a.py").write_text("x")
        (tmp_workspace / "b.py").write_text("x")
        (tmp_workspace / "c.txt").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace))
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_glob_recursive(self, tmp_workspace: Path):
        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "sub" / "deep.py").write_text("x")
        (tmp_workspace / "root.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="**/*.py", path=str(tmp_workspace))
        assert "root.py" in result
        assert "deep.py" in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_glob_brace_expansion(self, tmp_workspace: Path):
        (tmp_workspace / "a.ts").write_text("x")
        (tmp_workspace / "b.tsx").write_text("x")
        (tmp_workspace / "c.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.{ts,tsx}", path=str(tmp_workspace))
        assert "a.ts" in result
        assert "b.tsx" in result
        assert "c.py" not in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_glob_no_matches(self, tmp_workspace: Path):
        tool = GlobTool()
        result = await tool.execute(pattern="*.md", path=str(tmp_workspace))
        assert "No files found" in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_glob_truncation(self, tmp_workspace: Path):
        for i in range(10):
            (tmp_workspace / f"file{i}.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace), limit=5)
        assert "truncated" in result.lower()
        py_lines = [ln for ln in result.splitlines() if ln.endswith(".py")]
        assert len(py_lines) == 5

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_git_dir_excluded(self, tmp_workspace: Path):
        (tmp_workspace / ".git").mkdir()
        (tmp_workspace / ".git" / "config").write_text("x")
        (tmp_workspace / "real.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*", path=str(tmp_workspace))
        assert "real.py" in result
        assert "config" not in result
        assert ".git" not in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_hidden_files_included(self, tmp_workspace: Path):
        (tmp_workspace / ".hidden").write_text("x")
        (tmp_workspace / "visible.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*", path=str(tmp_workspace))
        assert ".hidden" in result
        assert "visible.py" in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_gitignore_respected(self, tmp_workspace: Path):
        import subprocess

        subprocess.run(["git", "init"], cwd=str(tmp_workspace), capture_output=True, timeout=5)
        (tmp_workspace / ".gitignore").write_text("*.log\n")
        (tmp_workspace / "app.py").write_text("x")
        (tmp_workspace / "debug.log").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*", path=str(tmp_workspace))
        assert "app.py" in result
        assert "debug.log" not in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_forward_slash_paths(self, tmp_workspace: Path):
        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "sub" / "file.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="**/*.py", path=str(tmp_workspace))
        assert "sub/file.py" in result
        assert "sub\\file.py" not in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_cartesian_brace_expansion(self, tmp_workspace: Path):
        (tmp_workspace / "src").mkdir()
        (tmp_workspace / "test").mkdir()
        (tmp_workspace / "src" / "a.ts").write_text("x")
        (tmp_workspace / "test" / "b.ts").write_text("x")
        (tmp_workspace / "other.ts").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="{src,test}/**/*.ts", path=str(tmp_workspace))
        assert "a.ts" in result
        assert "b.ts" in result
        assert "other.ts" not in result

    @pytest.mark.usefixtures("rg_env")
    @pytest.mark.asyncio
    async def test_subdirectory_anchor(self, tmp_workspace: Path):
        (tmp_workspace / "src").mkdir()
        (tmp_workspace / "src" / "direct.ts").write_text("x")
        (tmp_workspace / "src" / "sub").mkdir()
        (tmp_workspace / "src" / "sub" / "nested.ts").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="src/*.ts", path=str(tmp_workspace))
        assert "direct.ts" in result
        assert "nested.ts" not in result


# ---------------------------------------------------------------------------
# Python fallback — rg+fd hidden via no_rg_env
# ---------------------------------------------------------------------------


class TestGlobPythonFallback:
    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_basic_glob(self, tmp_workspace: Path):
        (tmp_workspace / "a.py").write_text("x")
        (tmp_workspace / "b.py").write_text("x")
        (tmp_workspace / "c.txt").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace))
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_recursive(self, tmp_workspace: Path):
        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "sub" / "deep.py").write_text("x")
        (tmp_workspace / "root.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="**/*.py", path=str(tmp_workspace))
        assert "root.py" in result
        assert "deep.py" in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_no_matches(self, tmp_workspace: Path):
        tool = GlobTool()
        result = await tool.execute(pattern="*.md", path=str(tmp_workspace))
        assert "No files found" in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_truncation(self, tmp_workspace: Path):
        for i in range(10):
            (tmp_workspace / f"file{i}.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace), limit=5)
        assert "truncated" in result.lower()
        py_lines = [ln for ln in result.splitlines() if ln.endswith(".py")]
        assert len(py_lines) == 5

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_brace_expansion(self, tmp_workspace: Path):
        (tmp_workspace / "a.ts").write_text("x")
        (tmp_workspace / "b.tsx").write_text("x")
        (tmp_workspace / "c.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.{ts,tsx}", path=str(tmp_workspace))
        assert "a.ts" in result
        assert "b.tsx" in result
        assert "c.py" not in result
        assert "not supported" not in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_cartesian_brace_expansion(self, tmp_workspace: Path):
        (tmp_workspace / "src").mkdir()
        (tmp_workspace / "test").mkdir()
        (tmp_workspace / "src" / "a.ts").write_text("x")
        (tmp_workspace / "test" / "b.ts").write_text("x")
        (tmp_workspace / "other.ts").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="{src,test}/**/*.ts", path=str(tmp_workspace))
        assert "a.ts" in result
        assert "b.ts" in result
        assert "other.ts" not in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_gitignore_respected(self, tmp_workspace: Path):
        (tmp_workspace / ".gitignore").write_text("*.log\nbuild/\n")
        (tmp_workspace / "app.py").write_text("x")
        (tmp_workspace / "debug.log").write_text("x")
        (tmp_workspace / "build").mkdir()
        (tmp_workspace / "build" / "output.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="**/*", path=str(tmp_workspace))
        assert "app.py" in result
        assert "debug.log" not in result
        assert "output.py" not in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_excludes_heavy_dirs(self, tmp_workspace: Path):
        (tmp_workspace / "node_modules").mkdir()
        (tmp_workspace / "node_modules" / "dep.py").write_text("x")
        (tmp_workspace / "__pycache__").mkdir()
        (tmp_workspace / "__pycache__" / "mod.cpython-312.pyc").write_text("x")
        (tmp_workspace / "real.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="**/*.py", path=str(tmp_workspace))
        assert "real.py" in result
        assert "dep.py" not in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_python_forward_slash_paths(self, tmp_workspace: Path):
        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "sub" / "file.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="**/*.py", path=str(tmp_workspace))
        assert "sub/file.py" in result
        assert "sub\\file.py" not in result


# ---------------------------------------------------------------------------
# Fallback rollback — verify the chain works correctly
# ---------------------------------------------------------------------------


class TestGlobFallbackRollback:
    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_falls_to_python_when_rg_unavailable(self, tmp_workspace: Path):
        (tmp_workspace / "test.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace))
        assert "test.py" in result
        assert "No files found" not in result

    @pytest.mark.asyncio
    async def test_falls_to_python_when_rg_returns_error(self, tmp_workspace: Path):
        (tmp_workspace / "test.py").write_text("x")
        tool = GlobTool()
        error_result = RipgrepResult(lines=[], truncated=False, error="simulated failure")
        with patch.object(RipgrepBackend, "available", return_value=True), \
             patch.object(RipgrepBackend, "list_files", return_value=error_result), \
             patch("modex_agent.tools.standard.glob_tool.shutil.which", return_value=None):
            result = await tool.execute(pattern="*.py", path=str(tmp_workspace))
        assert "test.py" in result

    @pytest.mark.asyncio
    async def test_rg_used_when_available(self, tmp_workspace: Path):
        (tmp_workspace / "rg_found.py").write_text("x")
        tool = GlobTool()
        rg_result = RipgrepResult(lines=["rg_found.py"], truncated=False, error=None)
        with patch.object(RipgrepBackend, "available", return_value=True), \
             patch.object(RipgrepBackend, "list_files", return_value=rg_result) as mock_list:
            result = await tool.execute(pattern="*.py", path=str(tmp_workspace))
        mock_list.assert_called_once()
        assert "rg_found.py" in result


# ---------------------------------------------------------------------------
# Edge cases — backend-independent
# ---------------------------------------------------------------------------


class TestGlobEdgeCases:
    @pytest.mark.asyncio
    async def test_path_not_found(self, tmp_workspace: Path):
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_file_path_rejected(self, tmp_workspace: Path):
        (tmp_workspace / "file.txt").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace / "file.txt"))
        assert "must be a directory" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_directory(self, tmp_workspace: Path):
        tool = GlobTool()
        result = await tool.execute(pattern="*", path=str(tmp_workspace))
        assert "No files found" in result

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_limit_clamped_to_max(self, tmp_workspace: Path):
        for i in range(250):
            (tmp_workspace / f"f{i}.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace), limit=999)
        py_lines = [ln for ln in result.splitlines() if ln.endswith(".py")]
        assert len(py_lines) <= 200

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_limit_clamped_to_min(self, tmp_workspace: Path):
        (tmp_workspace / "a.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace), limit=0)
        assert "a.py" in result

    def test_tool_name(self):
        assert GlobTool().name == "glob"

    def test_tool_description_nonempty(self):
        assert len(GlobTool().description) > 50

    def test_tool_parameters_schema(self):
        params = GlobTool().parameters
        assert params["type"] == "object"
        assert "pattern" in params["properties"]
        assert "path" in params["properties"]
        assert "limit" in params["properties"]
        assert params["required"] == ["pattern"]

    def test_tool_path_has_default(self):
        params = GlobTool().parameters
        path_prop = params["properties"]["path"]
        assert path_prop["default"] == "."

    @pytest.mark.usefixtures("no_rg_env")
    @pytest.mark.asyncio
    async def test_glob_string_limit(self, tmp_workspace: Path):
        for i in range(10):
            (tmp_workspace / f"f{i}.py").write_text("x")
        tool = GlobTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace), limit="5")
        assert "truncated" in result.lower()

    def test_tool_has_limit_param(self):
        params = GlobTool().parameters
        limit_prop = params["properties"]["limit"]
        assert limit_prop["default"] == 100
        assert limit_prop["maximum"] == 200
        assert limit_prop["minimum"] == 1
