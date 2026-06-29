from dataclasses import FrozenInstanceError

import pytest

from modex_agent.ioc.configs.llm import LLMConfig, Modality, ModelCapabilities


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.temperature == 0.7
        assert cfg.model == "gpt-4"

    def test_partial_override(self) -> None:
        cfg = LLMConfig(model="claude-opus-4-5", api_key="sk-xxx")
        assert cfg.model == "claude-opus-4-5"
        assert cfg.api_key == "sk-xxx"
        assert cfg.base_url == ""

    def test_full_override(self) -> None:
        cfg = LLMConfig(
            model="openai/MiniMax-M2.5",
            api_key="sk-xxx",
            base_url="https://api.minimaxi.com/v1",
            temperature=0.7,
            max_tokens=80000,
        )
        assert cfg.model == "openai/MiniMax-M2.5"
        assert cfg.base_url == "https://api.minimaxi.com/v1"
        assert cfg.max_tokens == 80000

    def test_default_capabilities_text_only(self) -> None:
        cfg = LLMConfig()
        assert cfg.capabilities.modalities == frozenset({Modality.TEXT})
        assert cfg.capabilities.supports(Modality.TEXT)
        assert not cfg.capabilities.supports(Modality.IMAGE)
        assert not cfg.capabilities.supports(Modality.VIDEO)
        assert not cfg.capabilities.supports(Modality.AUDIO)


class TestModelCapabilities:
    def test_frozen(self) -> None:
        caps = ModelCapabilities()
        with pytest.raises(FrozenInstanceError):
            caps.modalities = frozenset()  # type: ignore[misc]

    def test_default_is_text_only(self) -> None:
        caps = ModelCapabilities()
        assert caps.modalities == frozenset({Modality.TEXT})

    def test_custom_modalities(self) -> None:
        caps = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
        assert caps.supports(Modality.IMAGE)
        assert caps.supports(Modality.TEXT)
        assert not caps.supports(Modality.AUDIO)
