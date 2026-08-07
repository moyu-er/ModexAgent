"""AciEditTool — EditFileTool with automatic post-edit lint feedback.

This is the ACI (Agent-Computer Interface) enhancement of the standard
:class:`~modex_agent.tools.standard.file_tool.EditFileTool`. It inherits
all of EditFileTool's behavior (four-level fuzzy matching, quote
preservation, encoding preservation, ``replace_all``) and adds **one**
enhancement: after a successful edit, it runs registered linters on the
edited file and appends diagnostics to the tool result.

This implements SWE-agent ACI principle 5 (immediate feedback loop):
the agent sees lint errors in the *same* observation as the edit diff,
without needing to call a separate ``lsp_diagnostics`` or ``bash ruff``
tool. The edit tool's interface (parameters, description, name) is
**unchanged** — the LLM does not need to learn anything new.

Behavior matrix:

- Edit succeeds + linter finds issues → diff + lint issues appended
- Edit succeeds + linter finds 0 issues → diff + "Lint: 0 issues"
- Edit succeeds + no linter matches file type → diff + "Lint: skipped"
- Edit succeeds + linter unavailable → diff + "Lint: unavailable (...)"
- Edit fails → original EditFileTool error (no lint runs)
- File creation (empty old_string) → original message (no lint runs)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from modex_agent.tools.lint import LintIssue, LintRegistry
from modex_agent.tools.standard.file_tool import EditFileTool

logger = logging.getLogger(__name__)

# Maximum issues to display in the tool result (truncation guard).
_MAX_DISPLAY_ISSUES: int = 20


class AciEditTool(EditFileTool):
    """EditFileTool with automatic post-edit lint diagnostics.

    Drop-in replacement for :class:`EditFileTool`: same ``name`` (``"edit"``),
    same ``description``, same ``parameters``, same edit semantics. The only
    difference is that successful edits trigger a lint pass whose results
    are appended to the tool result string.

    The lint registry is injected at construction. Use
    :data:`~modex_agent.tools.aci.lint.default_lint_registry` for the
    framework default (pre-registered with :class:`RuffLinter`), or
    construct a custom :class:`LintRegistry` and register additional
    linters.
    """

    def __init__(self, lint_registry: LintRegistry) -> None:
        super().__init__()
        self._lint_registry = lint_registry

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        **kwargs: Any,  # noqa: ANN401  — matches parent EditFileTool.execute signature
    ) -> str | Any:  # type: ignore[override]  # noqa: ANN401
        # Delegate to the parent EditFileTool.execute — it handles all
        # the edit logic (fuzzy matching, quote preservation, encoding,
        # replace_all, error cases, file creation via empty old_string).
        result = await super().execute(
            path=path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
            **kwargs,
        )

        # Lint only runs on successful edits that return a string.
        # Error paths return ToolResult (with .error set) — skip lint.
        # File creation (empty old_string) returns "Created ..." — skip
        # lint because a freshly created file may be incomplete.
        if not isinstance(result, str):
            return result

        # Skip lint for file creation paths
        if result.startswith("Created "):
            return result

        # Run lint and append diagnostics
        lint_suffix = await self._run_lint(path)
        if lint_suffix:
            return f"{result}\n{lint_suffix}"
        return result

    async def _run_lint(self, path: str) -> str:
        """Run linters on *path* and format the result as a suffix string.

        Returns an empty string if no suffix should be appended (should
        not happen in normal flow — always returns at least a "Lint:"
        line).
        """
        file_path = Path(path).expanduser().resolve()
        issues = await self._lint_registry.lint_file_async(file_path)

        if not issues:
            matched = self._lint_registry._select(file_path)  # noqa: SLF001
            if not matched:
                return f"Lint: skipped (no linter for {file_path.suffix or 'unknown'})"
            names = ", ".join(ln.name for ln in matched)
            return f"Lint ({names}): 0 issues"

        names = ", ".join(
            ln.name for ln in self._lint_registry._select(file_path)  # noqa: SLF001
        )
        return _format_issues(issues, names)


def _format_issues(issues: list[LintIssue], linter_names: str) -> str:
    """Format a list of LintIssue into a human/LLM-readable string.

    Structured issues (line > 0) are formatted with path:line:col + code.
    Unstructured issues (line == 0) are formatted as ``[source] message``.
    """
    n = len(issues)
    truncated = n > _MAX_DISPLAY_ISSUES
    display = issues[:_MAX_DISPLAY_ISSUES]

    lines: list[str] = [f"Lint ({linter_names}): {n} issues"]

    for issue in display:
        if issue.line > 0:
            # Structured: path:line:col severity code message
            col_str = f":{issue.column}" if issue.column > 0 else ""
            code_str = f" {issue.code}" if issue.code else ""
            lines.append(
                f"  {issue.line}{col_str} {issue.severity}{code_str} [{issue.source}] {issue.message}"
            )
        else:
            # Unstructured: [source] message
            lines.append(f"  [{issue.source}] {issue.message}")

    if truncated:
        remaining = n - _MAX_DISPLAY_ISSUES
        lines.append(f"  ... and {remaining} more")

    return "\n".join(lines)
