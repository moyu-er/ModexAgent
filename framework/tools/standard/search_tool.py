"""Search tools: file content search and file discovery.

Auto-detects the fastest available backend per platform:
  - search_files: ripgrep (rg) > git grep > Python re
  - find_files: fd > Python pathlib.rglob
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...core.tool_manager import Tool


def _resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _is_binary(file_path: Path, sample_size: int = 8192) -> bool:
    try:
        with file_path.open("rb") as f:
            chunk = f.read(sample_size)
            if b"\x00" in chunk:
                return True
    except (OSError, PermissionError):
        return True
    return False


async def _async_subprocess_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return await asyncio.to_thread(subprocess.run, *args, **kwargs)


class SearchFilesTool(Tool):
    ABSOLUTE_MAX_RESULTS = 200

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents for a pattern (regex or literal text), like the grep/ripgrep tool. "
            "Returns matching lines with file paths, line numbers, and context. "
            "Uses ripgrep when available for performance, falls back to git grep or Python re. "
            "Pattern: set regex=true for regex (default), regex=false for fixed string match."
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
        **kwargs: Any,
    ) -> str:
        search_path = _resolve_path(path)
        if not search_path.exists():
            return f"Error: Directory not found: {path}"
        if not search_path.is_dir():
            return f"Error: Not a directory: {path}"

        max_results = min(max_results, self.ABSOLUTE_MAX_RESULTS)

        has_rg = shutil.which("rg") is not None
        if has_rg:
            result = await self._search_with_ripgrep(
                query, search_path, file_pattern, regex, max_results, context_lines
            )
            if not result.startswith("Error:"):
                return result

        has_git = await self._is_git_repo(search_path)
        if has_git:
            result = await self._search_with_git_grep(
                query, search_path, file_pattern, regex, max_results, context_lines
            )
            if not result.startswith("Error:"):
                return result

        return await self._search_with_python(
            query, search_path, file_pattern, regex, max_results, context_lines
        )

    async def _is_git_repo(self, search_path: Path) -> bool:
        if shutil.which("git") is None:
            return False
        try:
            proc = await _async_subprocess_run(
                ["git", "-C", str(search_path), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return proc.returncode == 0 and proc.stdout.strip() != ""
        except Exception:
            return False

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
            "--heading",
            f"-C{context_lines}",
            "--max-count", str(max_results),
            "--glob", file_pattern,
            "--max-filesize", "10M",
        ]
        if not regex:
            cmd.append("--fixed-strings")
        cmd.extend([query, str(search_path)])

        try:
            proc = await _async_subprocess_run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if proc.returncode not in (0, 1):
                return f"Error: ripgrep failed (exit {proc.returncode}): {proc.stderr[:200]}"
            return self._parse_rg_output(proc.stdout, max_results)
        except subprocess.TimeoutExpired:
            return "Error: ripgrep search timed out after 30 seconds"
        except Exception as e:
            return f"Error: ripgrep execution failed: {e}"

    def _parse_rg_output(self, stdout: str, max_results: int) -> str:
        if not stdout.strip():
            return "No matches found."

        results: list[tuple[str, int, str, list[tuple[int, str]], list[tuple[int, str]]]] = []
        current_file = ""
        match_line_no = 0
        match_text = ""
        ctx_before: list[tuple[int, str]] = []
        ctx_after: list[tuple[int, str]] = []

        for line in stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            payload = data.get("data", {})

            if msg_type == "begin":
                current_file = payload.get("path", {}).get("text", "")

            elif msg_type == "match":
                match_line_no = payload.get("line_number", 0)
                match_text = payload.get("lines", {}).get("text", "").rstrip("\n\r")

            elif msg_type == "context":
                ctx_line_no = payload.get("line_number", 0)
                ctx_text = payload.get("lines", {}).get("text", "").rstrip("\n\r")
                if match_line_no == 0:
                    ctx_before.append((ctx_line_no, ctx_text))
                else:
                    ctx_after.append((ctx_line_no, ctx_text))

            elif msg_type == "end":
                if match_line_no > 0:
                    results.append((current_file, match_line_no, match_text, ctx_before, ctx_after))
                    if len(results) >= max_results:
                        break
                match_line_no = 0
                match_text = ""
                ctx_before = []
                ctx_after = []

        if match_line_no > 0 and len(results) < max_results:
            results.append((current_file, match_line_no, match_text, ctx_before, ctx_after))

        if not results:
            return "No matches found."

        return self._format_results(results, max_results)

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
        if regex:
            cmd.append("-E")  # Extended regex (| is alternation, not literal)
        else:
            cmd.append("-F")  # Fixed string (literal match)
        cmd.extend(["-e", query, "--", file_pattern])

        try:
            proc = await _async_subprocess_run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if proc.returncode not in (0, 1):
                return f"Error: git grep failed (exit {proc.returncode}): {proc.stderr[:200]}"
            return self._parse_git_grep_output(proc.stdout, max_results, context_lines, search_path)
        except subprocess.TimeoutExpired:
            return "Error: git grep search timed out after 30 seconds"
        except Exception as e:
            return f"Error: git grep execution failed: {e}"

    def _parse_git_grep_output(
        self,
        stdout: str,
        max_results: int,
        context_lines: int,
        search_path: Path,
    ) -> str:
        if not stdout.strip():
            return "No matches found."

        results: list[tuple[str, int, str, list[tuple[int, str]], list[tuple[int, str]]]] = []

        for line in stdout.strip().splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            try:
                line_no = int(parts[1])
            except ValueError:
                continue

            file_path = parts[0]
            match_text = parts[2]
            ctx_before, ctx_after = self._read_context(
                search_path / file_path, line_no, context_lines
            )
            results.append((file_path, line_no, match_text, ctx_before, ctx_after))
            if len(results) >= max_results:
                break

        if not results:
            return "No matches found."

        return self._format_results(results, max_results)

    def _read_context(
        self,
        file_path: Path,
        match_line: int,
        context_lines: int,
    ) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        try:
            with file_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                all_lines = list(enumerate(f, start=1))
        except (OSError, PermissionError):
            return [], []

        match_idx = match_line - 1
        if match_idx < 0 or match_idx >= len(all_lines):
            return [], []

        ctx_before = [
            (ln, txt.rstrip("\n\r"))
            for ln, txt in all_lines[max(0, match_idx - context_lines):match_idx]
        ]
        ctx_after = [
            (ln, txt.rstrip("\n\r"))
            for ln, txt in all_lines[match_idx + 1:min(len(all_lines), match_idx + 1 + context_lines)]
        ]
        return ctx_before, ctx_after

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
                with file_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                    all_lines = list(enumerate(f, start=1))
            except (OSError, PermissionError):
                continue

            for i, (line_no, line_text) in enumerate(all_lines):
                if pattern.search(line_text):
                    ctx_before = [
                        (ln, txt.rstrip("\n\r"))
                        for ln, txt in all_lines[max(0, i - context_lines):i]
                    ]
                    ctx_after = [
                        (ln, txt.rstrip("\n\r"))
                        for ln, txt in all_lines[i + 1:min(len(all_lines), i + 1 + context_lines)]
                    ]
                    rel_path = str(
                        file_path.relative_to(search_path)
                        if file_path.is_relative_to(search_path)
                        else file_path
                    )
                    results.append((rel_path, line_no, line_text.rstrip("\n\r"), ctx_before, ctx_after))
                    if len(results) >= max_results:
                        return self._format_results(results, max_results)

        if not results:
            return f"No matches found for '{query}' in {search_path}"

        return self._format_results(results, max_results)

    def _format_results(
        self,
        results: list[tuple[str, int, str, list[tuple[int, str]], list[tuple[int, str]]]],
        max_results: int,
    ) -> str:
        lines: list[str] = []
        total = len(results)

        lines.append(f"Found {total} match{'es' if total != 1 else ''}:")
        lines.append("")

        for file_path, line_no, text, ctx_before, ctx_after in results[:max_results]:
            lines.append(f"{file_path}:{line_no}")
            for ctx_ln, ctx_txt in ctx_before:
                lines.append(f"  {ctx_ln:4d} | {ctx_txt}")
            lines.append(f"> {line_no:4d} | {text}")
            for ctx_ln, ctx_txt in ctx_after:
                lines.append(f"  {ctx_ln:4d} | {ctx_txt}")
            lines.append("")

        if total > max_results:
            lines.append(
                f"[... {total - max_results} more matches not shown (limit: {max_results})]"
            )

        return "\n".join(lines)


class FindFilesTool(Tool):
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
        **kwargs: Any,
    ) -> str:
        search_path = _resolve_path(path)
        if not search_path.exists():
            return f"Error: Directory not found: {path}"
        if not search_path.is_dir():
            return f"Error: Not a directory: {path}"

        max_results = min(max_results, self.ABSOLUTE_MAX_RESULTS)

        has_fd = shutil.which("fd") is not None
        if has_fd:
            result = await self._find_with_fd(pattern, search_path, max_results)
            if not result.startswith("Error:"):
                return result

        return await self._find_with_python(pattern, search_path, max_results)

    _DEFAULT_EXCLUDES = [".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode"]

    async def _find_with_fd(
        self,
        pattern: str,
        search_path: Path,
        max_results: int,
    ) -> str:
        cmd = ["fd", "--max-results", str(max_results)]
        for d in self._DEFAULT_EXCLUDES:
            cmd.extend(["--exclude", d])
        cmd.extend([pattern, str(search_path)])
        try:
            proc = await _async_subprocess_run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if proc.returncode != 0:
                return f"Error: fd failed (exit {proc.returncode}): {proc.stderr[:200]}"
            files = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
            return self._format_results(files, max_results, pattern)
        except subprocess.TimeoutExpired:
            return "Error: fd search timed out after 30 seconds"
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
                # Skip excluded directories
                parts = set(file_path.parts)
                if parts & set(self._DEFAULT_EXCLUDES):
                    continue
                rel_path = str(
                    file_path.relative_to(search_path)
                    if file_path.is_relative_to(search_path)
                    else file_path
                )
                files.append(rel_path)
                if len(files) >= max_results:
                    break

        if not files:
            return f"No files matching '{pattern}' found in {search_path}"

        return self._format_results(files, max_results, pattern)

    def _format_results(self, files: list[str], max_results: int, pattern: str) -> str:
        total = len(files)
        lines: list[str] = [
            f"Found {total} file{'s' if total != 1 else ''} matching '{pattern}':",
            "",
        ]
        for f in files[:max_results]:
            lines.append(f)
        if total > max_results:
            lines.append(
                f"[... {total - max_results} more files not shown (limit: {max_results})]"
            )
        return "\n".join(lines)
