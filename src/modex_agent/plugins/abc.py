"""Plugin system abstract base classes.

The primary extension point is MemoryProvider — pluggable memory backends
that integrate with the framework's four-layer memory system as an
additive enhancement layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.scope import MemoryContext


class MemoryProvider(ABC):
    """Pluggable memory backend.

    Design constraints:
    - The built-in four-layer memory architecture always exists; providers
      are additive enhancement layers.
    - Per-provider error isolation: MemorySystem wraps each provider call
      in try/except so one failing provider never blocks others.
    - Implement is_available() so the framework can skip providers whose
      dependencies are not installed.

    Reference: references/hermes-agent/agent/memory_provider.py
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g. 'mem0', 'chroma', 'redis')."""
        ...

    # ---- lifecycle ----

    def is_available(self) -> bool:
        """Check whether the provider is available.

        Default returns True. Providers that need optional dependencies
        should override this to check imports / configuration.

        Returns False -> framework skips this provider silently.
        """
        return True

    @abstractmethod
    async def initialize(self, **kwargs: Any) -> None:
        """Initialize the provider (create connections, warm caches, etc.).

        Called by PluginManager.initialize_all().
        kwargs may include:
        - llm_provider: the framework's LLM Provider instance
        - workspace: working directory Path
        - config: plugin-specific config dict from bot_config.yml
        """
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """Clean up resources (close connections, flush caches, etc.)."""
        ...

    # ---- core capabilities ----

    @abstractmethod
    async def add(
        self,
        messages: list[ChatMessage],
        context: MemoryContext,
    ) -> dict[str, Any]:
        """Add messages to the memory backend.

        Called by MemorySystem after each turn (fan-out to all providers).

        Args:
            messages: ChatMessage 列表（结构化消息，B6 收敛自 list[dict]）
            context: MemoryContext (session_id, user_id, agent_id, etc.)

        Returns:
            {"status": "ok", "memories": [...]} or
            {"status": "error", "error": "..."}
        """
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for relevant memories.

        Returns:
            [{"memory": "fact text", "score": 0.95, "metadata": {...}}, ...]
        """
        ...

    # ---- optional extensions ----

    async def prefetch(  # noqa: ARG002
        self, _query: str, _context: MemoryContext
    ) -> str | None:
        """Per-turn dynamic memory prefetch.

        Called before each agent turn to retrieve relevant memories based on
        the current user query. Return a text block to inject into the
        system prompt, or None to skip injection.

        Default returns None (no injection).
        """
        return None

    async def on_pre_compress(  # noqa: ARG002
        self,
        _messages: list[ChatMessage],
        _context: MemoryContext,
    ) -> None:
        """Called before context compression prunes messages.

        Providers can extract insights from messages that are about to be
        discarded and persist them. Default is a no-op.
        """
        return None

    def system_prompt_block(self) -> str:
        """Static text block injected into the system prompt. Default empty."""
        return ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Tool schemas exposed to the agent (OpenAI function-calling format).

        If a provider wants the agent to actively call search/add/etc.,
        return tool schema dicts here. Not wired in Phase 1.
        """
        return []

    async def handle_tool_call(  # noqa: ARG002
        self, _tool_name: str, _args: dict[str, Any]
    ) -> Any:
        """Handle agent tool calls directed at this provider.

        Only required when get_tool_schemas() returns non-empty.
        """
        raise NotImplementedError(f"Provider '{self.name}' does not handle tool calls")
