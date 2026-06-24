"""Search tools: file content search and file discovery.

Auto-detects the fastest available backend per platform:
  - grep (SearchFilesTool): ripgrep (rg) > git grep > Python re
  - find  (FindFilesTool):  fd > Python pathlib.rglob
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...core.tool_manager import Tool

# Directories excluded from search by all backends.
DEFAULT_EXCLUDES = [
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
]


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

    def __init__(self) -> None:
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
                    "description": "Search pattern (regex or literal text)",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: current directory)",
                    "default": ".",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob filter for files, e.g. '*.py' (default: all files)",
                    "default": "*",
                },
                "regex": {
                    "type": "boolean",
                    "description": "If true, query is a regex; if false, literal text match",
                    "default": True,
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum matches to return (default: 50, hard cap: {self.ABSOLUTE_MAX_RESULTS})",
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
            "required": ["query"],
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
            return f"Error: Path not found: {path}"
        if not search_path.is_file() and not search_path.is_dir():
            return f"Error: Not a file or directory: {path}"

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
        # When given a file, check the directory that contains it.
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
    # ripgrep backend — uses --vimgrep for simple line-based output
    # ------------------------------------------------------------------

    async def _search_with_ripgrep(
        self,
        query: str,
        search_path: Path,
        file_pattern: str,
        regex: bool,
        max_results: int,
        context_lines: int,
    ) -> str:
        is_file = search_path.is_file()
        cmd = [
            "rg",
            "--vimgrep",
            f"-C{context_lines}",
            "--max-count",
            str(max_results),
            "--max-filesize",
            "10M",
        ]
        for d in DEFAULT_EXCLUDES:
            cmd.extend(["--glob", f"!{d}"])
        if not is_file and file_pattern != "*":
            # ripgrep --glob matches against the full relative path.
            # A pattern like "sub/*.py" does NOT match "search_root/sub/x.py"
            # unless prefixed with "**/".  Python rglob("sub/*.py") *does* match
            # nested "sub/*.py" paths, so we normalise to keep backends consistent.
            rg_pattern = file_pattern
            if "/" in rg_pattern and not rg_pattern.startswith("**/"):
                rg_pattern = f"**/{rg_pattern}"
            cmd.extend(["--glob", rg_pattern])
        if not regex:
            cmd.append("--fixed-strings")
        cmd.extend([query, str(search_path)])

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

    # Pre-compiled regex for vimgrep output: matches :lnum:col:text from the
    # right so Windows absolute paths (e.g. F:\path\file.py) are handled.
    _VIMGREP_RE = re.compile(r":(\d+):(\d+):(.*)$")

    def _parse_vimgrep(self, line: str) -> tuple[str, int, str] | None:
        """Parse one line of --vimgrep output: file:lnum:col:text"""
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
        """Parse --vimgrep output into grouped results.

        vimgrep format: file:lnum:col:text  (one line per match)
        With -C, context lines are also in vimgrep format but have '-' lnum prefix
        or just plain file:lnum:col:text lines around matches.
        We group consecutive lines from the same file into match blocks.
        """
        # Collect all parsed lines
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
        shown = 0

        # Group by file, preserving order
        current_file = ""
        for file_path, lnum, text in entries:
            if shown >= max_results:
                break
            if file_path != current_file:
                if current_file:
                    lines.append("")  # blank line between files
                current_file = file_path
                lines.append(f"{file_path}:")
            lines.append(f"  {lnum:4d} | {text}")
            shown += 1

        total = len(entries)
        header = f"Found {total} match{'es' if total != 1 else ''}:"
        result_lines = [header, ""] + lines

        if total > max_results:
            result_lines.append(
                f"[... {total - max_results} more matches not shown (limit: {max_results})]"
            )

        return "\n".join(result_lines)

    # ------------------------------------------------------------------
    # git grep backend — -C provides context lines natively
    # ------------------------------------------------------------------

    async def _search_with_git_grep(
        self,
        query: str,
        search_path: Path,
        file_pattern: str,
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
            git_file_pattern = file_pattern

        cmd = [
            "git",
            "-C",
            str(git_dir),
            "grep",
            "-n",
            f"-C{context_lines}",
            "--untracked",
        ]
        if regex:
            cmd.append("-E")  # Extended regex (| is alternation, not literal)
        else:
            cmd.append("-F")  # Fixed string (literal match)
        cmd.extend(["-e", query, "--", git_file_pattern])

        try:
            proc = await _async_subprocess_run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode not in (0, 1):
                return f"Error: git grep failed (exit {proc.returncode}): {proc.stderr[:200]}"
            return self._parse_git_grep_output(proc.stdout, max_results)
        except subprocess.TimeoutExpired:
            return "Error: git grep search timed out after 30 seconds"
        except Exception as e:
            return f"Error: git grep execution failed: {e}"

    def _parse_git_grep_output(
        self,
        stdout: str,
        max_results: int,
    ) -> str:
        if not stdout.strip():
            return "No matches found."

        # git grep -C output format:
        #   file:lnum:text        (match line)
        #   file-lnum-text        (context line, uses '-' separator)
        entries: list[tuple[str, int, str]] = []

        for raw_line in stdout.splitlines():
            # Try match format first (file:lnum:text)
            parsed = self._parse_git_grep_line(raw_line)
            if parsed:
                entries.append(parsed)

        if not entries:
            return "No matches found."

        return self._format_vimgrep_entries(entries, max_results)

    # Pre-compiled regex for git grep match lines: :lnum:text from the right.
    _GIT_GREP_RE = re.compile(r":(\d+):(.*)$")

    @staticmethod
    def _parse_git_grep_line(raw_line: str) -> tuple[str, int, str] | None:
        """Parse git grep -C output line.

        Match lines:  file:lnum:text
        Context lines: file-lnum-text  (dash separator, negative lnum marker)

        Uses regex so Windows absolute paths (e.g. ``F:\\path\\file.py:lnum:text``)
        are parsed correctly and colons inside the text are preserved.
        """
        # Try match format first (file:lnum:text)
        m = SearchFilesTool._GIT_GREP_RE.search(raw_line)
        if m:
            try:
                lnum = int(m.group(1))
            except ValueError:
                return None
            file_path = raw_line[: m.start()]
            return file_path, lnum, m.group(2).rstrip("\n\r")

        # Context format: file-lnum-text  or  file--lnum-text (negative lnum)
        dash_idx = raw_line.find("-")
        if dash_idx < 0:
            return None
        # Skip past the first dash; if the next char is also a dash it means
        # a negative line number (context before the match) — skip that too.
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
    # Python re fallback — skips excluded directories
    # ------------------------------------------------------------------

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

        exclude_set = set(DEFAULT_EXCLUDES)
        results: list[tuple[str, int, str, list[tuple[int, str]], list[tuple[int, str]]]] = []

        is_file = search_path.is_file()
        rel_root = search_path.parent if is_file else search_path
        files_to_search = [search_path] if is_file else search_path.rglob(file_pattern.lstrip("/"))

        for file_path in files_to_search:
            if not file_path.is_file():
                continue
            # Skip files inside excluded directories
            if exclude_set & set(file_path.parts):
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
                        for ln, txt in all_lines[max(0, i - context_lines) : i]
                    ]
                    ctx_after = [
                        (ln, txt.rstrip("\n\r"))
                        for ln, txt in all_lines[i + 1 : min(len(all_lines), i + 1 + context_lines)]
                    ]
                    rel_path = str(
                        file_path.relative_to(rel_root)
                        if file_path.is_relative_to(rel_root)
                        else file_path
                    )
                    results.append(
                        (rel_path, line_no, line_text.rstrip("\n\r"), ctx_before, ctx_after)
                    )
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

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "find"

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
                    "description": "Glob pattern to match file names, e.g. '*.py' or '**/*test*.py'",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: current directory)",
                    "default": ".",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"Maximum files to return (default: 100, hard cap: {self.ABSOLUTE_MAX_RESULTS})",
                    "default": 100,
                    "minimum": 1,
                    "maximum": self.ABSOLUTE_MAX_RESULTS,
                },
            },
            "required": ["pattern"],
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

    async def _find_with_fd(
        self,
        pattern: str,
        search_path: Path,
        max_results: int,
    ) -> str:
        cmd = ["fd", "--type", "f", "--max-results", str(max_results)]
        for d in DEFAULT_EXCLUDES:
            cmd.extend(["--exclude", d])
        cmd.extend([pattern, str(search_path)])
        try:
            proc = await _async_subprocess_run(cmd, capture_output=True, text=True, timeout=30)
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
        exclude_set = set(DEFAULT_EXCLUDES)
        files: list[str] = []
        for file_path in search_path.rglob(pattern.lstrip("/")):
            if file_path.is_file():
                if exclude_set & set(file_path.parts):
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
            lines.append(f"[... {total - max_results} more files not shown (limit: {max_results})]")
        return "\n".join(lines)
