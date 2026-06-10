"""SystemPromptPipeline — ordered collection of versioned prompt providers."""
from __future__ import annotations

import logging

from framework.memory.pipeline.abc import SystemPromptProvider

logger = logging.getLogger(__name__)


class SystemPromptPipeline:
    """Ordered collection of SystemPromptProvider instances.

    Assembles the full system prompt by iterating providers in order,
    skipping empty results and catching exceptions.
    Sections are joined with ``"\\n\\n---\\n\\n"``.
    """

    def __init__(self, providers: list[SystemPromptProvider]) -> None:
        self._providers = providers

    async def get_or_refresh(self) -> str:
        """Assemble system prompt from all providers, refreshing as needed."""
        parts: list[str] = []
        for provider in self._providers:
            try:
                content = await provider.get_or_refresh()
            except Exception:
                logger.warning(
                    "Provider %s failed, skipping",
                    type(provider).__name__,
                    exc_info=True,
                )
                continue
            if content:
                parts.append(content)
        return "\n\n---\n\n".join(parts)
