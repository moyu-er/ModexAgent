import pytest
from pydantic import ValidationError

from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.providers.factory import create_llm_provider
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.providers.llm_config import LLMConfig, Modality, ModelCapabilities
from modex_agent.providers.protocol_engines import ANTHROPIC_FORMAT, OPENAI_COMPATIBLE_FORMAT


class TestLLMConfig:
    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.temperature == 0.7
        assert cfg.top_p == 0.95
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
        assert cfg.interface_format == OPENAI_COMPATIBLE_FORMAT

    def test_interface_format_accepts_any_name(self) -> None:
        # The field is a plain name; the protocol-engine registry is the
        # closed-set authority (an unknown name fails loudly at
        # create_llm_provider — covered in test_protocol_engines.py).
        cfg = LLMConfig(interface_format=ANTHROPIC_FORMAT)
        assert cfg.interface_format == ANTHROPIC_FORMAT

    def test_reasoning_effort_enum_value(self) -> None:
        cfg = LLMConfig(reasoning_effort=ReasoningEffort.MEDIUM)
        assert cfg.reasoning_effort == ReasoningEffort.MEDIUM

    def test_reasoning_effort_rejects_invalid_string(self) -> None:
        with pytest.raises(ValueError):
            LLMConfig(reasoning_effort="invalid")  # type: ignore[arg-type]


class TestCreateLLMProvider:
    def test_passes_reasoning_effort_to_openai_compat_engine(self) -> None:
        cfg = LLMConfig(
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://api.example.com",
            reasoning_effort=ReasoningEffort.HIGH,
            interface_format=OPENAI_COMPATIBLE_FORMAT,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, HTTPStreamProvider)
        assert isinstance(provider._protocol, OpenAICompatProtocol)
        assert provider._cfg.reasoning_effort == ReasoningEffort.HIGH
        assert provider._model == "gpt-4o"

    def test_passes_reasoning_effort_to_anthropic_engine(self) -> None:
        cfg = LLMConfig(
            model="claude-3-5-sonnet",
            api_key="sk-test",
            base_url="https://api.example.com",
            reasoning_effort=ReasoningEffort.MEDIUM,
            interface_format=ANTHROPIC_FORMAT,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, HTTPStreamProvider)
        assert isinstance(provider._protocol, AnthropicProtocol)
        assert provider._cfg.reasoning_effort == ReasoningEffort.MEDIUM
        # Native anthropic wire dispatch: dispatch is interface_format-driven;
        # model names are never rewritten (no LiteLLM model-name routing).
        assert provider._model == "claude-3-5-sonnet"

    def test_openai_compatible_keeps_model_name_verbatim(self) -> None:
        cfg = LLMConfig(
            model="openai/gpt-4o",
            api_key="sk-test",
            base_url="https://api.example.com",
            interface_format=OPENAI_COMPATIBLE_FORMAT,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, HTTPStreamProvider)
        assert isinstance(provider._protocol, OpenAICompatProtocol)
        # Verbatim passthrough (user ruling 2026-08-26): a stale prefix
        # reaches the API as part of the model name.
        assert provider._model == "openai/gpt-4o"

    def test_anthropic_keeps_model_name_verbatim(self) -> None:
        cfg = LLMConfig(
            model="anthropic/claude-3-5-sonnet",
            api_key="sk-test",
            base_url="https://api.example.com",
            interface_format=ANTHROPIC_FORMAT,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, HTTPStreamProvider)
        assert isinstance(provider._protocol, AnthropicProtocol)
        assert provider._model == "anthropic/claude-3-5-sonnet"

    def test_passes_top_p_to_openai_compat_engine(self) -> None:
        cfg = LLMConfig(
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://api.example.com",
            top_p=0.9,
            interface_format=OPENAI_COMPATIBLE_FORMAT,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, HTTPStreamProvider)
        assert provider._top_p == 0.9

    def test_passes_top_p_to_anthropic_engine(self) -> None:
        cfg = LLMConfig(
            model="claude-3-5-sonnet",
            api_key="sk-test",
            base_url="https://api.example.com",
            top_p=0.9,
            interface_format=ANTHROPIC_FORMAT,
        )
        provider = create_llm_provider(cfg)
        assert isinstance(provider, HTTPStreamProvider)
        assert provider._top_p == 0.9


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
