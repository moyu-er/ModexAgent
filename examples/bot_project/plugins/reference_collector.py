"""ReferenceCollectorPlugin — a real, self-contained custom plugin example.

Collects URLs mentioned in new conversation content and, at each turn end,
appends the deduplicated list back into the transcript as a ``<sources>``
system-reminder so later turns can cite them and the conversation stays
auditable.

Watermark semantics (idempotent, self-limiting): the hook keeps a
high-water mark of the transcript length it has already scanned. Each
turn it scans ONLY the content appended since that mark — new user/agent
messages, tool results, AND reminders injected by other hooks — then
advances the mark. Consequences:

- Its own previous ``<sources>`` reminders are never re-scanned (they are
  behind the mark), so the list never feeds itself and cannot grow
  across turns.
- Each injection covers exactly "everything since the last injection"
  (or the conversation start), matching the per-hook memory-point rule.
- The hook is injected once per agent (factory contract: one
  ``create()`` per assembly; the roster-wins dedup guards against a
  second registration).

This file is the reference walkthrough of the user-extension path:

1. A ``Plugin`` subclass whose ``register(ctx)`` registers a HOOK-slot
   factory under the roster-referenceable name ``reference_collector``.
2. Discovered by directory discovery (``ComponentRegistryLoader`` scans
   ``plugins/*.py`` at startup — no framework code change needed).
3. Referenced from YAML: add ``hooks: [+reference_collector]`` to a
   pool.yml or subagent template, restart, and every turn of that agent
   gets source collection.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import MessageRole
from modex_agent.hook.abc import AfterTurnHook
from modex_agent.plugins.abc import ReactHookFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext

_URL_PATTERN = re.compile(r"https?://[^\s\"'<>()\[\]]+")

_TRAILING_PUNCTUATION = ".,;:!?)»›]"


class ReferenceCollectorConfig(BaseModel):
    """Roster-facing config for the reference collector hook."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_sources: int = 20
    """Truncate the reminder beyond this many URLs (0 disables the hook)."""


class ReferenceCollectorHook(AfterTurnHook):
    """Append the new-content URLs as a ``<sources>`` reminder.

    The watermark lives on the hook instance (one per agent session);
    ``len(messages)`` after the scan is the next turn's start offset.
    """

    def __init__(self, max_sources: int) -> None:
        self._max_sources = max_sources
        self._watermark = 0

    @property
    def name(self) -> str:
        return "reference_collector"

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if self._max_sources <= 0:
            return
        messages = await ctx.history.to_list()
        new_messages = messages[self._watermark :]
        seen: list[str] = []
        seen_set: set[str] = set()
        for message in new_messages:
            content = message.content if message.content is not None else ""
            for url in _URL_PATTERN.findall(content):
                url = url.rstrip(_TRAILING_PUNCTUATION)
                if url not in seen_set:
                    seen_set.add(url)
                    seen.append(url)
        if not seen:
            return
        lines = "\n".join(f"- {url}" for url in seen[: self._max_sources])
        overflow = len(seen) - self._max_sources
        if overflow > 0:
            lines += f"\n- … and {overflow} more (truncated at {self._max_sources})"
        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": (
                    "<sources>\nURLs mentioned since the last collection, "
                    f"deduplicated for citation:\n{lines}\n</sources>"
                ),
            }
        )
        # Advance past everything scanned AND this reminder itself — the
        # reminder lands behind the next scan window, so it can never feed
        # itself on a later turn.
        self._watermark = len(messages) + 1


class ReferenceCollectorHookFactory(ReactHookFactory):
    """HOOK-slot factory — react runner, all native agent types."""

    config_model = ReferenceCollectorConfig

    async def create(
        self, config: ReferenceCollectorConfig, ctx: AssemblyContext
    ) -> ReferenceCollectorHook:
        return ReferenceCollectorHook(max_sources=config.max_sources)


class ReferenceCollectorPlugin(Plugin):
    """Entry point discovered by ``ComponentRegistryLoader``."""

    config_model = ReferenceCollectorConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_hook("reference_collector", ReferenceCollectorHookFactory())


__all__ = [
    "ReferenceCollectorConfig",
    "ReferenceCollectorHook",
    "ReferenceCollectorHookFactory",
    "ReferenceCollectorPlugin",
]
