# Search Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `search_files` and `find_files` standard tools to ModexAgent, with cross-platform backend selection (ripgrep/git grep/Python re for search, fd/pathlib for find), paginated results, and integration into bot_project's main agent, peers, and subagents.

**Architecture:** Two new `Tool` subclasses in `framework/tools/standard/search_tool.py`. Each tool auto-detects the fastest available backend at runtime (rg/git/fd if installed, else pure Python). Results are formatted as structured text with pagination caps. Registration happens in `builders.py` alongside existing file/shell tools.

**Tech Stack:** Python 3.11+, `pathlib`, `re`, `subprocess`, `shutil.which`, `fnmatch`, `tempfile` (for tests)

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `framework/tools/standard/search_tool.py` | **Create** | `SearchFilesTool` + `FindFilesTool` implementations with backend detection |
| `framework/tools/standard/__init__.py` | **Modify** | Export `SearchFilesTool`, `FindFilesTool` |
| `examples/bot_project/bot/service/builders.py` | **Modify** | Register search tools in `_register_tools()` and `_build_peer_tool_manager()` |
| `examples/bot_project/config/bot_config.yml` | **Modify** | Add `search_tools` config section |
| `tests/unit/tools/test_search_tools.py` | **Create** | Unit tests for both tools including backend fallback and pagination |

---

## Reference: Existing Patterns

Before touching code, read these files to understand patterns:

- `framework/tools/standard/file_tool.py` — How `ReadFileTool`/`ListDirTool` are structured (property-based schema, async execute, error messages)
- `framework/tools/standard/shell_tool.py` — How `ShellTool` handles cross-platform concerns
- `tests/unit/tools/test_standard_tools.py` — Test patterns (`pytest.mark.asyncio`, `tmp_workspace` fixture, assertion style)
- `examples/bot_project/bot/service/builders.py:48-85` — `_register_tools()` registration pattern
- `examples/bot_project/bot/service/builders.py:187-238` — `_build_peer_tool_manager()` peer tool registration pattern

---

## Task 1: Create `SearchFilesTool` and `FindFilesTool`

**Files:**
- Create: `framework/tools/standard/search_tool.py`

**Background:** This file contains two tool classes. `SearchFilesTool` searches file contents with auto-detected backend. `FindFilesTool` discovers files by glob pattern with auto-detected backend. Both enforce pagination caps and return structured text.

- [ ] **Step 1: Create `framework/tools/standard/search_tool.py` with both tool classes**

