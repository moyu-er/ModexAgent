"""Shared structured-text formatting for KB responses.

Used by both KbTool (agent in-process) and REST route (server-side).
CLI echoes the pre-formatted text from REST route -- no formatting in CLI.

Projections: internal fields (entry_id, task_id, session_id, timestamps)
are NEVER included in formatted output -- agents must not see them.
"""

from __future__ import annotations

from typing import Final

from bot.kb.models import KbEntry, KbSearchResult

_MAX_PREVIEW_LINES: Final = 3
_MAX_PREVIEW_CHARS: Final = 200


def format_search_results(results: list[KbSearchResult]) -> str:
    if not results:
        return "No results found."

    lines = [f"Found {len(results)} result(s):\n"]
    for index, result in enumerate(results, 1):
        category = result.entry.category or "uncategorized"
        lines.append(
            f"{index}. [{result.entry.key}] ({category}, score: {result.score:.2f})"
        )
        preview = _truncate_value(result.entry.value)
        for line in preview.split("\n"):
            lines.append(f"   {line}")
        lines.append("")
    return "\n".join(lines).strip()


def format_entry(entry: KbEntry | None, key: str) -> str:
    if entry is None:
        return f"Not found: {key}"

    category = entry.category or "none"
    tags = entry.tags or "none"
    lines = [
        f"[{entry.key}] (category: {category}, tags: {tags})",
        "-" * 50,
        entry.value,
    ]
    return "\n".join(lines)


def format_upsert_confirmation(entry: KbEntry) -> str:
    category = entry.category or "none"
    return f"Saved: {entry.key} (category: {category})"


def format_delete_confirmation(deleted: bool, key: str) -> str:
    if deleted:
        return f"Deleted: {key}"
    return f"Not found: {key}"


def format_key_list(keys: list[str]) -> str:
    if not keys:
        return "No keys found."

    lines = [f"{len(keys)} key(s):"]
    for key in keys:
        lines.append(f"- {key}")
    return "\n".join(lines)


def _truncate_value(value: str) -> str:
    lines = value.split("\n")
    if len(lines) > _MAX_PREVIEW_LINES:
        truncated = "\n".join(lines[:_MAX_PREVIEW_LINES])
        if len(truncated) > _MAX_PREVIEW_CHARS:
            return truncated[:_MAX_PREVIEW_CHARS] + "..."
        return truncated + "\n... (use 'get' to see full content)"
    if len(value) > _MAX_PREVIEW_CHARS:
        return value[:_MAX_PREVIEW_CHARS] + "..."
    return value
