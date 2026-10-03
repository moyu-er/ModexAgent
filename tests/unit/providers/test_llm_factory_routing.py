"""Factory routing tests — every interface_format name lands on
HTTPStreamProvider with the engine the registry resolves.

Covers the T19 wiring contract: three-format engine dispatch, verbatim
model-name passthrough (no prefix processing — user ruling 2026-08-26),
factory-side URL resolution (endpoint_url override or engine url() join),
config-field passthrough, the api_key environment fallback performed inside
the HTTPStreamProvider constructor, and the GlobalModelConfig → LLMConfig →
factory headers chain that plugins/defaults/llm.py rides on.
"""

from __future__ import annotations

import pytest

from modex_agent.providers.factory import create_llm_provider
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.formats.openai_responses import OpenAIResponsesProtocol
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.providers.llm_config import LLMConfig
from modex_agent.providers.model_config import GlobalModelConfig
from modex_agent.providers.protocol_engines import (
    ANTHROPIC_FORMAT,
    OPENAI_COMPATIBLE_FORMAT,
    OPENAI_RESPONSE_FORMAT,
)

_BASE = "https://api.example.com/v1"

_BUNDLED_FORMATS = (OPENAI_COMPATIBLE_FORMAT, OPENAI_RESPONSE_FORMAT, ANTHROPIC_FORMAT)


class TestThreeFormatRouting:
    @pytest.mark.parametrize(
        ("fmt", "engine_type"),
        [
            (OPENAI_COMPATIBLE_FORMAT, OpenAICompatProtocol),
            (OPENAI_RESPONSE_FORMAT, OpenAIResponsesProtocol),
            (ANTHROPIC_FORMAT, AnthropicProtocol),
        ],
    )
    def test_each_format_builds_provider_with_matching_engine(
        self, fmt: str, engine_type: type
    ) -> None:
        provider = create_llm_provider(
            LLMConfig(model="m", api_key="sk-test", base_url=_BASE, interface_format=fmt)
        )
        assert isinstance(provider, HTTPStreamProvider)
        assert type(provider._protocol) is engine_type


class TestModelPassthrough:
    """Model names pass through the factory VERBATIM — a stale routing prefix
    reaches the API as part of the model name and fails there naturally."""

    def test_openai_prefixed_model_name_passes_verbatim(self) -> None:
        provider = create_llm_provider(
            LLMConfig(model="openai/gpt-x", api_key="sk-test", base_url=_BASE)
        )
        assert isinstance(provider, HTTPStreamProvider)
        assert provider._model == "openai/gpt-x"
        assert provider.get_default_model() == "openai/gpt-x"

    def test_anthropic_prefixed_model_name_passes_verbatim(self) -> None:
        provider = create_llm_provider(
            LLMConfig(
                model="anthropic/claude-x",
                api_key="sk-test",
                base_url=_BASE,
                interface_format=ANTHROPIC_FORMAT,
            )
        )
        assert isinstance(provider, HTTPStreamProvider)
        assert provider._model == "anthropic/claude-x"