```python
"""Search tools: file content search and file discovery.

Provides coding-agent-style search capabilities without requiring shell access.
Auto-detects fastest available backend per platform.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...core.tool_manager import Tool


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _resolve_path(path: str) -> Path:
    """Resolve path string to absolute Path."""
    return Path(path).expanduser().resolve()


def _is_binary(file_path: Path, sample_size: int = 8192) -> bool:
    """Check if file is binary by looking for null bytes in the first chunk."""
    try:
        with file_path.open("rb") as f:
            chunk = f.read(sample_size)
            if b"\x00" in chunk:
                return True
    except (OSError, PermissionError):
        return True
    return False


# --------------------------------------------------------------------------- #
# SearchFilesTool
# --------------------------------------------------------------------------- #

class SearchFilesTool(Tool):
    """Search file contents for a text pattern or regular expression."""

    # Backend availability caches (class-level, lazy)
    _has_rg: bool | None = None
    _has_git: bool | None = None

    # Hard caps to prevent token overflow
    ABSOLUTE_MAX_RESULTS = 200

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return (
            "Search file contents for a pattern (regex or literal text). "
            "Returns matching lines with file paths, line numbers, and context. "
            "Uses ripgrep when available for performance, falls back to Python re."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search pattern (regex or literal text)"
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                    "default": "."
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob filter for files, e.g. '*.py' (default: all files)",
                    "default": "*"
                },
                "regex": {
                    "type": "boolean",
                    "description": "If true, query is a regex; if false, literal text match",
                    "default": True
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum matches to return (default: 50, hard cap: {self.ABSOLUTE_MAX_RESULTS})",
                    "default": 50,
                    "minimum": 1,
                    "maximum": self.ABSOLUTE_MAX_RESULTS
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context before/after each match (default: 2)",
                    "default": 2,
                    "minimum": 0,
                    "maximum": 10
                }
            },
            "required": ["query"]
        }

    async def execute(
        self,
        query: str,
        path: str = ".",
        file_pattern: str = "*",
        regex: bool = True,
        max_results: int = 50,
        context_lines: int = 2,
        **kwargs: Any
    ) -> str:
        search_path = _resolve_path(path)
        if not search_path.exists():
            return f"Error: Directory not found: {path}"
        if not search_path.is_dir():
            return f"Error: Not a directory: {path}"

        max_results = min(max_results, self.ABSOLUTE_MAX_RESULTS)

        # Detect backends
        if self._has_rg is None:
            self._has_rg = shutil.which("rg") is not None
        if self._has_git is None:
            self._has_git = shutil.which("git") is not None

        # Try backends in order: rg > git grep > Python re
        if self._has_rg:
            return await self._search_with_ripgrep(
                query, search_path, file_pattern, regex, max_results, context_lines
            )

        if self._has_git and (search_path / ".git").exists():
            result = await self._search_with_git_grep(
                query, search_path, file_pattern, regex, max_results, context_lines
            )
            if not result.startswith("Error:"):
                return result

        return await self._search_with_python(
            query, search_path, file_pattern, regex, max_results, context_lines
        )

    # -- ripgrep backend --

    async def _search_with_ripgrep(
        self,
        query: str,
        search_path: Path,
        file_pattern: str,
        regex: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        cmd = [
            "rg",
            "--json",
            "--context", str(context_lines),
            "--max-count", str(max_results),
            "--glob", file_pattern,
        ]
        if not regex:
            cmd.append("--fixed-strings")
        cmd.extend([query, str(search_path)])

        try:
            proc = await subprocess_run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode not in (0, 1):  # rg returns 1 when no matches
                return f"Error: ripgrep failed: {proc.stderr}"
            return self._parse_rg_json(proc.stdout, max_results)
        except Exception as e:
            return f"Error: ripgrep execution failed: {e}"

    def _parse_rg_json(self, stdout: str, max_results: int) -> str:
        """Parse ripgrep --json output into structured text."""
        import json

        lines = stdout.strip().splitlines()
        if not lines:
            return "No matches found."

        results: list[tuple[str, int, str]] = []  # (file, line, text)
        current_file = ""
        context_before: list[str] = []
        context_after: list[str] = []
        match_line = ""
        match_line_no = 0

        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            if msg_type == "begin":
                current_file = data.get("data", {}).get("path", {}).get("text", "")
            elif msg_type == "match":
                mdata = data.get("data", {})
                match_line_no = mdata.get("line_number", 0)
                texts = mdata.get("lines", {}).get("text", "")
                if texts:
                    match_line = texts.rstrip("\n\r")
            elif msg_type == "context":
                cdata = data.get("data", {})
                c_line_no = cdata.get("line_number", 0)
                c_text = cdata.get("lines", {}).get("text", "").rstrip("\n\r")
                if match_line_no == 0:
                    context_before.append((c_line_no, c_text))
                else:
                    context_after.append((c_line_no, c_text))
            elif msg_type == "end":
                if match_line_no > 0:
                    results.append((current_file, match_line_no, match_line, context_before.copy(), context_after.copy()))
                context_before = []
                context_after = []
                match_line = ""
                match_line_no = 0

        if not results:
            return "No matches found."

        return self._format_results(results, max_results)

    # -- git grep backend --

    async def _search_with_git_grep(
        self,
        query: str,
        search_path: Path,
        file_pattern: str,
        regex: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        cmd = [
            "git", "-C", str(search_path),
            "grep", "-n",
            "--untracked",
        ]
        if context_lines > 0:
            cmd.extend([f"-C{context_lines}"])
        if not regex:
            cmd.append("-F")
        cmd.extend(["-e", query, "--", file_pattern])

        try:
            proc = await subprocess_run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode not in (0, 1):
                return f"Error: git grep failed: {proc.stderr}"
            return self._parse_git_grep(proc.stdout, max_results)
        except Exception as e:
            return f"Error: git grep execution failed: {e}"

    def _parse_git_grep(self, stdout: str, max_results: int) -> str:
        """Parse git grep output into structured text."""
        lines = stdout.strip().splitlines()
        if not lines:
            return "No matches found."

        # git grep -C output groups matches with context
        results: list[tuple[str, int, str]] = []
        current_file = ""

        for line in lines[:max_results * 5]:  # generous buffer
            if line.startswith("--"):
                continue
            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_path, line_no_str, text = parts[0], parts[1], parts[2]
                try:
                    line_no = int(line_no_str)
                except ValueError:
                    continue
                if file_path != current_file:
                    current_file = file_path
                results.append((file_path, line_no, text))

        return self._format_simple_results(results, max_results)

    # -- Python re backend --

    async def _search_with_python(
        self,
        query: str,
        search_path: Path,
        file_pattern: str,
        regex: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        try:
            if regex:
                pattern = re.compile(query)
            else:
                pattern = re.compile(re.escape(query))
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        results: list[tuple[str, int, str, list[tuple[int, str]], list[tuple[int, str]]]] = []

        for file_path in search_path.rglob(file_pattern.lstrip("/")):
            if not file_path.is_file():
                continue
            if _is_binary(file_path):
                continue

            try:
                with file_path.open("r", encoding="utf-8", errors="replace") as f:
                    all_lines = list(enumerate(f, start=1))
            except (OSError, PermissionError):
                continue

            for i, (line_no, line_text) in enumerate(all_lines):
                if pattern.search(line_text):
                    # Gather context
                    ctx_before = [
                        (ln, txt.rstrip("\n\r"))
                        for ln, txt in all_lines[max(0, i - context_lines):i]
                    ]
                    ctx_after = [
                        (ln, txt.rstrip("\n\r"))
                        for ln, txt in all_lines[i + 1:min(len(all_lines), i + 1 + context_lines)]
                    ]
                    results.append((
                        str(file_path.relative_to(search_path) if file_path.is_relative_to(search_path) else file_path),
                        line_no,
                        line_text.rstrip("\n\r"),
                        ctx_before,
                        ctx_after,
                    ))
                    if len(results) >= max_results:
                        return self._format_results(results, max_results)

        if not results:
            return f"No matches found for '{query}' in {search_path}"

        return self._format_results(results, max_results)

    # -- Formatting --

    def _format_results(
        self,
        results: list,
        max_results: int,
    ) -> str:
        """Format search results with context lines."""
        lines: list[str] = []
        total = len(results)
        shown = min(total, max_results)

        lines.append(f"Found {total} match{'es' if total != 1 else ''}:")
        lines.append("")

        for file_path, line_no, text, ctx_before, ctx_after in results[:max_results]:
            lines.append(f"{file_path}:{line_no}")
            for ctx_line_no, ctx_text in ctx_before:
                lines.append(f"  {ctx_line_no:4d} | {ctx_text}")
            lines.append(f"> {line_no:4d} | {text}")
            for ctx_line_no, ctx_text in ctx_after:
                lines.append(f"  {ctx_line_no:4d} | {ctx_text}")
            lines.append("")

        if total > max_results:
            lines.append(f"[... {total - max_results} more matches not shown (limit: {max_results})]")

        return "\n".join(lines)

    def _format_simple_results(
        self,
        results: list[tuple[str, int, str]],
        max_results: int,
    ) -> str:
        """Format simple line-by-line results (for git grep)."""
        lines: list[str] = []
        total = len(results)
        shown = min(total, max_results)

        lines.append(f"Found {total} match{'es' if total != 1 else ''}:")
        lines.append("")

        for file_path, line_no, text in results[:max_results]:
            lines.append(f"{file_path}:{line_no}: {text}")

        if total > max_results:
            lines.append(f"[... {total - max_results} more matches not shown (limit: {max_results})]")

        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# FindFilesTool
# --------------------------------------------------------------------------- #

class FindFilesTool(Tool):
    """Find files by name pattern (glob syntax)."""

    _has_fd: bool | None = None
    ABSOLUTE_MAX_RESULTS = 500

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return (
            "Find files matching a glob pattern within a directory tree. "
            "Uses 'fd' when available for performance, falls back to Python pathlib. "
            "Pattern examples: '*.py', '**/*test*.py', '*.md'"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match file names, e.g. '*.py' or '**/*test*.py'"
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                    "default": "."
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum files to return (default: 100, hard cap: {self.ABSOLUTE_MAX_RESULTS})",
                    "default": 100,
                    "minimum": 1,
                    "maximum": self.ABSOLUTE_MAX_RESULTS
                }
            },
            "required": ["pattern"]
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        max_results: int = 100,
        **kwargs: Any
    ) -> str:
        search_path = _resolve_path(path)
        if not search_path.exists():
            return f"Error: Directory not found: {path}"
        if not search_path.is_dir():
            return f"Error: Not a directory: {path}"

        max_results = min(max_results, self.ABSOLUTE_MAX_RESULTS)

        if self._has_fd is None:
            self._has_fd = shutil.which("fd") is not None

        if self._has_fd:
            return await self._find_with_fd(pattern, search_path, max_results)

        return await self._find_with_python(pattern, search_path, max_results)

    async def _find_with_fd(
        self,
        pattern: str,
        search_path: Path,
        max_results: int,
    ) -> str:
        # fd uses glob patterns by default
        cmd = ["fd", "--max-results", str(max_results), pattern, str(search_path)]
        try:
            proc = await subprocess_run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                return f"Error: fd failed: {proc.stderr}"
            files = [line.strip() for line in proc.stdout.strip().splitlines() if line.strip()]
            return self._format_find_results(files, max_results)
        except Exception as e:
            return f"Error: fd execution failed: {e}"

    async def _find_with_python(
        self,
        pattern: str,
        search_path: Path,
        max_results: int,
    ) -> str:
        files: list[str] = []
        for file_path in search_path.rglob(pattern.lstrip("/")):
            if file_path.is_file():
                rel_path = str(file_path.relative_to(search_path) if file_path.is_relative_to(search_path) else file_path)
                files.append(rel_path)
                if len(files) >= max_results:
                    break

        if not files:
            return f"No files matching '{pattern}' found in {search_path}"

        return self._format_find_results(files, max_results)

    def _format_find_results(self, files: list[str], max_results: int) -> str:
        total = len(files)
        lines: list[str] = [
            f"Found {total} file{'s' if total != 1 else ''} matching '{self._last_pattern}':"
            if hasattr(self, '_last_pattern') else
            f"Found {total} file{'s' if total != 1 else ''}:"
        ]
        lines.append("")
        for f in files[:max_results]:
            lines.append(f)
        if total > max_results:
            lines.append(f"[... {total - max_results} more files not shown (limit: {max_results})]")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Async subprocess helper (for use in async execute methods)
# --------------------------------------------------------------------------- #

import asyncio

async def subprocess_run(*args, **kwargs):
    """Run a subprocess asynchronously and return CompletedProcess-like result."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: subprocess.run(*args, **kwargs))
```

