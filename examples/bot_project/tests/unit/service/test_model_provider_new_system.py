# tests/unit/service/test_model_provider_new_system.py
"""model.yml → BotModelConfig → BotModelProvider → create_llm_provider chain (T25).

Locks the bot-side wiring of the new provider system (ADR-0046): model.yml
providers with headers/top_p/endpoint_url and all three interface formats
resolve through ``BotModelProvider._real_provider`` to
``HTTPStreamProvider`` instances carrying the right engine, headers, top_p,
and endpoint_url — plus the per-(provider, model) cache hit guarantee.

No network: construction-time configuration assertions only. The transport
behavior of HTTPStreamProvider is covered by
``tests/integration/providers/test_http_provider_e2e.py`` (MockTransport).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import current_model_choice
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider

from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    ToolCallDelta,
)
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.formats.openai_responses import OpenAIResponsesProtocol
from modex_agent.providers.http.provider import HTTPStreamProvider

_YML = """
models:
  default_provider: "Compat"
  default_model: "M1"
  providers:
    - key: compat
      name: "Compat"
      base_url: https://compat.example.com/v1
      interface_format: openai_compatible
      api_key: sk-compat
      headers:
        X-Org: modex
        Authorization: "Bearer user"
      endpoint_url: "https://gw.compat.example.com/full"
      models:
        - {name: M1, model: m1, temperature: 0.3, top_p: 0.5, max_output_tokens: 1000}
        - {name: M2, model: m2}
    - key: resp
      name: "Resp"
      base_url: https://resp.example.com/v1
      interface_format: openai_response
      api_key: sk-resp
      headers:
        X-Trace: "t1"
      models:
        - {name: R1, model: r1, top_p: 0.8}
    - key: anth
      name: "Anth"
      base_url: https://api.anthropic.com
      interface_format: anthropic
      api_key: sk-anth
      models:
        - {name: A1, model: claude-sonnet-4-5, top_p: 0.33}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


@pytest.mark.parametrize(
    ("provider_name", "model_name", "engine_type"),
    [
        ("Compat", "M1", OpenAICompatProtocol),
        ("Resp", "R1", OpenAIResponsesProtocol),
        ("Anth", "A1", AnthropicProtocol),
    ],
)
def test_three_formats_route_to_matching_engines(
    tmp_path: Path, provider_name: str, model_name: str, engine_type: type
) -> None:
    async def go() -> None:
        cfg = _cfg(tmp_path)
        prov = BotModelProvider(cfg)
        try:
            resolved = cfg.resolve(provider_name, model_name)
            assert resolved is not None
            real = prov._real_provider(resolved)
            assert isinstance(real, HTTPStreamProvider)
            assert type(real._protocol) is engine_type
        finally:
            await prov.aclose()

    asyncio.run(go())


def test_headers_and_endpoint_url_flow_into_provider(tmp_path: Path) -> None:
    async def go() -> None:
        cfg = _cfg(tmp_path)
        prov = BotModelProvider(cfg)
        try:
            resolved = cfg.resolve("Compat", "M1")
            assert resolved is not None
            real = prov._real_provider(resolved)
            assert isinstance(real, HTTPStreamProvider)
            assert real._cfg.extra_headers == {
                "X-Org": "modex",
                "Authorization": "Bearer user",
            }
            # endpoint_url flows through the factory as the resolved
            # request URL, used verbatim (no per-format join).
            assert real._url == "https://gw.compat.example.com/full"
        finally:
            await prov.aclose()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("provider_name", "model_name", "expected_top_p"),
    [
        ("Compat", "M1", 0.5),
        ("Compat", "M2", 0.95),  # top_p absent in yml → synthesize default
        ("Resp", "R1", 0.8),
        ("Anth", "A1", 0.33),
    ],
)
def test_model_level_top_p_reaches_provider(
    tmp_path: Path, provider_name: str, model_name: str, expected_top_p: float
) -> None:
    async def go() -> None:
        cfg = _cfg(tmp_path)
        prov = BotModelProvider(cfg)
        try:
            resolved = cfg.resolve(provider_name, model_name)
            assert resolved is not None
            real = prov._real_provider(resolved)
            assert isinstance(real, HTTPStreamProvider)
            assert real._top_p == expected_top_p
        finally:
            await prov.aclose()

    asyncio.run(go())


def test_same_model_resolution_reuses_cached_provider(tmp_path: Path) -> None:
    async def go() -> None:
        cfg = _cfg(tmp_path)
        prov = BotModelProvider(cfg)
        try:
            resolved = cfg.resolve("Compat", "M1")
            assert resolved is not None
            first = prov._real_provider(resolved)
            second = prov._real_provider(resolved)
            # Per-turn cache hit: same (provider, model) → same instance.
            assert first is second

            # A different model builds a different instance.
            other = cfg.resolve("Compat", "M2")
            assert other is not None
            third = prov._real_provider(other)
            assert third is not first
            assert len(prov._cache) == 2
        finally:
            await prov.aclose()

    asyncio.run(go())


