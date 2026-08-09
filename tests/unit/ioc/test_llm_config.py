import pytest

pytest.importorskip("openai")  # skip if openai not installed (CI [dev] doesn't include [llm] deps)

from pydantic import ValidationError

from modex_agent.core.constants import InterfaceFormat, ReasoningEffort
from modex_agent.ioc.configs.llm import LLMConfig, Modality, ModelCapabilities
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.providers.litellm_provider import LiteLLMProvider
from modex_agent.providers.openai_provider import OpenAIProvider


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
            max_output_tokens=80000,
        )
        assert cfg.model == "openai/MiniMax-M2.5"
        assert cfg.base_url == "https://api.minimaxi.com/v1"
        assert cfg.max_output_tokens == 80000

    def test_default_capabilities_text_only(self) -> None:
        cfg = LLMConfig()
        assert cfg.capabilities.modalities == frozenset({Modality.TEXT})
        assert cfg.capabilities.supports(Modality.TEXT)
        assert not cfg.capabilities.supports(Modality.IMAGE)
        assert not cfg.capabilities.supports(Modality.VIDEO)
        assert not cfg.capabilities.supports(Modality.AUDIO)

    def test_default_reasoning_effort_is_none(self) -> None:
        cfg = LLMConfig()
        assert cfg.reasoning_effort == ReasoningEffort.NONE

    def test_default_interface_format_is_openai_compatible(self) -> None:
        cfg = LLMConfig()
        assert cfg.interface_format == InterfaceFormat.OPENAI_COMPATIBLE

    def test_interface_format_accepts_enum(self) -> None:
        cfg = LLMConfig(interface_format=InterfaceFormat.ANTHROPIC)
        assert cfg.interface_format == InterfaceFormat.ANTHROPIC

    def test_interface_format_rejects_invalid_string(self) -> None:
        with pytest.raises(ValueError):
            LLMConfig(interface_format="invalid")  # type: ignore[arg-type]

    def test_reasoning_effort_enum_value(self) -> None:
        cfg = LLMConfig(reasoning_effort=ReasoningEffort.MEDIUM)
        assert cfg.reasoning_effort == ReasoningEffort.MEDIUM

    def test_reasoning_effort_rejects_invalid_string(self) -> None:
        with pytest.raises(ValueError):
            LLMConfig(reasoning_effort="invalid")  # type: ignore[arg-type]


class TestCreateLLMProvider:
    def test_passes_reasoning_effort_to_openai_provider(self) -> None:
        cfg = LLMConfig(
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://api.example.com",
            reasoning_effort=ReasoningEffort.HIGH,
            interface_format=InterfaceFormat.OPENAI_COMPATIBLE,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, OpenAIProvider)
        assert provider._reasoning_effort == ReasoningEffort.HIGH
        assert provider._model == "gpt-4o"

    def test_passes_reasoning_effort_to_litellm_provider(self) -> None:
        cfg = LLMConfig(
            model="claude-3-5-sonnet",
            api_key="sk-test",
            base_url="https://api.example.com",
            reasoning_effort=ReasoningEffort.MEDIUM,
            interface_format=InterfaceFormat.ANTHROPIC,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, LiteLLMProvider)
        assert provider._reasoning_effort == ReasoningEffort.MEDIUM
        assert provider._model == "anthropic/claude-3-5-sonnet"

    def test_openai_compatible_strips_openai_prefix(self) -> None:
        cfg = LLMConfig(
            model="openai/gpt-4o",
            api_key="sk-test",
            base_url="https://api.example.com",
            interface_format=InterfaceFormat.OPENAI_COMPATIBLE,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, OpenAIProvider)
        assert provider._model == "gpt-4o"

    def test_anthropic_strips_anthropic_prefix_then_re_adds(self) -> None:
        cfg = LLMConfig(
            model="anthropic/claude-3-5-sonnet",
            api_key="sk-test",
            base_url="https://api.example.com",
            interface_format=InterfaceFormat.ANTHROPIC,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, LiteLLMProvider)
        assert provider._model == "anthropic/claude-3-5-sonnet"


class TestModelCapabilities:
    def test_frozen(self) -> None:
        caps = ModelCapabilities()
        with pytest.raises(ValidationError):
            caps.modalities = frozenset()  # type: ignore[misc]

    def test_default_is_text_only(self) -> None:
        caps = ModelCapabilities()
        assert caps.modalities == frozenset({Modality.TEXT})

    def test_custom_modalities(self) -> None:
        caps = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
        assert caps.supports(Modality.IMAGE)
        assert caps.supports(Modality.TEXT)
        assert not caps.supports(Modality.AUDIO)