**Note:** The `_format_find_results` references `self._last_pattern` but it's not set. Fix this in the next step by storing the pattern before formatting.

- [ ] **Step 2: Fix the `_format_find_results` bug — store pattern for display**

In `FindFilesTool._find_with_fd` and `FindFilesTool._find_with_python`, before calling `_format_find_results`, set `self._last_pattern = pattern`. Or better, pass pattern as argument.

Change `_format_find_results` signature:
```python
def _format_find_results(self, files: list[str], max_results: int, pattern: str = "") -> str:
    total = len(files)
    lines: list[str] = [
        f"Found {total} file{'s' if total != 1 else ''} matching '{pattern}':"
        if pattern else
        f"Found {total} file{'s' if total != 1 else ''}:"
    ]
    ...
```

And update callers:
```python
# In _find_with_fd:
return self._format_find_results(files, max_results, pattern)

# In _find_with_python:
return self._format_find_results(files, max_results, pattern)
```

---

## Task 2: Export New Tools from `__init__.py`

**Files:**
- Modify: `framework/tools/standard/__init__.py`

- [ ] **Step 1: Add imports and update `__all__`**

```python
# Add to imports:
from .search_tool import FindFilesTool, SearchFilesTool

# Add to __all__:
__all__ = [
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "ListDirTool",
    "FileTool",
    "ShellTool",
    "SearchFilesTool",   # NEW
    "FindFilesTool",     # NEW
]
```

