"""File content search tool.

Auto-detects the fastest available backend per platform:
  - grep (SearchFilesTool): ripgrep (rg) > git grep > Python re

File discovery (glob) lives in ``glob_tool.py``.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...core.tool_manager import ParallelTool
from ._fallback import DEFAULT_EXCLUDES, expand_braces, is_ignored, load_gitignore

_MAX_FILE_SIZE = 10 * 1024 * 1024


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


class SearchFilesTool(ParallelTool):
    ABSOLUTE_MAX_RESULTS = 200

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "- Fast content search tool that works with any codebase size\n"
            "- Searches file contents using regular expressions\n"
            '- Supports full regex syntax (eg. "log.*Error", "function\\s+\\w+", etc.)\n'
            '- Filter files by pattern with the include parameter (eg. "*.js", "*.{ts,tsx}")\n'
            "- Returns matching lines with file paths and line numbers\n"
            "- Use this tool when you need to find files containing specific patterns\n"
            "- If you need to identify/count the number of matches within files, "
            "use the Bash tool with rg directly if available. "
            "Do NOT use grep for counting.\n"
            "- When you are doing an open-ended search that may require multiple rounds "
            "of globbing and grepping, consider delegating to a subagent instead\n"
            "- You can call multiple tools in a single response — batch speculative "
            "searches that are potentially useful"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The regex pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "The directory or file to search in. "
                        "Defaults to the current working directory."
                    ),
                    "default": ".",
                },
                "include": {
                    "type": "string",
                    "description": (
                        'File pattern to include in the search '
                        '(e.g. "*.js", "*.{ts,tsx}"). Defaults to all files.'
                    ),
                    "default": "*",
                },
                "regex": {
                    "type": "boolean",
                    "description": (
                        "If true (default), pattern is a regex; "
                        "if false, literal text match"
                    ),
                    "default": True,
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"Maximum matches to return "
                        f"(default: 50, hard cap: {self.ABSOLUTE_MAX_RESULTS})"
                    ),
                    "default": 50,
                    "minimum": 1,
                    "maximum": self.ABSOLUTE_MAX_RESULTS,
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Lines of context before/after each match (default: 2)",
                    "default": 2,
                    "minimum": 0,
                    "maximum": 10,
                },
            },
            "required": ["pattern"],
        }

    async def execute(self, **kwargs: Any) -> str:
        pattern = kwargs["pattern"]
        path = kwargs.get("path", ".")
        include = kwargs.get("include", "*")
        regex = kwargs.get("regex", True)
        max_results = kwargs.get("max_results", 50)
        context_lines = kwargs.get("context_lines", 2)
        # Coerce string-typed params that LLM providers may send as strings
        # (JSON numbers occasionally arrive as strings depending on the
        # provider's function-calling implementation).  Without this,
        # min("4", 200) raises TypeError on Python 3.
        if isinstance(max_results, str):
            max_results = int(max_results)
        if isinstance(context_lines, str):
            context_lines = int(context_lines)
        if isinstance(regex, str):
            regex = regex.lower() in ("true", "1", "yes")

        search_path = _resolve_path(path)
        if not search_path.exists():
            return f"Error: Path not found: {path}"
        if not search_path.is_file() and not search_path.is_dir():
            return f"Error: Not a file or directory: {path}"

        max_results = min(max_results, self.ABSOLUTE_MAX_RESULTS)

        if shutil.which("rg") is not None:
            result = await self._search_with_ripgrep(
                pattern, search_path, include, regex, max_results, context_lines
            )
            if not result.startswith("Error:"):
                return result

        if await self._is_git_repo(search_path):
            result = await self._search_with_git_grep(
                pattern, search_path, include, regex, max_results, context_lines
            )
            if not result.startswith("Error:"):
                return result

        return await self._search_with_python(
            pattern, search_path, include, regex, max_results, context_lines
        )

    async def _is_git_repo(self, search_path: Path) -> bool:
        if shutil.which("git") is None:
            return False
        check_path = search_path if search_path.is_dir() else search_path.parent
        try:
            proc = await _async_subprocess_run(
                ["git", "-C", str(check_path), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return proc.returncode == 0 and proc.stdout.strip() != ""
        except Exception:
            return False

    # ------------------------------------------------------------------
    # ripgrep backend
    # ------------------------------------------------------------------

    async def _search_with_ripgrep(
        self,
        pattern: str,
        search_path: Path,
        include: str,
        regex: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        is_file = search_path.is_file()
        cmd = [
            "rg",
            "--no-config",
            "--hidden",
            "--vimgrep",
            f"-C{context_lines}",
            "--max-count", str(max_results),
            "--max-filesize", "10M",
        ]
        if not is_file and include != "*":
            rg_pattern = include
            if "/" in rg_pattern and not rg_pattern.startswith("**/"):
                rg_pattern = f"**/{rg_pattern}"
            cmd.extend(["--glob", rg_pattern])
        cmd.extend(["--glob", "!**/.git/**"])
        if not regex:
            cmd.append("--fixed-strings")
        cmd.extend([pattern, str(search_path)])

        try:
            proc = await _async_subprocess_run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode not in (0, 1):
                return f"Error: ripgrep failed (exit {proc.returncode}): {proc.stderr[:200]}"
            if not proc.stdout.strip():
                return "No matches found."
            return self._parse_vimgrep_output(proc.stdout, max_results)
        except subprocess.TimeoutExpired:
            return "Error: ripgrep search timed out after 30 seconds"
        except Exception as e:
            return f"Error: ripgrep execution failed: {e}"

    _VIMGREP_RE = re.compile(r":(\d+):(\d+):(.*)$")

    def _parse_vimgrep(self, line: str) -> tuple[str, int, str] | None:
        m = self._VIMGREP_RE.search(line)
        if not m:
            return None
        try:
            lnum = int(m.group(1))
        except ValueError:
            return None
        file_path = line[: m.start()]
        return file_path, lnum, m.group(3).rstrip("\n\r")

    def _parse_vimgrep_output(self, stdout: str, max_results: int) -> str:
        entries: list[tuple[str, int, str]] = []
        for raw_line in stdout.splitlines():
            parsed = self._parse_vimgrep(raw_line)
            if parsed:
                entries.append(parsed)

        if not entries:
            return "No matches found."

        return self._format_vimgrep_entries(entries, max_results)

    def _format_vimgrep_entries(
        self,
        entries: list[tuple[str, int, str]],
        max_results: int,
    ) -> str:
        lines: list[str] = []
        current_file = ""
        for shown, (file_path, lnum, text) in enumerate(entries):
            if shown >= max_results:
                break
            if file_path != current_file:
                if current_file:
                    lines.append("")
                current_file = file_path
                lines.append(f"{file_path}:")
            lines.append(f"  {lnum:4d} | {text}")

        total = len(entries)
        header = f"Found {total} match{'es' if total != 1 else ''}:"
        result_lines = [header, ""] + lines

        if total > max_results:
            result_lines.append(
                f"[... {total - max_results} more matches not shown (limit: {max_results})]"
            )

        return "\n".join(result_lines)

    # ------------------------------------------------------------------
    # git grep backend
    # ------------------------------------------------------------------

    async def _search_with_git_grep(
        self,
        pattern: str,
        search_path: Path,
        include: str,
        regex: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        is_file = search_path.is_file()
        if is_file:
            repo_root_proc = await _async_subprocess_run(
                ["git", "-C", str(search_path.parent), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if repo_root_proc.returncode != 0:
                return f"Error: git rev-parse failed: {repo_root_proc.stderr[:200]}"
            git_dir = Path(repo_root_proc.stdout.strip())
            try:
                git_file_pattern = search_path.relative_to(git_dir).as_posix()
            except ValueError:
                git_file_pattern = str(search_path)
        else:
            git_dir = search_path
            git_file_pattern = include

        cmd = [
            "git", "-C", str(git_dir), "grep",
            "-n", f"-C{context_lines}", "--untracked",
        ]
        if regex:
            cmd.append("-E")
        else:
            cmd.append("-F")
        cmd.extend(["-e", pattern, "--", git_file_pattern])

        try:
            proc = await _async_subprocess_run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode not in (0, 1):
                return f"Error: git grep failed (exit {proc.returncode}): {proc.stderr[:200]}"
            return self._parse_git_grep_output(proc.stdout, max_results)
        except subprocess.TimeoutExpired:
            return "Error: git grep search timed out after 30 seconds"
        except Exception as e:
            return f"Error: git grep execution failed: {e}"

    def _parse_git_grep_output(self, stdout: str, max_results: int) -> str:
        if not stdout.strip():
            return "No matches found."

        entries: list[tuple[str, int, str]] = []
        for raw_line in stdout.splitlines():
            parsed = self._parse_git_grep_line(raw_line)
            if parsed:
                entries.append(parsed)

        if not entries:
            return "No matches found."

        return self._format_vimgrep_entries(entries, max_results)

    _GIT_GREP_RE = re.compile(r":(\d+):(.*)$")

    @staticmethod
    def _parse_git_grep_line(raw_line: str) -> tuple[str, int, str] | None:
        m = SearchFilesTool._GIT_GREP_RE.search(raw_line)
        if m:
            try:
                lnum = int(m.group(1))
            except ValueError:
                return None
            file_path = raw_line[: m.start()]
            return file_path, lnum, m.group(2).rstrip("\n\r")

        dash_idx = raw_line.find("-")
        if dash_idx < 0:
            return None
        num_start = dash_idx + 1
        if num_start < len(raw_line) and raw_line[num_start] == "-":
            num_start += 1
        second_dash = raw_line.find("-", num_start)
        if second_dash < 0:
            return None
        try:
            lnum = abs(int(raw_line[num_start:second_dash]))
        except (ValueError, IndexError):
            return None
        file_path = raw_line[:dash_idx]
        text = raw_line[second_dash + 1 :]
        return file_path, lnum, text.rstrip("\n\r")

    # ------------------------------------------------------------------
    # Python re fallback
    # ------------------------------------------------------------------

    async def _search_with_python(
        self,
        pattern: str,
        search_path: Path,
        include: str,
        regex: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        try:
            compiled = re.compile(pattern) if regex else re.compile(re.escape(pattern))
        except re.error as e:
            return f"Error: Invalid regex pattern: {e}"

        include_patterns = expand_braces(include) if include != "*" else ["*"]
        positive_gi, negative_gi = load_gitignore(
            search_path if search_path.is_dir() else search_path.parent
        )
        exclude_set = set(DEFAULT_EXCLUDES)

        is_file = search_path.is_file()

        if is_file:
            files_to_search: list[Path] = [search_path]
        else:
            files_to_search = []
            seen_paths: set[Path] = set()
            for inc in include_patterns:
                inc = inc.lstrip("/")
                for fp in search_path.rglob(inc):
                    if fp.is_file() and fp not in seen_paths:
                        seen_paths.add(fp)
                        files_to_search.append(fp)

        results: list[tuple[str, int, str, list[tuple[int, str]], list[tuple[int, str]]]] = []

        for file_path in files_to_search:
            if exclude_set & set(file_path.parts):
                continue
            try:
                rel = file_path.relative_to(search_path if not is_file else search_path.parent)
            except ValueError:
                rel = file_path
            rel_str = str(rel).replace("\\", "/")
            if is_ignored(rel_str, rel.parts, positive_gi, negative_gi):
                continue
            if _is_binary(file_path):
                continue
            try:
                if file_path.stat().st_size > _MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            try:
                with file_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                    all_lines = list(enumerate(f, start=1))
            except (OSError, PermissionError):
                continue

            for i, (line_no, line_text) in enumerate(all_lines):
                if compiled.search(line_text):
                    ctx_before = [
                        (ln, txt.rstrip("\n\r"))
                        for ln, txt in all_lines[max(0, i - context_lines) : i]
                    ]
                    ctx_after = [
                        (ln, txt.rstrip("\n\r"))
                        for ln, txt in all_lines[i + 1 : min(len(all_lines), i + 1 + context_lines)]
                    ]
                    results.append(
                        (rel_str, line_no, line_text.rstrip("\n\r"), ctx_before, ctx_after)
                    )
                    if len(results) >= max_results:
                        return self._format_results(results, max_results)

        if not results:
            return "No matches found."

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

        current_file = ""
        for file_path, line_no, text, ctx_before, ctx_after in results[:max_results]:
            if file_path != current_file:
                if current_file:
                    lines.append("")
                current_file = file_path
                lines.append(f"{file_path}:")
            for ctx_ln, ctx_txt in ctx_before:
                lines.append(f"  {ctx_ln:4d} | {ctx_txt}")
            lines.append(f"  {line_no:4d} | {text}")
            for ctx_ln, ctx_txt in ctx_after:
                lines.append(f"  {ctx_ln:4d} | {ctx_txt}")

        if total > max_results:
            lines.append(
                f"[... {total - max_results} more matches not shown (limit: {max_results})]"
            )

        return "\n".join(lines)
