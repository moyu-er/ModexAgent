"""Tests for individual SystemPromptProvider implementations."""
from __future__ import annotations

import pytest

from modex_agent.memory.pipeline.providers import (
    BasePromptProvider,
    ExperienceProvider,
    KnowledgeProvider,
    RuntimeProvider,
    SkillProvider,
)


# -- BasePromptProvider --


@pytest.mark.asyncio
async def test_base_prompt_returns_content():
    provider = BasePromptProvider("You are a helpful assistant.")
    result = await provider.get_or_refresh()
    assert result == "You are a helpful assistant."


@pytest.mark.asyncio
async def test_base_prompt_never_refreshes():
    provider = BasePromptProvider("original")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    result = await provider.get_or_refresh()
    assert result == "original"


@pytest.mark.asyncio
async def test_base_prompt_empty_string():
    provider = BasePromptProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- RuntimeProvider --


@pytest.mark.asyncio
async def test_runtime_contains_date_and_platform():
    provider = RuntimeProvider()
    result = await provider.get_or_refresh()
    assert "Current Time:" in result
    assert "Platform:" in result


@pytest.mark.asyncio
async def test_runtime_version_changes_hourly():
    provider = RuntimeProvider()
    await provider.get_or_refresh()
    assert provider.last_version is not None
    assert len(provider.last_version) == 13  # YYYY-MM-DD-HH


# -- SkillProvider --


@pytest.mark.asyncio
async def test_skill_never_refreshes():
    provider = SkillProvider("skill content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"
    result = await provider.get_or_refresh()
    assert result == "skill content"


@pytest.mark.asyncio
async def test_skill_empty_when_no_content():
    provider = SkillProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- KnowledgeProvider --


@pytest.mark.asyncio
async def test_knowledge_never_refreshes_during_react():
    provider = KnowledgeProvider("knowledge content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_knowledge_empty_when_no_content():
    provider = KnowledgeProvider("")
    result = await provider.get_or_refresh()
    assert result == ""


# -- ExperienceProvider --


@pytest.mark.asyncio
async def test_experience_default_static():
    provider = ExperienceProvider("experience content")
    await provider.get_or_refresh()
    assert provider.last_version == "static"


@pytest.mark.asyncio
async def test_experience_empty_when_no_content():
    provider = ExperienceProvider("")
    result = await provider.get_or_refresh()
    assert result == ""
