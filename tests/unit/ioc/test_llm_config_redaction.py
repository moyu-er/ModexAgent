"""Secret repr masking and new provider fields on LLMConfig / GlobalModelConfig.

Locks the todo-11 contract: ``api_key`` / ``headers`` never appear in ``repr()``
(secrets stay out of logs and debug output) while ``model_dump()`` keeps them
(persistence / config round-trip is unaffected); the ``headers`` /
``responses_store`` / ``endpoint_url`` fields exist on both config layers and
flow through ``GlobalModelConfig.to_llm_dict`` into ``LLMConfig``.

Note on probe values: assertions use distinctive secret substrings (not the
single letter used in the plan's example) because the default ``capabilities``
repr contains the type name ``Modality``, which includes the letter ``y``.
"""

from __future__ import annotations

from modex_agent.core.constants import InterfaceFormat
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.model import GlobalModelConfig

_SECRET_KEY = "sk-secret-redacted-probe"
_SECRET_HEADER_VALUE = "Bearer secret-header-probe"


class TestLLMConfigReprRedaction:
    def test_repr_masks_api_key_and_headers(self) -> None:
        cfg = LLMConfig(model="m", api_key=_SECRET_KEY, headers={"Authorization": _SECRET_HEADER_VALUE})
        r = repr(cfg)
        assert _SECRET_KEY not in r
        assert _SECRET_HEADER_VALUE not in r
        assert "Authorization" not in r

    def test_repr_omits_masked_field_names_entirely(self) -> None:
        cfg = LLMConfig(model="m", api_key=_SECRET_KEY, headers={"Authorization": _SECRET_HEADER_VALUE})
        r = repr(cfg)
        assert "api_key=" not in r
        assert "headers=" not in r

    def test_empty_headers_not_in_repr(self) -> None:
        r = repr(LLMConfig(model="m"))
        assert "headers=" not in r

    def test_model_dump_keeps_secrets(self) -> None:
        cfg = LLMConfig(model="m", api_key=_SECRET_KEY, headers={"Authorization": _SECRET_HEADER_VALUE})
        d = cfg.model_dump()
        assert d["api_key"] == _SECRET_KEY
        assert d["headers"] == {"Authorization": _SECRET_HEADER_VALUE}


class TestLLMConfigNewFields:
    def test_defaults(self) -> None:
        cfg = LLMConfig()
        assert cfg.headers == {}
        assert cfg.responses_store is False
        assert cfg.endpoint_url == ""

    def test_field_values(self) -> None:
        cfg = LLMConfig(
            headers={"X-Custom": "v"},
            responses_store=True,
            endpoint_url="https://gateway.example.com/full-url",
        )
        assert cfg.headers == {"X-Custom": "v"}
        assert cfg.responses_store is True
        assert cfg.endpoint_url == "https://gateway.example.com/full-url"


class TestOpenAIResponseFormat:
    def test_enum_value_exists(self) -> None:
        assert InterfaceFormat.OPENAI_RESPONSE.value == "openai_response"

    def test_llm_config_accepts_openai_response(self) -> None:
        cfg = LLMConfig(interface_format=InterfaceFormat.OPENAI_RESPONSE)
        assert cfg.interface_format is InterfaceFormat.OPENAI_RESPONSE


class TestGlobalModelConfigPassthrough:
    def test_new_field_defaults(self) -> None:
        cfg = GlobalModelConfig()
        assert cfg.headers == {}
        assert cfg.responses_store is False
        assert cfg.endpoint_url == ""

    def test_to_llm_dict_passes_headers(self) -> None:
        d = GlobalModelConfig(headers={"a": "b"}).to_llm_dict()
        assert d["headers"] == {"a": "b"}

    def test_to_llm_dict_passes_responses_store_true(self) -> None:
        d = GlobalModelConfig(responses_store=True).to_llm_dict()
        assert d["responses_store"] is True

    def test_to_llm_dict_passes_endpoint_url(self) -> None:
        d = GlobalModelConfig(endpoint_url="https://gateway.example.com/full").to_llm_dict()
        assert d["endpoint_url"] == "https://gateway.example.com/full"

    def test_to_llm_dict_defaults(self) -> None:
        d = GlobalModelConfig().to_llm_dict()
        assert d["headers"] == {}
        assert d["responses_store"] is False
        assert d["endpoint_url"] == ""

    def test_to_llm_dict_constructs_llm_config(self) -> None:
        """The dict stays LLMConfig-shaped: plugins/defaults/llm.py builds
        ``LLMConfig(**model_config.to_llm_dict())`` and extra="forbid" would
        reject any stray key."""
        d = GlobalModelConfig(
            headers={"a": "b"},
            responses_store=True,
            endpoint_url="https://gateway.example.com/full",
        ).to_llm_dict()
        cfg = LLMConfig(**d)
        assert cfg.headers == {"a": "b"}
        assert cfg.responses_store is True
        assert cfg.endpoint_url == "https://gateway.example.com/full"
