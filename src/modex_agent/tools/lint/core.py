"""Standalone linter subsystem — language-agnostic single-file lint backends.

Provides:
- :class:`LintIssue` — a single diagnostic (frozen Pydantic value object).
  Only ``message`` and ``source`` are required; structured linters (ruff,
  mypy, tsc) also fill ``line``/``column``/``severity``/``code``, while
  unstructured linters leave them at defaults.
- :class:`LintResult` — the outcome of linting one file with one linter
  (status: ok / unavailable / error, plus issues list).
- :class:`FileLinter` — ABC for a single-file lint backend. Implementations
  declare ``supports(path)`` and implement ``lint(path)``.
- :class:`LintRegistry` — multi-match registry. A file may be handled by
  **multiple** linters concurrently (e.g. ruff + mypy for .py). All matching
  linters run in parallel via ``asyncio.gather``; results are merged and
  sorted by ``(line, column)`` with ``line=0`` (unstructured) sorting last.
- :class:`RuffLinter` — the built-in Python linter (``ruff check``).
- :data:`default_lint_registry` — module-level registry with ``RuffLinter``
  pre-registered. Additional built-in linters (mypy, biome, shellcheck,
  golangci-lint, clippy, yamllint, markdownlint, pmd) are registered by
  :mod:`modex_agent.tools.lint` on import. Bot layers may ``register()``
  additional linters or remove existing ones at startup.

Fail-open contract: if a linter is not installed, times out, or crashes,
the registry returns results from surviving linters (or an empty list if
none survive). Lint failures never propagate as errors to the caller.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Maximum seconds to wait for a single linter subprocess.
_LINT_TIMEOUT_S: float = 15.0

# Maximum issues to return from a single linter (truncation guard).
_MAX_ISSUES_PER_LINTER: int = 50


# ── Value objects ───────────────────────────────────────────────────────────


class LintIssue(BaseModel):
    """A single lint diagnostic.

    Compatible with both structured and unstructured linter output:

    - **Structured** (ruff, mypy, tsc): fills ``line``, ``column``,
      ``severity``, ``code`` alongside ``message`` and ``source``.
    - **Unstructured** (shellcheck raw output, custom linters): fills only
      ``message`` and ``source``; other fields default to ``0`` / ``"info"``
      / ``""``, signalling "unknown".

    The ``source`` field identifies which linter produced this issue, so
    merged results from multiple linters stay attributable.
    """

    model_config = ConfigDict(frozen=True)

    message: str
    """The diagnostic message (only required field besides source)."""

    source: str
    """Which linter produced this issue (e.g. ``"ruff"``, ``"mypy"``)."""

    line: int = 0
    """1-based line number. ``0`` means unknown (unstructured output)."""

    column: int = 0
    """1-based column. ``0`` means unknown."""

    severity: str = "info"
    """``"error"``, ``"warning"``, or ``"info"``. Defaults to ``"info"``."""

    code: str = ""
    """Rule code (e.g. ``"F821"``). Empty means unknown."""


class LintResult(BaseModel):
    """Outcome of linting one file with one linter.

    ``status`` distinguishes three cases so the edit tool can report them
    differently:

    - ``"ok"``: linter ran successfully. Check ``issues`` for diagnostics.
    - ``"unavailable"``: linter could not run (not installed, timed out).
      ``issues`` is empty; ``message`` explains why.
    - ``"error"``: linter ran but reported an internal error. ``issues``
      may still contain partial results.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "unavailable", "error"]
    issues: list[LintIssue] = Field(default_factory=list)
    message: str = ""
    """Human-readable detail when ``status != "ok"``."""


# ── Shared subprocess helper ────────────────────────────────────────────────