def test_default_model_temperature_baked_into_provider(tmp_path: Path) -> None:
    async def go() -> None:
        cfg = _cfg(tmp_path)
        prov = BotModelProvider(cfg)
        try:
            resolved = cfg.resolve("Compat", "M1")
            assert resolved is not None
            real = prov._real_provider(resolved)
            assert isinstance(real, HTTPStreamProvider)
            # Model-level sampling params are baked at construction, not
            # forwarded per turn.
            assert real._temperature == 0.3
            assert real._cfg.max_output_tokens == 1000
        finally:
            await prov.aclose()

    asyncio.run(go())


# ── Native stream delegation (event fidelity + envelope rewrite) ───────────


@pytest.fixture(autouse=True)
def _reset_ctxvar() -> Generator[None, None, None]:
    token = current_model_choice.set(None)
    yield
    current_model_choice.reset(token)


class _RecordingNative:
    """Fake native provider: records the delegated envelope, replays events."""

    def __init__(self, events: list[LLMStreamEvent]) -> None:
        self._events = events
        self.called = False
        self.last_request: LLMRequest | None = None

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.called = True
        self.last_request = request
        for event in self._events:
            yield event


def test_native_stream_passes_events_through_verbatim(tmp_path: Path) -> None:
    """The bot wrapper must not fold the event stream: display-only variants
    (ToolCallDelta — the WebUI "Preparing" heartbeat source) have no callback
    channel and previously died in the chat_stream fold."""

    async def go() -> None:
        cfg = _cfg(tmp_path)
        resolved = cfg.resolve("Compat", "M1")
        assert resolved is not None
        current_model_choice.set(resolved)
        events: list[LLMStreamEvent] = [
            ReasoningDelta(text="thinking"),
            TextDelta(text="hi"),
            ToolCallDelta(call_id="c1", tool_name="write", args_fragment='{"a"'),
            ToolCallComplete(call_id="c1", tool_name="write", arguments={"a": 1}),
            Finish(finish_reason=FinishReason.TOOL_CALLS),
        ]
        fake = _RecordingNative(events)
        prov = BotModelProvider(cfg)
        prov._cache[("compat", "m1")] = fake  # type: ignore[attr-defined]
        try:
            request = LLMRequest(
                model=prov.get_default_model(),
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            )
            received = [event async for event in prov.stream(request)]
        finally:
            await prov.aclose()

        assert fake.called
        assert received == events  # verbatim, incl. ToolCallDelta

    asyncio.run(go())


def test_native_stream_rewrites_model_and_clears_placeholders(tmp_path: Path) -> None:
    """The delegated envelope carries the RESOLVED model identity; the
    framework's placeholder sampling params never reach the wire (the real
    provider owns them, baked at construction)."""

    async def go() -> None:
        cfg = _cfg(tmp_path)
        resolved = cfg.resolve("Compat", "M2")  # non-default model
        assert resolved is not None
        current_model_choice.set(resolved)
        fake = _RecordingNative([Finish(finish_reason=FinishReason.STOP)])
        prov = BotModelProvider(cfg)
        prov._cache[("compat", "m2")] = fake  # type: ignore[attr-defined]
        try:
            request = LLMRequest(
                model=prov.get_default_model(),  # "m1" placeholder
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
                temperature=0.9,
                top_p=0.9,
                max_output_tokens=123,
            )
            async for _ in prov.stream(request):
                pass
        finally:
            await prov.aclose()

        assert fake.last_request is not None
        assert fake.last_request.model == "m2"
        assert fake.last_request.temperature is None
        assert fake.last_request.top_p is None
        assert fake.last_request.max_output_tokens is None

    asyncio.run(go())


def test_native_stream_placeholder_config_fails_fast() -> None:
    """No real model configured → one StreamFailure terminal event (folded by
    the assembler into an ERROR LLMResponse — the legacy fail-fast contract)."""

    async def go() -> None:
        from bot.service.model_config import _placeholder_model_config

        prov = BotModelProvider(_placeholder_model_config())
        try:
            request = LLMRequest(
                model=prov.get_default_model(),
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            )
            received = [event async for event in prov.stream(request)]
        finally:
            await prov.aclose()

        assert len(received) == 1
        assert isinstance(received[0], StreamFailure)
        assert "no model configured" in received[0].error_info.message

    asyncio.run(go())
