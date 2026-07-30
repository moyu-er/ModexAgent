"""Built-in linter implementations for common languages.

Each linter subclasses :class:`~modex_agent.tools.lint.core.FileLinter` and
uses the shared :func:`~modex_agent.tools.lint.core.run_lint_subprocess`
helper for fail-open subprocess invocation. All linters are cross-platform
(Mac + Windows) and work with zero config on single files.

Coverage:

- **mypy** — Python type checking (complements ruff)
- **biome** — JS/TS/JSON/CSS/GraphQL linting (concise reporter)
- **shellcheck** — Shell script linting (gcc format)
- **golangci-lint** — Go linting (requires go.mod context)
- **clippy** — Rust linting (JSON output via clippy-driver)
- **yamllint** — YAML linting (parsable format)
- **markdownlint-cli2** — Markdown linting (default stderr output)
- **pmd** — Java linting (quickstart ruleset)

All are registered in :data:`default_lint_registry` by default. Bot layers
may register additional linters or replace these at startup.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from modex_agent.tools.lint.core import (
    FileLinter,
    LintIssue,
    LintResult,
    run_lint_subprocess,
)

logger = logging.getLogger(__name__)

_MAX_ISSUES = 50


def _truncate(issues: list[LintIssue], source: str) -> list[LintIssue]:
    """Truncate issue list to _MAX_ISSUES, appending a truncation notice."""
    if len(issues) >= _MAX_ISSUES:
        issues.append(LintIssue(
            message=f"... ({_MAX_ISSUES} issues max, truncated)",
            source=source,
        ))
    return issues


# ── mypy ────────────────────────────────────────────────────────────────────

# mypy --show-column-numbers: "file:line:col: error: message [code]"
# Column may be absent for some messages.
_MYPY_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>error|note|warning):\s*(?P<message>.+?)\s*"
    r"\[(?P<code>[^\]]+)\]$"
)


class MypyLinter(FileLinter):
    """Python type checker using ``mypy --show-column-numbers``.

    Complements :class:`RuffLinter` — ruff catches style/syntax issues,
    mypy catches type errors. Both run concurrently on ``.py`` files.
    """

    timeout = 30.0

    @property
    def name(self) -> str:
        return "mypy"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".py"

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "mypy",
            ["--show-column-numbers", "--no-error-summary", str(path)],
            timeout=self.timeout,
        )
        if stdout is None:
            return LintResult(status="unavailable", message="mypy not found in PATH")
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        issues: list[LintIssue] = []
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            if raw_line.startswith(("Success:", "Found ", "There are ")):
                continue
            match = _MYPY_LINE_RE.match(raw_line)
            if match:
                issues.append(LintIssue(
                    message=match.group("message"),
                    source="mypy",
                    line=int(match.group("line")),
                    column=int(match.group("col") or 0),
                    severity="error" if match.group("severity") == "error" else "warning",
                    code=match.group("code"),
                ))
            else:
                issues.append(LintIssue(message=raw_line, source="mypy"))
        return _truncate(issues, "mypy")


# ── biome ───────────────────────────────────────────────────────────────────


class BiomeLinter(FileLinter):
    """JS/TS/JSON/CSS linter using ``biome lint --reporter=json``.

    Zero-config — uses biome's recommended defaults. Covers .js, .ts,
    .jsx, .tsx, .json, .css, .graphql. Parses the JSON ``diagnostics``
    array for structured output.
    """

    _SUFFIXES = frozenset({".js", ".ts", ".jsx", ".tsx", ".json", ".css", ".graphql"})

    @property
    def name(self) -> str:
        return "biome"

    def supports(self, path: Path) -> bool:
        return path.suffix in self._SUFFIXES

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "biome",
            ["lint", "--reporter=json", str(path)],
            timeout=self.timeout,
            merge_stderr=True,
        )
        if stdout is None:
            return LintResult(status="unavailable", message="biome not found in PATH")
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        import json as json_mod

        start = stdout.find("{")
        if start == -1:
            return []
        try:
            data = json_mod.loads(stdout[start:])
        except json_mod.JSONDecodeError:
            return [LintIssue(message="biome: failed to parse JSON output", source="biome")]
        diagnostics = data.get("diagnostics", [])
        issues: list[LintIssue] = []
        for d in diagnostics:
            severity = d.get("severity", "info")
            category = d.get("category", "")
            message = d.get("message", "")
            loc = d.get("location", {})
            span = loc.get("span", [{}])[0] if loc.get("span") else {}
            issues.append(LintIssue(
                message=message,
                source="biome",
                line=span.get("line_start", 0),
                column=span.get("column_start", 0),
                severity="error" if severity == "error" else "warning",
                code=category,
            ))
        return _truncate(issues, "biome")


# ── shellcheck ──────────────────────────────────────────────────────────────

# shellcheck -f gcc: "file:line:col: type: message [SC####]"
_SHELLCHECK_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<severity>error|warning|info|note|style):\s*(?P<message>.+?)\s*"
    r"\[(?P<code>SC\d+)\]$"
)


class ShellcheckLinter(FileLinter):
    """Shell script linter using ``shellcheck -f gcc``.

    Covers .sh, .bash, .zsh. Zero-config.
    """

    _SUFFIXES = frozenset({".sh", ".bash", ".zsh"})

    @property
    def name(self) -> str:
        return "shellcheck"

    def supports(self, path: Path) -> bool:
        return path.suffix in self._SUFFIXES

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "shellcheck", ["-f", "gcc", str(path)],
            timeout=self.timeout,
        )
        if stdout is None:
            return LintResult(status="unavailable", message="shellcheck not found in PATH")
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        issues: list[LintIssue] = []
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            match = _SHELLCHECK_LINE_RE.match(raw_line)
            if match:
                issues.append(LintIssue(
                    message=match.group("message"),
                    source="shellcheck",
                    line=int(match.group("line")),
                    column=int(match.group("col")),
                    severity=match.group("severity"),
                    code=match.group("code"),
                ))
            else:
                issues.append(LintIssue(message=raw_line, source="shellcheck"))
        return _truncate(issues, "shellcheck")


# ── golangci-lint ───────────────────────────────────────────────────────────

# golangci-lint: "file:line:col: message (linter)"
_GOLANGCI_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+?)\s*"
    r"\((?P<linter>[^)]+)\)$"
)


class GolangciLintLinter(FileLinter):
    """Go linter using ``golangci-lint run``.

    Requires Go module context (``go.mod`` in parent dirs). Cannot lint
    standalone ``.go`` files outside a module — fail-open handles this.
    """

    @property
    def name(self) -> str:
        return "golangci-lint"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".go"

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "golangci-lint",
            ["run", "--output.text.print-issued-lines=false", str(path)],
            timeout=self.timeout,
        )
        if stdout is None:
            return LintResult(
                status="unavailable", message="golangci-lint not found in PATH"
            )
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        issues: list[LintIssue] = []
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            match = _GOLANGCI_LINE_RE.match(raw_line)
            if match:
                issues.append(LintIssue(
                    message=match.group("message"),
                    source="golangci-lint",
                    line=int(match.group("line")),
                    column=int(match.group("col")),
                    severity="warning",
                    code=match.group("linter"),
                ))
            else:
                issues.append(LintIssue(message=raw_line, source="golangci-lint"))
        return _truncate(issues, "golangci-lint")


# ── clippy ──────────────────────────────────────────────────────────────────

# clippy-driver --error-format json: one JSON object per line on stdout
# Each object has: message, code, level, spans[{file_name, line_start, column_start}]


class ClippyLinter(FileLinter):
    """Rust linter using ``clippy-driver --error-format json``.

    Uses ``clippy-driver`` (not ``cargo clippy``) for single-file linting
    without requiring a Cargo project. Passes ``-D warnings`` to treat
    warnings as errors for exit-code purposes.
    """

    timeout = 30.0

    @property
    def name(self) -> str:
        return "clippy"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".rs"

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "clippy-driver",
            ["--edition", "2021", "--error-format", "json", "-D", "warnings", str(path)],
            timeout=self.timeout,
            merge_stderr=True,
        )
        if stdout is None:
            return LintResult(
                status="unavailable", message="clippy-driver not found in PATH"
            )
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        issues: list[LintIssue] = []
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line or not raw_line.startswith("{"):
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            level = obj.get("level", "warning")
            message = obj.get("message", "")
            code_obj = obj.get("code")
            code = code_obj.get("code", "") if isinstance(code_obj, dict) else ""
            spans = obj.get("spans", [])
            primary = next((s for s in spans if s.get("is_primary")), None)
            if primary:
                issues.append(LintIssue(
                    message=message,
                    source="clippy",
                    line=primary.get("line_start", 0),
                    column=primary.get("column_start", 0),
                    severity="error" if level == "error" else "warning",
                    code=code,
                ))
            elif code:
                issues.append(LintIssue(message=message, source="clippy", code=code))
            # Skip diagnostics without spans AND without code (e.g. "aborting due to N previous error")
        return _truncate(issues, "clippy")


# ── yamllint ────────────────────────────────────────────────────────────────

# yamllint -f parsable: "file:line:col: [level] message (rule)"
_YAMLLINT_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*"
    r"\[(?P<severity>error|warning)\]\s*(?P<message>.+?)\s*"
    r"\((?P<rule>[^)]+)\)$"
)


class YamllintLinter(FileLinter):
    """YAML linter using ``yamllint -f parsable``.

    Covers .yml, .yaml. Zero-config (built-in default config).
    """

    _SUFFIXES = frozenset({".yml", ".yaml"})

    @property
    def name(self) -> str:
        return "yamllint"

    def supports(self, path: Path) -> bool:
        return path.suffix in self._SUFFIXES

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "yamllint", ["-f", "parsable", str(path)],
            timeout=self.timeout,
        )
        if stdout is None:
            return LintResult(status="unavailable", message="yamllint not found in PATH")
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        issues: list[LintIssue] = []
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            match = _YAMLLINT_LINE_RE.match(raw_line)
            if match:
                issues.append(LintIssue(
                    message=match.group("message"),
                    source="yamllint",
                    line=int(match.group("line")),
                    column=int(match.group("col")),
                    severity=match.group("severity"),
                    code=match.group("rule"),
                ))
            else:
                issues.append(LintIssue(message=raw_line, source="yamllint"))
        return _truncate(issues, "yamllint")


# ── markdownlint-cli2 ───────────────────────────────────────────────────────

# markdownlint-cli2: "file:line(:col)? severity rule message [detail]"
# Output goes to stderr. Column is optional.
_MARKDOWNLINT_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?\s+"
    r"(?P<severity>error|warning)\s+"
    r"(?P<rule>MD\d+(?:/[\w-]+)*)\s+"
    r"(?P<message>.+?)(?:\s*\[.*\])?$"
)


class MarkdownlintLinter(FileLinter):
    """Markdown linter using ``markdownlint-cli2``.

    Covers .md, .markdown. Zero-config (all rules with default settings).
    Output goes to stderr, so ``merge_stderr=True`` is used.
    """

    _SUFFIXES = frozenset({".md", ".markdown"})

    @property
    def name(self) -> str:
        return "markdownlint"

    def supports(self, path: Path) -> bool:
        return path.suffix in self._SUFFIXES

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "markdownlint-cli2",
            [str(path)],
            timeout=self.timeout,
            merge_stderr=True,
        )
        if stdout is None:
            return LintResult(
                status="unavailable", message="markdownlint-cli2 not found in PATH"
            )
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        _skip_prefixes = (
            "markdownlint-cli2 v",
            "Finding:",
            "Linting:",
            "Summary:",
        )
        issues: list[LintIssue] = []
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            if any(raw_line.startswith(p) for p in _skip_prefixes):
                continue
            match = _MARKDOWNLINT_LINE_RE.match(raw_line)
            if match:
                issues.append(LintIssue(
                    message=match.group("message"),
                    source="markdownlint",
                    line=int(match.group("line")),
                    column=int(match.group("col") or 0),
                    severity=match.group("severity"),
                    code=match.group("rule"),
                ))
            else:
                issues.append(LintIssue(message=raw_line, source="markdownlint"))
        return _truncate(issues, "markdownlint")


# ── pmd ─────────────────────────────────────────────────────────────────────

# PMD text format: "file:line  RuleName  message"
# Field separation is 2+ spaces or tab. No column info in text format.
_PMD_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)\s+(?P<rule>\S+)\s+(?P<message>.+)$"
)


class PmdLinter(FileLinter):
    """Java linter using ``pmd check`` with the built-in quickstart ruleset.

    Uses ``rulesets/java/quickstart.xml`` for zero-config analysis. Covers
    ``.java`` files. Requires Java runtime (JDK 8+) and PMD on PATH
    (``pmd`` on Mac/Linux, ``pmd.bat`` on Windows — ``shutil.which``
    resolves both).
    """

    timeout = 30.0

    @property
    def name(self) -> str:
        return "pmd"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".java"

    async def lint(self, path: Path) -> LintResult:
        stdout, _, _ = await run_lint_subprocess(
            "pmd",
            ["check", "-d", str(path), "-f", "text", "-R", "rulesets/java/quickstart.xml"],
            timeout=self.timeout,
        )
        if stdout is None:
            return LintResult(status="unavailable", message="pmd not found in PATH")
        issues = self._parse_output(stdout)
        return LintResult(status="ok", issues=issues)

    @staticmethod
    def _parse_output(stdout: str) -> list[LintIssue]:
        issues: list[LintIssue] = []
        _skip_prefixes = ("Cause:", "Error while", "No rules", "PMDException:")
        for raw_line in stdout.strip().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            if any(raw_line.startswith(p) for p in _skip_prefixes):
                continue
            match = _PMD_LINE_RE.match(raw_line)
            if match:
                issues.append(LintIssue(
                    message=match.group("message"),
                    source="pmd",
                    line=int(match.group("line")),
                    severity="warning",
                    code=match.group("rule"),
                ))
            else:
                issues.append(LintIssue(message=raw_line, source="pmd"))
        return _truncate(issues, "pmd")


# ── CompositeLinter ─────────────────────────────────────────────────────────


class CompositeLinter(FileLinter):
    """Aggregate multiple linters behind a single :class:`FileLinter` facade.

    Useful when you want a single ``FileLinter`` instance that delegates
    to a set of child linters — e.g. registering one ``CompositeLinter``
    into a :class:`LintRegistry` instead of N individual linters.

    ``supports()`` returns True if **any** child supports the file.
    ``lint()`` runs all supporting children concurrently and merges
    results (same semantics as :meth:`LintRegistry.lint_file_async`).

    The ``name`` property joins child names with ``+`` (e.g. ``"ruff+mypy"``).
    """

    def __init__(self, linters: list[FileLinter]) -> None:
        if not linters:
            raise ValueError("CompositeLinter requires at least one child linter")
        self._children = linters

    @property
    def name(self) -> str:
        return "+".join(ln.name for ln in self._children)

    def supports(self, path: Path) -> bool:
        return any(ln.supports(path) for ln in self._children)

    async def lint(self, path: Path) -> LintResult:
        import asyncio

        matched = [ln for ln in self._children if ln.supports(path)]
        if not matched:
            return LintResult(status="ok")
        results = await asyncio.gather(
            *(ln.lint(path) for ln in matched), return_exceptions=True
        )
        issues: list[LintIssue] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("CompositeLinter child crashed: %s", r)
                continue
            issues.extend(r.issues)
        issues.sort(key=lambda i: (i.line if i.line > 0 else float("inf"), i.column))
        return LintResult(status="ok", issues=_truncate(issues, self.name))
