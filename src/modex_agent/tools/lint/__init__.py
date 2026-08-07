"""Linter subsystem — standalone single-file lint backend framework.

Provides a language-agnostic lint abstraction that can be used independently
of ACI tools. The ACI ``AciEditTool`` is just one consumer; bot layers or
other tools can use :class:`LintRegistry` directly.

Public API:

- :class:`FileLinter` — ABC for single-file lint backends
- :class:`LintRegistry` — multi-match registry (concurrent execution)
- :class:`LintIssue` / :class:`LintResult` — frozen Pydantic value objects
- :func:`run_lint_subprocess` — fail-open subprocess helper
- :data:`default_lint_registry` — registry with all built-in linters

Built-in linters (9 languages, all cross-platform, zero-config, fail-open):

- :class:`RuffLinter` — Python (lint + format)
- :class:`MypyLinter` — Python (type checking, complements ruff)
- :class:`BiomeLinter` — JS/TS/JSON/CSS/GraphQL
- :class:`ShellcheckLinter` — Shell scripts
- :class:`GolangciLintLinter` — Go
- :class:`ClippyLinter` — Rust
- :class:`YamllintLinter` — YAML
- :class:`MarkdownlintLinter` — Markdown
- :class:`PmdLinter` — Java

Usage::

    from modex_agent.tools.lint import default_lint_registry

    issues = await default_lint_registry.lint_file_async(Path("src/foo.py"))
    # issues is a merged, sorted list from ruff + mypy
"""

from __future__ import annotations

from modex_agent.tools.lint.builtins import (
    BiomeLinter,
    ClippyLinter,
    CompositeLinter,
    GolangciLintLinter,
    MarkdownlintLinter,
    MypyLinter,
    PmdLinter,
    ShellcheckLinter,
    YamllintLinter,
)
from modex_agent.tools.lint.core import (
    FileLinter,
    LintIssue,
    LintRegistry,
    LintResult,
    RuffLinter,
    default_lint_registry,
    run_lint_subprocess,
)

for _cls in (
    MypyLinter,
    BiomeLinter,
    ShellcheckLinter,
    GolangciLintLinter,
    ClippyLinter,
    YamllintLinter,
    MarkdownlintLinter,
    PmdLinter,
):
    default_lint_registry.register(_cls())

__all__ = [
    "BiomeLinter",
    "ClippyLinter",
    "CompositeLinter",
    "FileLinter",
    "GolangciLintLinter",
    "LintIssue",
    "LintRegistry",
    "LintResult",
    "MarkdownlintLinter",
    "MypyLinter",
    "PmdLinter",
    "RuffLinter",
    "ShellcheckLinter",
    "YamllintLinter",
    "default_lint_registry",
    "run_lint_subprocess",
]