---

## Task 3: Register Search Tools in bot_project

**Files:**
- Modify: `examples/bot_project/bot/service/builders.py`

### 3.1 Main Agent Registration

- [ ] **Step 1: Add search tool registration in `_register_tools()`**

After shell_tools registration (around line 80), add:

```python
        search_tools_config = tools_config.get("search_tools", {})
        if search_tools_config.get("enabled", True):
            from framework.tools.standard import SearchFilesTool, FindFilesTool
            self.tool_manager.register(SearchFilesTool())
            self.tool_manager.register(FindFilesTool())
            print("   [OK] Search tools registered (search_files, find_files)")
```

### 3.2 Peer/Subagent Registration

- [ ] **Step 2: Add search tool registration in `_build_peer_tool_manager()`**

After shell_tools registration (around line 217), add:

```python
        search_tools_config = tools_config.get("search_tools", {})
        if search_tools_config.get("enabled", True):
            from framework.tools.standard import SearchFilesTool, FindFilesTool
            tm.register(SearchFilesTool())
            tm.register(FindFilesTool())
```

---

## Task 4: Add Config to `bot_config.yml`

**Files:**
- Modify: `examples/bot_project/config/bot_config.yml`

- [ ] **Step 1: Add `search_tools` section under `tools:`**

Insert before the `mcp_tools` section (around line 379):

