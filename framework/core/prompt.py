"""System prompt pipeline — versioned, cacheable prompt section assembly.

Moved from framework.memory.pipeline to core to break the core <-> memory
import cycle (core.context and core.agent depend on SystemPromptPipeline).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class SystemPromptProvider(ABC):
    """One section of the system prompt pipeline with version-based caching.

    Subclasses implement _fetch_version() and _fetch_content().
    The ABC handles version comparison, caching, and conditional refresh.
    """

    def __init__(self) -> None:
        self._last_version: str | None = None
        self._cached_content: str = ""

    @abstractmethod
    async def _fetch_version(self) -> str:
        """Get current version string from underlying storage."""

    @abstractmethod
    async def _fetch_content(self) -> str:
        """Get fresh content from underlying storage."""

    async def get_or_refresh(self) -> str:
        """Return cached content or refresh if version changed."""
        current = await self._fetch_version()
        if self._last_version is None or current != self._last_version:
            self._cached_content = await self._fetch_content()
            self._last_version = current
        return self._cached_content

    @property
    def last_version(self) -> str | None:
        """Last cached version string, for debugging/logging."""
        return self._last_version


class SystemPromptPipeline:
    """Ordered collection of SystemPromptProvider instances.

    Assembles the full system prompt by iterating providers in order,
    skipping empty results and catching exceptions.
    Sections are joined with "\\n\\n---\\n\\n".
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