async def run_lint_subprocess(
    binary: str,
    args: list[str],
    *,
    timeout: float = 15.0,
    merge_stderr: bool = False,
) -> tuple[str | None, str | None, int | None]:
    """Run a linter binary, return (stdout, stderr, exit_code).

    Fail-open: returns ``(None, None, None)`` if the binary is not on
    PATH, the subprocess times out, or any other error occurs.

    When ``merge_stderr=True``, stderr is redirected into stdout
    (``stderr=STDOUT``) and the returned ``stderr`` is ``None``. Use this
    for linters that write diagnostics to stderr (clippy,
    markdownlint-cli2).
    """
    resolved = shutil.which(binary)
    if resolved is None:
        return None, None, None
    try:
        stderr_pipe = (
            asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
        )
        proc = await asyncio.create_subprocess_exec(
            resolved,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_pipe,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except Exception as exc:
        logger.warning("Linter subprocess '%s' failed: %s", binary, exc)
        return None, None, None

    stdout_str = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr_str = (
        stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    )
    return stdout_str, stderr_str, proc.returncode


# ── FileLinter ABC ──────────────────────────────────────────────────────────


class FileLinter(ABC):
    """Abstract base for a single-file lint backend.

    Implementations declare which files they handle via :meth:`supports`
    (typically by extension) and perform the actual linting in
    :meth:`lint`. The :meth:`name` property identifies the linter in
    merged output (``"ruff"``, ``"mypy"``, etc.).

    Fail-open contract: ``lint()`` should catch expected failures (binary
    not found, timeout, file unreadable) and return a
    ``LintResult(status="unavailable")`` rather than raising. Unexpected
    exceptions are caught by :class:`LintRegistry` and treated as
    fail-open.
    """

    timeout: float = 15.0

    @property
    @abstractmethod
    def name(self) -> str:
        """Linter name for output attribution."""
        ...

    @abstractmethod
    def supports(self, path: Path) -> bool:
        """Whether this linter handles the given file (by extension/MIME)."""
        ...

    @abstractmethod
    async def lint(self, path: Path) -> LintResult:
        """Lint a single file. Fail-open: return ``unavailable`` on error."""
        ...


# ── LintRegistry ────────────────────────────────────────────────────────────


class LintRegistry:
    """Multi-match linter registry.

    A file may be handled by **multiple** linters — all matching linters
    run concurrently and their issues are merged. This supports scenarios
    like running both ruff and mypy on the same ``.py`` file.

    Registration order determines ``linter_names`` listing order and the
    output format label, but does not affect which linter wins (all
    matching linters run).

    Sorting: merged issues are sorted by ``(line, column)`` ascending;
    issues with ``line == 0`` (unstructured output) sort last.
    """

    def __init__(self) -> None:
        self._linters: list[FileLinter] = []

    def register(self, linter: FileLinter) -> None:
        """Register a linter. Idempotent by name (re-registration replaces)."""
        # Replace if a linter with the same name already exists
        self._linters = [ln for ln in self._linters if ln.name != linter.name]
        self._linters.append(linter)

    @property
    def linter_names(self) -> list[str]:
        """Names of all registered linters, in registration order."""
        return [ln.name for ln in self._linters]

    def _select(self, path: Path) -> list[FileLinter]:
        """Return all linters whose ``supports()`` returns True for *path*."""
        return [ln for ln in self._linters if ln.supports(path)]

    def lint_file(self, path: Path) -> list[LintIssue]:
        """Lint *path* with all matching linters, merge and sort results.

        Synchronous wrapper — runs the async linters via
        ``asyncio.run``. For use inside async tool ``execute()``, prefer
        :meth:`lint_file_async`.

        Returns:
            Merged, sorted list of issues from all matching linters.
            Empty if no linter matches or all linters failed (fail-open).
        """
        return asyncio.run(self.lint_file_async(path))

    async def lint_file_async(self, path: Path) -> list[LintIssue]:
        """Async variant — use from async tool ``execute()`` methods."""
        matched = self._select(path)
        if not matched:
            return []

        results = await asyncio.gather(
            *(ln.lint(path) for ln in matched), return_exceptions=True
        )

        issues: list[LintIssue] = []
        for r in results:
            if isinstance(r, Exception):
                # fail-open: a crashing linter doesn't break others
                logger.warning("Linter crashed: %s", r, exc_info=True)
                continue
            issues.extend(r.issues)

        # Sort: (line, column) ascending; line=0 (unstructured) sorts last
        issues.sort(key=lambda i: (i.line if i.line > 0 else float("inf"), i.column))
        return issues


# ── RuffLinter ──────────────────────────────────────────────────────────────

# ruff concise output format: "path:line:col: CODE message"
# Example: "src/foo.py:10:5: F821 Undefined name *bar*"
_RUFF_LINE_RE = re.compile(
    r"^(?P<path>.+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>\S+)\s+(?P<msg>.*)$"
)


class RuffLinter(FileLinter):
    """Built-in Python linter using ``ruff check``.

    Runs ``ruff check --output-format=concise <path>`` and parses the
    concise output format into structured :class:`LintIssue` objects with
    line, column, code, and severity.

    Fail-open: if ruff is not on PATH, times out, or crashes, returns
    ``LintResult(status="unavailable")``.
    """

    @property
    def name(self) -> str:
        return "ruff"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".py"

    async def lint(self, path: Path) -> LintResult:
        if not path.exists() or not path.is_file():
            return LintResult(
                status="unavailable", message=f"file not found: {path}"
            )
        stdout, _, _ = await run_lint_subprocess(
            "ruff", ["check", "--output-format=concise", str(path)],
            timeout=self.timeout,
        )
        if stdout is None:
            return LintResult(
                status="unavailable", message="ruff not found in PATH"
            )
        issues = self._parse_output(stdout, path)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str, path: Path) -> list[LintIssue]:
        """Parse ruff concise output into LintIssue list.

        Filters out ruff's summary lines (``All checks passed!``,
        ``Found N error(s).``) — these are not diagnostics.
        """
        # ruff summary lines that are not diagnostics
        _summary_prefixes = ("All checks passed", "Found ", "Fixed ", "[*] ")

        issues: list[LintIssue] = []
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            # Skip ruff summary lines
            if any(raw_line.startswith(p) for p in _summary_prefixes):
                continue
            match = _RUFF_LINE_RE.match(raw_line)
            if match:
                code = match.group("code")
                issues.append(LintIssue(
                    message=match.group("msg"),
                    source="ruff",
                    line=int(match.group("line")),
                    column=int(match.group("col")),
                    severity=_ruff_severity(code),
                    code=code,
                ))
            else:
                # Unparseable line → unstructured issue
                issues.append(LintIssue(
                    message=raw_line, source="ruff"
                ))
            if len(issues) >= _MAX_ISSUES_PER_LINTER:
                issues.append(LintIssue(
                    message=f"... ({_MAX_ISSUES_PER_LINTER} issues max, truncated)",
                    source="ruff",
                ))
                break
        return issues


def _ruff_severity(code: str) -> str:
    """Map ruff rule prefix to severity.

    Ruff rule codes: E/W (pycodestyle), F (pyflakes), N (pep8-naming),
    UP (pyupgrade), B (flake8-bugbear), SIM (flake8-simplify), etc.
    F-prefixed (pyflakes: undefined names, unused imports) are errors;
    E/W are warnings; everything else defaults to warning.
    """
    if code.startswith("F"):
        return "error"
    if code.startswith(("E", "W", "N", "UP", "B", "SIM")):
        return "warning"
    return "warning"


# ── Default registry ────────────────────────────────────────────────────────

#: Module-level default registry. Starts with only RuffLinter; additional
#: built-in linters are registered by ``tools/lint/__init__.py`` on import.
default_lint_registry: LintRegistry = LintRegistry()
default_lint_registry.register(RuffLinter())
