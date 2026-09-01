"""Graph-scoped markdown knowledge base tool.

Size waiver: # noqa: SIZE_OK - the task requires all five actions in one module.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from modex_agent.core.agent import current_agent_context
from modex_agent.core.tool_manager import ExclusiveTool, ToolConfig
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_knowledge_capabilities import KnowledgeToolCapabilities
from modex_agent.tools.standard.file_tool import (
    _build_unified_diff,
    _find_actual_string,
    _paginate_file,
    _read_file,
    _write_file,
)

_PATTERNS: Final[tuple[str, ...]] = (
    "findings",
    "decisions",
    "open_questions",
    "context",
    "changelog",
)
_GREP_MAX_RESULTS: Final = 100
_GREP_DEFAULT_MAX: Final = 50
_GREP_MAX_CONTEXT: Final = 5
_GREP_DEFAULT_CONTEXT: Final = 2


class GraphKnowledgeBaseTool(ExclusiveTool):
    """Share markdown findings and decisions within one graph instance."""

    def __init__(
        self,
        *,
        knowledge_dir: Path,
        capabilities: KnowledgeToolCapabilities,
        node_name: str,
    ) -> None:
        self._knowledge_dir = knowledge_dir
        self._capabilities = capabilities
        self._node_name = node_name
        super().__init__(name="knowledge_base", parameters=self.parameters, config=ToolConfig())

    @property
    def description(self) -> str:
        return (
            f"You are node: {self._node_name}\n"
            "Shared knowledge base for cross-node information exchange within "
            "this graph run. Use it to record findings, decisions, and questions so downstream "
            "or non-adjacent nodes can benefit from your work, and to review what other nodes "
            "have already established.\n\n"
            "Usage:\n"
            "- Read findings/open_questions before starting work to avoid duplicating effort.\n"
            "- Use grep to check whether a topic has already been recorded.\n"
            "- Append to findings/decisions by convention — use write only when starting a fresh file.\n"
            "- The changelog is auto-maintained; your writes and edits are attributed automatically."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "edit", "ls", "grep"],
                    "description": (
                        "Operation to perform on the knowledge base. read: view file content; "
                        "write: create or replace a file; edit: in-place string replacement; "
                        "ls: list available files; grep: search across files."
                    ),
                },
                "pattern": {
                    "type": "string",
                    "enum": list(_PATTERNS),
                    "description": (
                        "Knowledge file to operate on. Required for read/write/edit/ls. "
                        "Optional for grep: omit to search all files."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write. Required for write.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find. Required for edit.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text. Empty string deletes. Required for edit.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["create", "overwrite"],
                    "description": "Write mode. create (default) fails if file exists; overwrite replaces.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Lines to skip from start (0-based). For read pagination.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return. For read pagination.",
                },
                "query": {
                    "type": "string",
                    "description": "Search term or regex pattern. Required for grep.",
                },
                "regex": {
                    "type": "boolean",
                    "description": "true (default) = regex match; false = literal text.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max matches to return (default 50, cap 100). For grep.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Context lines before/after each match (default 2, max 5). For grep.",
                },
            },
            "required": ["action"],
        }

    def get_dynamic_schema(self) -> dict[str, Any]:
        """Return schema with action enum filtered by capabilities."""
        schema = super().get_dynamic_schema()
        function = dict(schema.get("function", {}))
        parameters = dict(function.get("parameters", {}))
        properties = dict(parameters.get("properties", {}))
        properties["action"] = {
            **dict(properties.get("action", {})),
            "enum": self._capabilities.allowed_actions(),
        }
        parameters["properties"] = properties
        function["parameters"] = parameters
        return {**schema, "function": function}

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401 - Tool ABC contract
        """Perform one capability-checked knowledge base action."""
        # Normalize enum-like params internally (strip + lowercase). Not exposed
        # in the JSON Schema — the LLM sees lowercase enum values, but we
        # tolerate minor casing/whitespace deviations at runtime.
        action = str(kwargs.get("action", "")).strip().lower()
        if action not in self._capabilities.allowed_actions():
            return f"Error: action {action!r} is not allowed by this tool's capabilities."

        pattern_value = kwargs.get("pattern")
        pattern = None if pattern_value is None else str(pattern_value).strip().lower()
        # Validate pattern against the closed set (defense in depth — JSON Schema
        # enum is not enforced at runtime by all ToolManager implementations).
        if pattern is not None and pattern not in _PATTERNS:
            return f"Error: invalid pattern {pattern!r}. Allowed: {', '.join(_PATTERNS)}."
        if action in ("read", "write", "edit") and pattern is None:
            return f"Error: pattern is required for action {action!r}."
        if action in ("write", "edit") and pattern == "changelog":
            verb = "write to" if action == "write" else "edit"
            return f"Error: changelog is auto-maintained. You cannot {verb} it directly."

        handlers: dict[str, Callable[[str | None, dict[str, Any]], str]] = {
            "read": self._read,
            "write": self._write,
            "edit": self._edit,
            "ls": self._list_files,
            "grep": self._grep,
        }
        return handlers[action](pattern, kwargs)

    def _resolve_pattern_path(self, pattern: str) -> Path:
        """Resolve a pattern name within the graph knowledge directory."""
        return self._knowledge_dir / f"{pattern}.md"

    def _read(self, pattern: str | None, kwargs: dict[str, Any]) -> str:
        assert pattern is not None
        path = self._resolve_pattern_path(pattern)
        if not path.exists():
            return (
                f"The '{pattern}' knowledge file has not been created yet — "
                f"no node has recorded {pattern} for this graph instance. "
                f"Use action='write' with pattern='{pattern}' to create it "
                f"and record your {pattern}."
            )
        result = _paginate_file(path, int(kwargs.get("offset", 0)), int(kwargs.get("limit", 200)))
        if not result.startswith("Error:"):
            self._increment_read_count()
        return result

    def _write(self, pattern: str | None, kwargs: dict[str, Any]) -> str:
        assert pattern is not None
        content_value = kwargs.get("content")
        if content_value is None:
            return "Error: content is required for action 'write'."
        content = str(content_value)
        mode = str(kwargs.get("mode", "create"))
        if mode not in ("create", "overwrite"):
            return f"Error: invalid write mode {mode!r}."
        path = self._resolve_pattern_path(pattern)
        if mode == "create" and path.exists():
            try:
                existing_content = _read_file(path)[0]
            except (OSError, UnicodeDecodeError):
                existing_content = ""
            if existing_content.strip():
                return (
                    f"The '{pattern}' knowledge file already has content. "
                    f"Read it first with action='read' pattern='{pattern}', "
                    f"then use action='edit' to append or modify specific sections."
                )

        old, encoding, line_endings = _read_file(path) if path.exists() else ("", "utf-8", "LF")
        _write_file(path, content, encoding, line_endings)
        diff = _build_unified_diff(old, content, pattern)
        self._append_changelog("write", pattern, diff)
        self._increment_write_count()
        message = f"Wrote {pattern}.md."
        return f"{message}\n{diff}" if diff else message

    def _edit(self, pattern: str | None, kwargs: dict[str, Any]) -> str:
        assert pattern is not None
        old_value = kwargs.get("old_string")
        new_value = kwargs.get("new_string")
        if old_value is None:
            return "Error: old_string is required for action 'edit'."
        if new_value is None:
            return "Error: new_string is required for action 'edit'."
        path = self._resolve_pattern_path(pattern)
        if not path.exists():
            return (
                f"The '{pattern}' knowledge file has not been created yet. "
                f"Use action='write' with pattern='{pattern}' to create it first, "
                f"then use 'edit' to refine specific sections."
            )

        content, encoding, line_endings = _read_file(path)
        actual = _find_actual_string(content, str(old_value))
        if actual is None:
            return "Error: old_string not found in file"
        updated = content.replace(actual, str(new_value), 1)
        _write_file(path, updated, encoding, line_endings)
        diff = _build_unified_diff(content, updated, pattern)
        self._append_changelog("edit", pattern, diff)
        self._increment_write_count()
        message = f"Updated {pattern}.md."
        return f"{message}\n{diff}" if diff else message

    def _list_files(self, _pattern: str | None, _kwargs: dict[str, Any]) -> str:
        lines = ["Knowledge files in this graph instance:"]
        for pattern in _PATTERNS:
            path = self._resolve_pattern_path(pattern)
            if not path.exists():
                lines.append(f"- {path.name}: (not created)")
                continue
            line_count = len(_read_file(path)[0].splitlines())
            size = path.stat().st_size
            size_text = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} bytes"
            lines.append(f"- {path.name}: {size_text} ({line_count} lines)")
        return "\n".join(lines)

    def _grep(self, pattern: str | None, kwargs: dict[str, Any]) -> str:
        query_value = kwargs.get("query")
        if query_value is None or not str(query_value):
            return "Error: query is required for action 'grep'."
        regex_value = kwargs.get("regex", True)
        use_regex = (
            regex_value.lower() in ("true", "1", "yes")
            if isinstance(regex_value, str)
            else bool(regex_value)
        )
        query = str(query_value)
        try:
            compiled = re.compile(query if use_regex else re.escape(query))
        except re.error as error:
            return f"Error: Invalid regex pattern: {error}"

        max_results = min(
            max(int(kwargs.get("max_results", _GREP_DEFAULT_MAX)), 1), _GREP_MAX_RESULTS
        )
        context_lines = min(
            max(int(kwargs.get("context_lines", _GREP_DEFAULT_CONTEXT)), 0), _GREP_MAX_CONTEXT
        )
        files = (
            [self._resolve_pattern_path(pattern)]
            if pattern is not None
            else sorted(self._knowledge_dir.glob("*.md"))
        )
        matches: list[tuple[str, list[tuple[int, str]]]] = []
        for path in files:
            if not path.exists():
                continue
            file_lines = _read_file(path)[0].splitlines()
            for index, line in enumerate(file_lines):
                if compiled.search(line) is None:
                    continue
                start = max(index - context_lines, 0)
                end = min(index + context_lines + 1, len(file_lines))
                matches.append(
                    (path.name, [(number + 1, file_lines[number]) for number in range(start, end)])
                )
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        self._increment_read_count()
        if not matches:
            return "No matches found."
        result = [f"Found {len(matches)} match{'es' if len(matches) != 1 else ''}:", ""]
        for file_name, block in matches:
            result.append(f"{file_name}:")
            result.extend(f"  {number:4d} | {text}" for number, text in block)
        return "\n".join(result)

    def _append_changelog(self, action: str, pattern: str, diff: str) -> None:
        """Append an attributed changelog entry using server local time."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{self._node_name}] {timestamp} | {action} {pattern}"
        if diff:
            entry += f"\n{diff}"
        changelog_path = self._knowledge_dir / "changelog.md"
        changelog_path.parent.mkdir(parents=True, exist_ok=True)
        with changelog_path.open("a", encoding="utf-8") as file:
            file.write(f"{entry}\n\n")

    @staticmethod
    def _increment_read_count() -> None:
        GraphKnowledgeBaseTool._increment_count(TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT)

    @staticmethod
    def _increment_write_count() -> None:
        GraphKnowledgeBaseTool._increment_count(TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT)

    @staticmethod
    def _increment_count(key: TurnCustomKey) -> None:
        agent_context = current_agent_context.get(None)
        if agent_context is not None and agent_context.runtime is not None:
            state = agent_context.runtime.state
            if state is not None:
                count = state.custom.get(key, 0)
                state.custom[key] = count + 1


__all__ = ["GraphKnowledgeBaseTool"]
