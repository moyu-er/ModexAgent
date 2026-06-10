"""SystemPromptProvider ABC — versioned, cacheable system prompt section."""
from __future__ import annotations

from abc import ABC, abstractmethod


class SystemPromptProvider(ABC):
    """One section of the system prompt pipeline with version-based caching.

    Subclasses implement _fetch_version() and _fetch_content().
    The ABC handles version comparison, caching, and conditional refresh.

    Lifecycle:
    - Constructed with _last_version = None, _cached_content = ""
    - First get_or_refresh() always fetches (because _last_version is None)
    - Subsequent calls compare _fetch_version() with _last_version
    - Version match → return cached content (zero I/O)
    - Version mismatch → re-fetch content, update cache
    """

    def __init__(self) -> None:
        self._last_version: str | None = None
        self._cached_content: str = ""

    @abstractmethod
    async def _fetch_version(self) -> str:
        """Get current version string from underlying storage.

        Returns "" on error to force refresh.
        """

    @abstractmethod
    async def _fetch_content(self) -> str:
        """Get fresh content from underlying storage.

        Returns "" if no content is available.
        """

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