```yaml
  # Search tools: file content search and file discovery
  search_tools:
    enabled: true
```

---

## Task 5: Write Unit Tests

**Files:**
- Create: `tests/unit/tools/test_search_tools.py`

- [ ] **Step 1: Write tests for `FindFilesTool`**

```python
"""Unit tests for search tools: SearchFilesTool and FindFilesTool.

Tests backend fallback, pagination, regex/literal matching, and error handling.
"""

import tempfile
from pathlib import Path

import pytest

from framework.tools.standard.search_tool import FindFilesTool, SearchFilesTool


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


# ---------------------------------------------------------------------------
# FindFilesTool
# ---------------------------------------------------------------------------

class TestFindFilesTool:
    @pytest.mark.asyncio
    async def test_find_by_extension(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("x")
        (tmp_workspace / "b.py").write_text("x")
        (tmp_workspace / "c.txt").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace))
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result
        assert "Found 2 files" in result

    @pytest.mark.asyncio
    async def test_find_recursive(self, tmp_workspace):
        (tmp_workspace / "sub").mkdir()
        (tmp_workspace / "sub" / "deep.py").write_text("x")
        (tmp_workspace / "root.py").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="**/*.py", path=str(tmp_workspace))
        assert "root.py" in result
        assert "deep.py" in result

    @pytest.mark.asyncio
    async def test_find_no_matches(self, tmp_workspace):
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.md", path=str(tmp_workspace))
        assert "No files matching" in result

    @pytest.mark.asyncio
    async def test_find_pagination(self, tmp_workspace):
        for i in range(10):
            (tmp_workspace / f"file{i}.py").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace), max_results=5)
        assert "Found 10 files" in result
        assert "more files not shown" in result

    @pytest.mark.asyncio
    async def test_find_not_found_directory(self, tmp_workspace):
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_find_not_a_directory(self, tmp_workspace):
        (tmp_workspace / "file.txt").write_text("x")
        tool = FindFilesTool()
        result = await tool.execute(pattern="*.py", path=str(tmp_workspace / "file.txt"))
        assert "not a directory" in result.lower()
```

- [ ] **Step 2: Write tests for `SearchFilesTool`**

