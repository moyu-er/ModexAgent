"""Tests for SystemPromptPipeline assembly."""
from __future__ import annotations

import pytest

from modex_agent.core.prompt import SystemPromptPipeline, SystemPromptProvider


class _FakeProvider(SystemPromptProvider):
    def __init__(self, content: str, version: str = "v1") -> None:
        super().__init__()
        self._content = content
        self._version = version

    async def _fetch_version(self) -> str:
        return self._version

    async def _fetch_content(self) -> str:
        return self._content


class _EmptyProvider(SystemPromptProvider):
    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        return ""


class _ErrorProvider(SystemPromptProvider):
    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_assembles_multiple_providers():
    providers = [_FakeProvider("part A"), _FakeProvider("part B")]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "part A\n\n---\n\npart B"


@pytest.mark.asyncio
async def test_skips_empty_providers():
    providers = [_FakeProvider("part A"), _EmptyProvider(), _FakeProvider("part C")]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "part A\n\n---\n\npart C"


@pytest.mark.asyncio
async def test_skips_failing_providers():
    providers = [_FakeProvider("part A"), _ErrorProvider(), _FakeProvider("part C")]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "part A\n\n---\n\npart C"


@pytest.mark.asyncio
async def test_empty_pipeline_returns_empty_string():
    pipeline = SystemPromptPipeline([])
    result = await pipeline.get_or_refresh()
    assert result == ""


@pytest.mark.asyncio
async def test_single_provider_no_separator():
    providers = [_FakeProvider("only part")]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == "only part"


@pytest.mark.asyncio
async def test_all_empty_returns_empty():
    providers = [_EmptyProvider(), _EmptyProvider()]
    pipeline = SystemPromptPipeline(providers)
    result = await pipeline.get_or_refresh()
    assert result == ""
