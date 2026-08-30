from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.types import LLMResponse, MessageRole
from modex_agent.trace.cassette import CassetteRecorder, CassetteReplayEngine


class _ScriptedProvider(CallbackStreamProvider):
    def get_default_model(self) -> str:
        return "fixture-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        return LLMResponse(content="recorded")


def test_fresh_replay_engine_has_zero_misses(tmp_path: Path) -> None:
    engine = CassetteReplayEngine(tmp_path)

    assert engine.misses == 0


async def test_unknown_llm_lookup_increments_misses(tmp_path: Path) -> None:
    recorder = CassetteRecorder(tmp_path)
    cassette_dir = recorder.save("empty")
    engine = CassetteReplayEngine(cassette_dir)
    engine.load()
    replay = engine.wrap_provider(_ScriptedProvider())

    # Call chat_stream directly, not chat(): chat() routes through the
    # provider retry wrapper, whose _is_transient matches bare digit
    # substrings ("429"/"500"/"502"...) in the error text. A per-run
    # content-addressed key (created_at is part of the key payload) can
    # randomly contain such a substring, re-triggering the lookup and
    # counting a second miss — a ~7% flake. The miss counter, not retry
    # semantics, is this test's subject.
    with pytest.raises(KeyError, match="Cassette miss"):
        await replay.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="unknown")]
        )

    assert engine.misses == 1


async def test_successful_llm_replay_leaves_misses_at_zero(tmp_path: Path) -> None:
    messages = [ChatMessage(role=MessageRole.USER, content="known")]
    recorder = CassetteRecorder(tmp_path)
    await recorder.wrap_provider(_ScriptedProvider()).chat(messages=messages)
    cassette_dir = recorder.save("known")
    engine = CassetteReplayEngine(cassette_dir)
    engine.load()

    result = await engine.wrap_provider(_ScriptedProvider()).chat(messages=messages)

    assert result.content == "recorded"
    assert engine.misses == 0


def test_unknown_tool_lookup_increments_misses(tmp_path: Path) -> None:
    engine = CassetteReplayEngine(tmp_path)

    with pytest.raises(KeyError, match="Cassette miss"):
        engine._lookup_tool("unknown")

    assert engine.misses == 1
