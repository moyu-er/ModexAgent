"""Cassette legacy-usage replay + bridge key-surface regression tests (Todo 22).

Pins two T2/T13 verbal conclusions as regression tests:

1. Old cassettes recorded before TokenUsage typing carry provider wire keys on
   disk (``prompt_tokens`` / ``prompt_cache_hit_tokens`` / ``completion_tokens``).
   The replay path is ``_llm_response_from_dict`` →
   ``TokenUsage.model_validate(d.get("usage") or {})`` — the before-validator
   normalizes legacy keys, and a missing ``usage`` key yields the all-zero
   default. No production change was needed (verified: this is the only
   LLMResponse construction path in the replay engine, no ``model_construct``
   bypass).

2. The ``CallbackStreamProvider.stream()`` bridge deliberately never passes
   ``model=`` to ``chat_stream`` — the cassette ``llm_call_key`` hashes model
   as a key input, and the legacy client never passed it. The bridge path and
   a direct ``chat_stream`` call with the old-client kwargs face must produce
   the identical recorded key.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMResponse, TokenUsage
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.trace.cassette import CassetteRecorder, CassetteReplayEngine

_TRACE_ID = "trace-legacy-usage-001"


class _ScriptedProvider(CallbackStreamProvider):
    """chat_stream-only mock returning a canned response (standard mock shape)."""

    def __init__(self, response: LLMResponse) -> None:
        super().__init__()
        self._response = response

    def get_default_model(self) -> str:
        return "scripted-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        return self._response


class _RaisingProvider(CallbackStreamProvider):
    """Provider that raises if chat_stream is ever called (replay must not)."""

    def get_default_model(self) -> str:
        return "raising-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        raise AssertionError("Replay must not call the wrapped provider")


# ------------------------------------------------------------------
# Legacy usage-key replay
# ------------------------------------------------------------------


async def _replay_with_usage_on_disk(
    tmp_path: Path, usage_on_disk: dict[str, Any] | None
) -> LLMResponse:
    """Record one LLM call, overwrite the on-disk payload's ``response.usage``
    (simulating a pre-T2 cassette), then replay through the engine.

    ``usage_on_disk=None`` removes the key entirely.
    """
    response = LLMResponse(
        content="hi",
        usage=TokenUsage(input_tokens=80, cache_read_input_tokens=20, output_tokens=40),
    )
    provider = _ScriptedProvider(response)
    recorder = CassetteRecorder(tmp_path)
    wrapped = recorder.wrap_provider(provider)

    messages = [ChatMessage(role=MessageRole.USER, content="hi")]
    await wrapped.chat(messages=messages, temperature=0.5)
    cassette_dir = recorder.save(_TRACE_ID)

    payload_path = cassette_dir / f"{recorder.entries[0].key}.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    if usage_on_disk is None:
        del payload["response"]["usage"]
    else:
        payload["response"]["usage"] = usage_on_disk
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    engine = CassetteReplayEngine(cassette_dir)
    engine.load()
    replay_wrapped = engine.wrap_provider(_RaisingProvider())
    return await replay_wrapped.chat(messages=messages, temperature=0.5)


class TestLegacyUsageReplay:
    async def test_legacy_prompt_token_keys_normalize_on_replay(self, tmp_path: Path) -> None:
        """Given a cassette whose on-disk usage carries the legacy DeepSeek/
        OpenAI key form, When replayed, Then TokenUsage.model_validate
        normalizes: prompt_tokens includes cached tokens, so
        input = 100 - 20 = 80."""
        replayed = await _replay_with_usage_on_disk(
            tmp_path,
            {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "prompt_cache_hit_tokens": 20,
            },
        )
        assert replayed.usage.input_tokens == 80
        assert replayed.usage.cache_read_input_tokens == 20
        assert replayed.usage.output_tokens == 40
        assert replayed.usage.total_tokens == 140
        assert replayed.usage == TokenUsage(
            input_tokens=80, cache_read_input_tokens=20, output_tokens=40
        )

    async def test_missing_usage_key_defaults_to_zero_usage(self, tmp_path: Path) -> None:
        """Given a cassette record with no usage key at all, When replayed,
        Then usage is the all-zero TokenUsage default."""
        replayed = await _replay_with_usage_on_disk(tmp_path, None)
        assert replayed.usage == TokenUsage()
        assert replayed.usage.total_tokens == 0


# ------------------------------------------------------------------
# Bridge key surface (llm_call_key red line)
# ------------------------------------------------------------------


class TestBridgeKeySurface:
    async def test_bridge_records_same_key_as_direct_chat_stream(self, tmp_path: Path) -> None:
        """Given a cassette-wrapped chat_stream-only mock, When the same
        request goes through (a) the T13 ``stream()`` bridge and (b) a direct
        ``chat_stream`` call with the old-client kwargs face, Then both calls
        record the identical llm_call_key — the bridge never passes ``model=``
        and keeps ``prompt_cache_key`` as the sole kwarg."""
        provider = _ScriptedProvider(LLMResponse(content="hello"))
        recorder = CassetteRecorder(tmp_path)
        wrapped = recorder.wrap_provider(provider)

        messages = [ChatMessage(role=MessageRole.USER, content="hi")]
        request = LLMRequest(
            model="req-model",  # set on the envelope — bridge must still not pass it
            messages=messages,
            temperature=0.3,
            max_output_tokens=128,
            tools=({"name": "probe", "parameters": {}},),
            prompt_cache_key="sess-1",
        )
        # Path A: T13 bridge — stream() → chat_stream.
        _ = [event async for event in wrapped.stream(request)]
        # Path B: direct chat_stream with the old-client kwargs face (no model).
        await wrapped.chat_stream(
            messages=messages,
            temperature=0.3,
            max_output_tokens=128,
            tools=[{"name": "probe", "parameters": {}}],
            prompt_cache_key="sess-1",
        )

        entries = recorder.entries
        assert len(entries) == 2
        bridge_entry, direct_entry = entries

        # Key inputs are directly observable in the recorded request —
        # the bridge's face matches the old client's.
        bridge_request = bridge_entry.data["request"]
        assert bridge_request["model"] is None
        assert bridge_request["temperature"] == 0.3
        assert bridge_request["max_output_tokens"] == 128
        assert bridge_request["tools"] == [{"name": "probe", "parameters": {}}]
        assert bridge_request["kwargs"] == {"prompt_cache_key": "sess-1"}

        # The red line: identical llm_call_key through both paths.
        assert bridge_entry.key == direct_entry.key
