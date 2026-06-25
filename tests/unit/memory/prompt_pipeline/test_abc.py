"""Tests for SystemPromptProvider ABC contract."""
from __future__ import annotations

import pytest

from modex_agent.core.prompt import SystemPromptProvider


class _StaticProvider(SystemPromptProvider):
    """Test provider that returns fixed content."""

    def __init__(self, content: str = "hello") -> None:
        super().__init__()
        self._content = content
        self._version_calls = 0
        self._content_calls = 0

    async def _fetch_version(self) -> str:
        self._version_calls += 1
        return "v1"

    async def _fetch_content(self) -> str:
        self._content_calls += 1
        return self._content


class _ChangingProvider(SystemPromptProvider):
    """Test provider whose version changes each call."""

    def __init__(self) -> None:
        super().__init__()
        self._counter = 0

    async def _fetch_version(self) -> str:
        return f"v{self._counter}"

    async def _fetch_content(self) -> str:
        self._counter += 1
        return f"content-{self._counter}"


class _EmptyVersionProvider(SystemPromptProvider):
    """Test provider that returns empty version after first call."""

    def __init__(self) -> None:
        super().__init__()
        self._calls = 0

    async def _fetch_version(self) -> str:
        self._calls += 1
        return "v1" if self._calls == 1 else ""

    async def _fetch_content(self) -> str:
        return "refreshed"


@pytest.mark.asyncio
async def test_first_call_always_fetches():
    provider = _StaticProvider("test content")
    result = await provider.get_or_refresh()
    assert result == "test content"
    assert provider._version_calls == 1
    assert provider._content_calls == 1
    assert provider.last_version == "v1"


@pytest.mark.asyncio
async def test_cached_hit_when_version_unchanged():
    provider = _StaticProvider("cached")
    await provider.get_or_refresh()
    result = await provider.get_or_refresh()
    assert result == "cached"
    assert provider._version_calls == 2
    assert provider._content_calls == 1


@pytest.mark.asyncio
async def test_version_change_triggers_refresh():
    provider = _ChangingProvider()
    r1 = await provider.get_or_refresh()
    r2 = await provider.get_or_refresh()
    assert r1 != r2


@pytest.mark.asyncio
async def test_empty_version_forces_refresh():
    provider = _EmptyVersionProvider()
    r = await provider.get_or_refresh()
    assert r == "refreshed"


@pytest.mark.asyncio
async def test_cannot_instantiate_abc_directly():
    with pytest.raises(TypeError):
        SystemPromptProvider()


@pytest.mark.asyncio
async def test_initial_state():
    provider = _StaticProvider()
    assert provider.last_version is None