```python
# ---------------------------------------------------------------------------
# SearchFilesTool
# ---------------------------------------------------------------------------

class TestSearchFilesTool:
    @pytest.mark.asyncio
    async def test_search_literal_match(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("def hello():\n    pass\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="hello", path=str(tmp_workspace), regex=False)
        assert "code.py" in result
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_search_regex_match(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("def hello_world():\n    pass\n")
        tool = SearchFilesTool()
        result = await tool.execute(query=r"hello_\w+", path=str(tmp_workspace), regex=True)
        assert "code.py" in result
        assert "hello_world" in result

    @pytest.mark.asyncio
    async def test_search_file_pattern_filter(self, tmp_workspace):
        (tmp_workspace / "a.py").write_text("target\n")
        (tmp_workspace / "b.txt").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace), file_pattern="*.py")
        assert "a.py" in result
        assert "b.txt" not in result

    @pytest.mark.asyncio
    async def test_search_context_lines(self, tmp_workspace):
        (tmp_workspace / "code.py").write_text("line1\nline2\nline3\ntarget\nline5\nline6\nline7\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace), context_lines=2)
        assert "line1" in result
        assert "line2" in result
        assert "line3" in result
        assert ">" in result  # match marker
        assert "line5" in result
        assert "line6" in result
        assert "line7" in result

    @pytest.mark.asyncio
    async def test_search_no_matches(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(query="nonexistent", path=str(tmp_workspace))
        assert "No matches found" in result

    @pytest.mark.asyncio
    async def test_search_invalid_regex(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(query="[invalid", path=str(tmp_workspace), regex=True)
        assert "Invalid regex" in result

    @pytest.mark.asyncio
    async def test_search_pagination(self, tmp_workspace):
        for i in range(10):
            (tmp_workspace / f"file{i}.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace), max_results=5)
        assert "Found 10 matches" in result
        assert "more matches not shown" in result

    @pytest.mark.asyncio
    async def test_search_skips_binary(self, tmp_workspace):
        # Write a file with null bytes (binary)
        (tmp_workspace / "binary.dat").write_bytes(b"\x00\x01\x02target\x03")
        (tmp_workspace / "text.py").write_text("target\n")
        tool = SearchFilesTool()
        result = await tool.execute(query="target", path=str(tmp_workspace))
        assert "text.py" in result
        assert "binary.dat" not in result

    @pytest.mark.asyncio
    async def test_search_not_found_directory(self, tmp_workspace):
        tool = SearchFilesTool()
        result = await tool.execute(query="test", path=str(tmp_workspace / "missing"))
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_search_not_a_directory(self, tmp_workspace):
        (tmp_workspace / "file.txt").write_text("x")
        tool = SearchFilesTool()
        result = await tool.execute(query="test", path=str(tmp_workspace / "file.txt"))
        assert "not a directory" in result.lower()
```

- [ ] **Step 3: Run the new tests**

```bash
# From project root
pytest tests/unit/tools/test_search_tools.py -v
```

Expected: All tests PASS.

---

## Task 6: Run Full Test Suite

- [ ] **Step 1: Run all tool tests**

```bash
pytest tests/unit/tools/ -v
```

Expected: All tests in `test_standard_tools.py` and `test_search_tools.py` PASS.

- [ ] **Step 2: Run full unit test suite**

```bash
pytest tests/unit/ -v
```

Expected: No regressions. If failures occur, they must be pre-existing (not caused by search tools).

---

## Task 7: Verify bot_project Integration

- [ ] **Step 1: Dry-run bot_project config loading**

```bash
# From examples/bot_project directory
cd examples/bot_project
python -c "
from bot.utils.config_loader import ConfigLoader
from pathlib import Path
config = ConfigLoader(Path('config')).load_yaml('bot_config.yml')
print('tools.search_tools:', config.get('tools', {}).get('search_tools'))
"
```

Expected output shows `search_tools` config is loaded correctly.

- [ ] **Step 2: Verify tool registration logic compiles**

```bash
# From project root
python -c "
import sys
sys.path.insert(0, 'examples/bot_project')
from bot.service.builders import AgentBuilderMixin
print('builders.py imports OK')
"
```

Expected: No ImportError.

- [ ] **Step 3: Verify search tools can be instantiated and called**

```bash
python -c "
import asyncio
from framework.tools.standard import SearchFilesTool, FindFilesTool

async def test():
    s = SearchFilesTool()
    print('search_files name:', s.name)
    f = FindFilesTool()
    print('find_files name:', f.name)

asyncio.run(test())
"
```

Expected: Both tool names printed without errors.

---

## Self-Review Checklist

**1. Spec coverage:**
- [x] `search_files` tool with regex/literal, file_pattern, context_lines, pagination — Task 1
- [x] `find_files` tool with glob pattern, pagination — Task 1
- [x] Cross-platform backend selection (rg/git/fd/Python) — Task 1
- [x] Integration into bot_project main agent — Task 3.1
- [x] Integration into bot_project peers/subagents — Task 3.2
- [x] Config in bot_config.yml — Task 4
- [x] Unit tests for both tools — Task 5

**2. Placeholder scan:**
- [x] No "TBD", "TODO", "implement later"
- [x] All steps have complete code blocks
- [x] No vague "add error handling" — specific error messages and return values shown

**3. Type consistency:**
- [x] `SearchFilesTool` and `FindFilesTool` both inherit `Tool`
- [x] `execute()` signatures match parameter schemas
- [x] `max_results` hard caps consistent (`ABSOLUTE_MAX_RESULTS`)
- [x] `_resolve_path` helper used consistently (matches `file_tool.py` pattern)

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-06-search-tools-plan.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
