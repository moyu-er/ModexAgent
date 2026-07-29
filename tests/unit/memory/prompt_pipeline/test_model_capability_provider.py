from __future__ import annotations

import pytest

from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.memory.prompt_pipeline.providers import ModelInfoProvider

_CAPABLE = ModelInfo(
    model_name="test-vision",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
)
_TEXT_ONLY = ModelInfo(
    model_name="test-text",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT})),
)


@pytest.mark.asyncio
async def test_vision_caps_emits_capability_section() -> None:
    result = await ModelInfoProvider(_CAPABLE).get_or_refresh()
    assert "Your Capabilities" in result
    assert "can perceive images" in result


@pytest.mark.asyncio
async def test_text_only_caps_emits_limitation() -> None:
    result = await ModelInfoProvider(_TEXT_ONLY).get_or_refresh()
    assert "Your Capabilities" in result
    assert "cannot perceive images" in result


@pytest.mark.asyncio
async def test_none_emits_nothing() -> None:
    result = await ModelInfoProvider(None).get_or_refresh()
    assert result == ""


@pytest.mark.asyncio
async def test_read_mentioned_as_example() -> None:
    result = await ModelInfoProvider(_CAPABLE).get_or_refresh()
    assert "`read`" in result


@pytest.mark.asyncio
async def test_version_is_deterministic() -> None:
    provider = ModelInfoProvider(_CAPABLE)
    await provider.get_or_refresh()
    assert provider.last_version == "model:test-vision:image,text"