class TestUrlResolution:
    """The factory resolves the final request URL (endpoint_url verbatim,
    else the engine's per-format url() join on the normalized base_url) and
    hands the provider one resolved url."""

    @pytest.mark.parametrize(
        ("fmt", "base_url", "expected_url"),
        [
            (
                OPENAI_COMPATIBLE_FORMAT,
                "https://api.example.com/v1",
                "https://api.example.com/v1/chat/completions",
            ),
            (
                OPENAI_RESPONSE_FORMAT,
                "https://api.example.com/v1",
                "https://api.example.com/v1/responses",
            ),
            # anthropic: base already ending in /v1 joins to {base}/messages
            (
                ANTHROPIC_FORMAT,
                "https://api.anthropic.com/v1",
                "https://api.anthropic.com/v1/messages",
            ),
            # anthropic: bare base gets the /v1 segment appended
            (
                ANTHROPIC_FORMAT,
                "https://api.anthropic.com",
                "https://api.anthropic.com/v1/messages",
            ),
        ],
    )
    def test_default_join_per_format(
        self, fmt: str, base_url: str, expected_url: str
    ) -> None:
        provider = create_llm_provider(
            LLMConfig(model="m", api_key="sk-test", base_url=base_url, interface_format=fmt)
        )
        assert isinstance(provider, HTTPStreamProvider)
        assert provider._url == expected_url

    @pytest.mark.parametrize("fmt", _BUNDLED_FORMATS)
    def test_endpoint_url_override_used_verbatim(self, fmt: str) -> None:
        provider = create_llm_provider(
            LLMConfig(
                model="m",
                api_key="sk-test",
                base_url=_BASE,
                endpoint_url="https://gw.example.com/full",
                interface_format=fmt,
            )
        )
        assert isinstance(provider, HTTPStreamProvider)
        assert provider._url == "https://gw.example.com/full"

    def test_base_url_normalized_before_join(self) -> None:
        provider = create_llm_provider(
            LLMConfig(
                model="m",
                api_key="sk-test",
                base_url=" https://api.example.com/v1/ ",
            )
        )
        assert provider._url == "https://api.example.com/v1/chat/completions"

    def test_empty_base_url_joins_relative_path(self) -> None:
        # No base_url and no endpoint_url: the join of the empty base is a
        # relative URL — the natural failure surfaces at request time.
        provider = create_llm_provider(LLMConfig(model="m", api_key="sk-test"))
        assert provider._url == "/chat/completions"

    def test_headers_from_llm_config_reach_provider(self) -> None:
        provider = create_llm_provider(
            LLMConfig(
                model="m",
                api_key="sk-test",
                base_url=_BASE,
                headers={"X-Custom": "v1", "Authorization": "Bearer user"},
            )
        )
        # Stored raw on the provider config; case-normalized user-wins merge
        # happens at request time in HTTPStreamProvider.stream.
        assert provider._cfg.extra_headers == {
            "X-Custom": "v1",
            "Authorization": "Bearer user",
        }

    def test_global_model_config_headers_flow_into_provider(self) -> None:
        # The plugins/defaults/llm.py path: GlobalModelConfig.to_llm_dict()
        # → LLMConfig → create_llm_provider. No dedicated headers test exists
        # in tests/unit/plugins/, so the chain is pinned here.
        gmc = GlobalModelConfig(
            model="m",
            api_key="sk-test",
            base_url=_BASE,
            headers={"X-Org": "modex"},
        )
        provider = create_llm_provider(LLMConfig(**gmc.to_llm_dict()))
        assert isinstance(provider, HTTPStreamProvider)
        assert provider._cfg.extra_headers == {"X-Org": "modex"}


class TestApiKeyEnvFallback:
    @pytest.mark.parametrize(
        ("fmt", "env_name"),
        [
            (OPENAI_COMPATIBLE_FORMAT, "OPENAI_API_KEY"),
            (OPENAI_RESPONSE_FORMAT, "OPENAI_API_KEY"),
            (ANTHROPIC_FORMAT, "ANTHROPIC_API_KEY"),
        ],
    )
    def test_empty_api_key_falls_back_to_engine_env_var(
        self, fmt: str, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(env_name, raising=False)
        monkeypatch.setenv(env_name, "env-key")
        provider = create_llm_provider(
            LLMConfig(model="m", api_key="", base_url=_BASE, interface_format=fmt)
        )
        assert provider._cfg.api_key == "env-key"

    @pytest.mark.parametrize(
        ("fmt", "env_name"),
        [
            (OPENAI_COMPATIBLE_FORMAT, "OPENAI_API_KEY"),
            (ANTHROPIC_FORMAT, "ANTHROPIC_API_KEY"),
        ],
    )
    def test_empty_api_key_without_env_var_is_none(
        self, fmt: str, env_name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(env_name, raising=False)
        provider = create_llm_provider(
            LLMConfig(model="m", api_key="", base_url=_BASE, interface_format=fmt)
        )
        assert provider._cfg.api_key is None

    def test_explicit_api_key_wins_over_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        provider = create_llm_provider(LLMConfig(model="m", api_key="sk-explicit", base_url=_BASE))
        assert provider._cfg.api_key == "sk-explicit"
