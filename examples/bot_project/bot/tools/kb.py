from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

from bot.kb.formatting import (
    format_delete_confirmation,
    format_entry,
    format_key_list,
    format_search_results,
    format_upsert_confirmation,
)
from bot.kb.models import (
    KbAction,
    KbFilter,
    KbUpsertRequest,
)
from bot.kb.provider import KbProvider
from modex_agent.core.tool_manager import Tool, ToolConfig


class KbTool(Tool):
    def __init__(
        self,
        provider: KbProvider,
        task_id_provider: Callable[[], str | None],
        session_id_provider: Callable[[], str | None],
    ) -> None:
        self._provider = provider
        self._task_id_provider = task_id_provider
        self._session_id_provider = session_id_provider
        super().__init__(
            name="kb",
            description=(
                "Save and look up knowledge that persists across conversations. "
                "Use when you need to remember something for later, "
                "find what was previously saved, or search across stored notes.\n\n"
                "Actions:\n"
                "  set(key, value) — Save knowledge under a short key for later retrieval\n"
                "  get(key) — Retrieve a specific piece of knowledge by its key\n"
                "  search(query) — Find knowledge by searching its content\n"
                "  delete(key) — Remove knowledge by key\n"
                "  list(prefix?) — Browse saved keys, optionally filtered by prefix"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "get", "search", "delete", "list"],
                        "description": "What to do with the knowledge base",
                    },
                    "key": {
                        "type": "string",
                        "description": "A short identifier for the knowledge (e.g. 'deploy-steps')",
                    },
                    "value": {
                        "type": "string",
                        "description": "The knowledge content to store",
                    },
                    "query": {
                        "type": "string",
                        "description": "Natural language search terms",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional: filter by category (e.g. 'project', 'config')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 20)",
                    },
                    "prefix": {
                        "type": "string",
                        "description": "Optional: only list keys starting with this prefix",
                    },
                },
                "required": ["action"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs) -> str:
        try:
            action = KbAction(kwargs["action"])
        except ValueError:
            return '{"error": "unknown action"}'

        task_id = self._task_id_provider()
        session_id = self._session_id_provider()
        filter = KbFilter(
            task_id=task_id,
            session_id=session_id,
            category=kwargs.get("category"),
        )

        match action:
            case KbAction.SEARCH:
                results = await self._provider.search(
                    kwargs["query"], filter, kwargs.get("limit", 20)
                )
                return format_search_results(results)
            case KbAction.GET:
                entry = await self._provider.get(kwargs["key"], filter)
                return format_entry(entry, kwargs["key"])
            case KbAction.SET:
                request = KbUpsertRequest(
                    key=kwargs["key"],
                    value=kwargs["value"],
                    task_id=task_id or "",
                    session_id=session_id or "",
                    category=kwargs.get("category", ""),
                )
                entry = await self._provider.upsert(request)
                return format_upsert_confirmation(entry)
            case KbAction.DELETE:
                deleted = await self._provider.delete(kwargs["key"], filter)
                return format_delete_confirmation(deleted, kwargs["key"])
            case KbAction.LIST:
                keys = await self._provider.list_keys(filter, kwargs.get("prefix"))
                return format_key_list(keys)
            case unreachable:
                assert_never(unreachable)
