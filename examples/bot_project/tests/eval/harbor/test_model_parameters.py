from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.eval.harbor import entry as entry_module
from bot.eval.harbor.agent import POOL_MODE_ENV_VARS
from bot.eval.harbor.entry import EntryConfig
from bot.eval.harbor.pool_mode_types import build_model_config
from bot.service import model_provider as model_provider_module
from bot.service.model_provider import BotModelProvider

from modex_agent.core.constants import FinishReason, ReasoningEffort
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.types import LLMResponse, MessageRole
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.runtime.models import JsonValue


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "LLM_MODEL": "openai/scripted-model",
        "MODEX_EXPERIMENT_ID": "exp-id",
        "MODEX_EXPERIMENT_NAME": "terminal-bench.model-params",
        "MODEX_EXPERIMENT_DATASET_ID": "dataset-id",
        "MODEX_EXPERIMENT_ITEM_ID": "item-id",
        "MODEX_MEMORY_NS": "model-params",
        "MODEX_TASK_INPUT_DIR": str(tmp_path),
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "logs"),
    }


def test_entry_config_defaults_model_parameters(tmp_path: Path) -> None:
    # Given
    environment = _environment(tmp_path)

    # When
    config = EntryConfig.from_environment(environment)

    # Then
    assert config.temperature == 0.7
    assert config.reasoning_effort is ReasoningEffort.NONE
    assert config.max_context_tokens == 200000
    assert config.max_output_tokens == 50000


def test_entry_config_parses_model_parameters(tmp_path: Path) -> None:
    # Given
    environment = _environment(tmp_path)
    environment.update(
        MODEX_TEMPERATURE="1.0",
        MODEX_REASONING_EFFORT="high",
        MODEX_MAX_CONTEXT_TOKENS="500000",
        MODEX_MAX_OUTPUT_TOKENS="256000",
    )

    # When
    config = EntryConfig.from_environment(environment)

    # Then
    assert config.temperature == 1.0
    assert config.reasoning_effort is ReasoningEffort.HIGH
    assert config.max_context_tokens == 500000
    assert config.max_output_tokens == 256000


def test_entry_config_rejects_invalid_temperature(tmp_path: Path) -> None:
    # Given
    environment = _environment(tmp_path)
    environment["MODEX_TEMPERATURE"] = "hot"

    # When / Then
    with pytest.raises(ValueError, match="hot"):
        EntryConfig.from_environment(environment)


def test_entry_config_rejects_invalid_max_context_tokens(tmp_path: Path) -> None:
    # Given
    environment = _environment(tmp_path)
    environment["MODEX_MAX_CONTEXT_TOKENS"] = "huge"

    # When / Then
    with pytest.raises(ValueError, match="MODEX_MAX_CONTEXT_TOKENS"):
        EntryConfig.from_environment(environment)


def test_entry_config_rejects_invalid_max_output_tokens(tmp_path: Path) -> None:
    # Given
    environment = _environment(tmp_path)
    environment["MODEX_MAX_OUTPUT_TOKENS"] = "huge"

    # When / Then
    with pytest.raises(ValueError, match="MODEX_MAX_OUTPUT_TOKENS"):
        EntryConfig.from_environment(environment)


def test_entry_config_rejects_invalid_reasoning_effort_with_valid_values(
    tmp_path: Path,
) -> None:
    # Given
    environment = _environment(tmp_path)
    environment["MODEX_REASONING_EFFORT"] = "turbo"

    # When
    with pytest.raises(ValueError) as error:
        EntryConfig.from_environment(environment)

    # Then
    message = str(error.value)
    assert all(effort.value in message for effort in ReasoningEffort)


def test_pool_model_config_receives_model_parameters(tmp_path: Path) -> None:
    # Given
    environment = _environment(tmp_path)
    environment.update(
        MODEX_TEMPERATURE="1.0",
        MODEX_REASONING_EFFORT="high",
        MODEX_MAX_CONTEXT_TOKENS="500000",
        MODEX_MAX_OUTPUT_TOKENS="256000",
    )
    entry_config = EntryConfig.from_environment(environment)

    # When
    model_config = build_model_config(entry_config)

    # Then
    model = model_config.providers[0].models[0]
    assert model.temperature == 1.0
    assert model.reasoning_effort is ReasoningEffort.HIGH
    assert model.max_output_tokens == 256000
    assert model_config.max_context_tokens == 500000


@pytest.mark.asyncio
async def test_bare_provider_receives_model_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    environment = _environment(tmp_path)
    environment.update(MODEX_TEMPERATURE="1.0", MODEX_REASONING_EFFORT="high")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MODEX_AGENT_MODE", raising=False)
    provider_factory = MagicMock()
    bare_execute = AsyncMock()

    # When
    with (
        patch.object(entry_module, "LiteLLMProvider", provider_factory),
        patch.object(entry_module, "execute_entry", bare_execute),
    ):
        await entry_module._run_from_environment()

    # Then
    provider_factory.assert_called_once_with(
        "openai/scripted-model",
        api_key=None,
        base_url=None,
        temperature=1.0,
        reasoning_effort=ReasoningEffort.HIGH,
    )


def test_pool_mode_env_vars_include_model_parameters() -> None:
    # Given / When
    names = set(POOL_MODE_ENV_VARS)

    # Then
    assert {
        "MODEX_TEMPERATURE",
        "MODEX_REASONING_EFFORT",
        "MODEX_MAX_CONTEXT_TOKENS",
        "MODEX_MAX_OUTPUT_TOKENS",
    } <= names
    assert {"MODEX_TASK_NAME", "MODEX_TASK_WORKSPACE"} <= names


class _BakedRealProvider(LLMProvider):
    """Scripted stand-in for the provider create_llm_provider builds."""

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = messages, model, temperature, max_output_tokens, tools, kwargs
        return LLMResponse(content="ok", finish_reason=FinishReason.STOP)

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:  # noqa: ANN401
        _ = kwargs
        return LLMResponse(content="ok", finish_reason=FinishReason.STOP)

    def get_default_model(self) -> str:
        return "openai/scripted-model"


@pytest.mark.asyncio
async def test_pool_provider_bakes_max_output_tokens_from_entry(
    tmp_path: Path,
) -> None:
    # Given
    environment = _environment(tmp_path)
    environment["MODEX_MAX_OUTPUT_TOKENS"] = "256000"
    entry_config = EntryConfig.from_environment(environment)
    model_config = build_model_config(entry_config)
    captured: list[LLMConfig] = []

    def capture_factory(config: LLMConfig) -> LLMProvider:
        captured.append(config)
        return _BakedRealProvider()

    # When
    with patch.object(model_provider_module, "create_llm_provider", capture_factory):
        provider = BotModelProvider(model_config)
        response = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")]
        )

    # Then
    assert response.content == "ok"
    assert [config.max_output_tokens for config in captured] == [256000]
