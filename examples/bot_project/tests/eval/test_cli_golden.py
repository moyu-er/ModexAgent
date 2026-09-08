from datetime import UTC, datetime

import pytest
import typer
from bot.eval import cli as eval_cli

from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.providers import HTTPStreamProvider
from modex_agent.trace.cassette import llm_call_key


def _message_key(message: ChatMessage) -> str:
    return llm_call_key(
        [message.to_dict()],
        model="test-model",
        temperature=0.7,
        max_output_tokens=None,
        tools=None,
        kwargs={},
    )


def test_golden_serialization_ignores_message_creation_time() -> None:
    first = ChatMessage(
        role=MessageRole.USER,
        content="same request",
        created_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    )
    second = first.model_copy(update={"created_at": datetime(2026, 8, 15, 11, tzinfo=UTC)})

    with eval_cli._stable_golden_message_serialization():
        first_key = _message_key(first)
        second_key = _message_key(second)

    assert first_key == second_key


def test_golden_serialization_restores_normal_message_output() -> None:
    message = ChatMessage(
        role=MessageRole.USER,
        content="request",
        created_at=datetime(2026, 8, 15, 10, tzinfo=UTC),
    )

    with eval_cli._stable_golden_message_serialization():
        assert "created_at" not in message.to_dict()

    assert message.to_dict()["created_at"] == "2026-08-15 10:00:00"


def test_golden_provider_requires_recording_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("TEST_LLM_API_KEY", "TEST_LLM_BASE_URL", "TEST_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(typer.Exit):
        eval_cli._golden_provider_from_env()


async def test_golden_provider_builds_direct_http_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-key")
    monkeypatch.setenv("TEST_LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("TEST_LLM_MODEL", "test-model")

    provider = eval_cli._golden_provider_from_env()
    try:
        assert isinstance(provider, HTTPStreamProvider)
    finally:
        await provider.aclose()
