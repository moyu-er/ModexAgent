"""Tests for modex_agent.providers.http.protocol — LLMProtocol ABC + envelopes.

A stub engine implements the full contract, proving the ABC is subclassable
and every member callable (including the default classify_http_error
delegation and the api_key_env property). Incomplete engines fail to
instantiate, and the two frozen value objects enforce immutability and a
closed shape.
"""

from __future__ import annotations

from abc import ABCMeta
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError

from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorKind
from modex_agent.core.message import ChatMessage
from modex_agent.core.stream_events import LLMStreamEvent, TextDelta
from modex_agent.core.types import MessageRole
from modex_agent.providers.http.errors import classify_http_error
from modex_agent.providers.http.protocol import LLMProtocol, ProtocolConfig, WireRequest
from modex_agent.providers.http.sse import SseFrame


class _StubProtocol(LLMProtocol):
    """Minimal concrete engine exercising every ABC member."""

    def build_body(self, request: LLMRequest, cfg: ProtocolConfig) -> dict[str, Any]:
        return {"model": request.model, "stream": True, "parse_think_tags": cfg.parse_think_tags}

    def url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/chat/completions"

    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        if api_key is None:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    async def events(self, frames: AsyncIterator[SseFrame]) -> AsyncIterator[LLMStreamEvent]:
        async for frame in frames:
            yield TextDelta(text=frame.data)

    @property
    def api_key_env(self) -> str:
        return "OPENAI_API_KEY"


def _frames(*frames: SseFrame) -> AsyncIterator[SseFrame]:
    """In-memory frame stream — zero network."""

    async def _gen() -> AsyncIterator[SseFrame]:
        for frame in frames:
            yield frame

    return _gen()


async def test_stub_engine_satisfies_the_full_contract() -> None:
    engine = _StubProtocol()
    request = LLMRequest(
        model="stub-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )
    cfg = ProtocolConfig()

    assert engine.build_body(request, cfg) == {
        "model": "stub-model",
        "stream": True,
        "parse_think_tags": True,
    }
    assert engine.url("https://api.example.com/v1") == "https://api.example.com/v1/chat/completions"
    assert engine.auth_headers("sk-secret") == {"Authorization": "Bearer sk-secret"}
    assert engine.auth_headers(None) == {}
    assert engine.api_key_env == "OPENAI_API_KEY"

    events = [
        event
        async for event in engine.events(_frames(SseFrame(data='{"a": 1}'), SseFrame(data="b")))
    ]
    assert events == [TextDelta(text='{"a": 1}'), TextDelta(text="b")]


_ABSTRACT_MEMBERS = ("build_body", "url", "auth_headers", "events", "api_key_env")


@pytest.mark.parametrize("member", _ABSTRACT_MEMBERS)
def test_engine_missing_one_abstract_member_cannot_instantiate(member: str) -> None:
    """Re-abstract exactly one member on the complete stub → TypeError."""
    incomplete = ABCMeta(
        "_IncompleteEngine",
        (_StubProtocol,),
        {member: getattr(LLMProtocol, member)},
    )
    with pytest.raises(TypeError, match=member):
        incomplete()


def test_abc_itself_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError, match="abstract"):
        LLMProtocol()


def test_default_classify_http_error_delegates_to_the_shared_classifier() -> None:
    engine = _StubProtocol()
    status, body, provider = 429, b'{"error": {"message": "slow down"}}', "stub"
    headers = {"Retry-After": "30"}
    got = engine.classify_http_error(status, body, provider, headers)
    assert got == classify_http_error(status, body, provider, headers)
    assert got.retry_after_seconds == 30.0


def test_default_classify_http_error_works_without_headers() -> None:
    engine = _StubProtocol()
    info = engine.classify_http_error(401, b"", "stub")
    assert info.kind is LLMErrorKind.AUTH
    assert info.should_retry is False


class TestWireRequest:
    def test_holds_the_translated_request(self) -> None:
        wire = WireRequest(
            url="https://api.example.com/v1/chat/completions",
            headers={"Authorization": "Bearer sk-secret"},
            body={"model": "stub-model", "stream": True},
        )
        assert wire.url == "https://api.example.com/v1/chat/completions"
        assert wire.headers == {"Authorization": "Bearer sk-secret"}
        assert wire.body == {"model": "stub-model", "stream": True}

    def test_frozen_rejects_mutation(self) -> None:
        wire = WireRequest(url="https://api.example.com", headers={}, body={})
        with pytest.raises(ValidationError):
            wire.url = "https://other.example.com"  # type: ignore[misc]

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WireRequest(url="u", headers={}, body={}, stream=True)


class TestProtocolConfig:
    def test_defaults(self) -> None:
        cfg = ProtocolConfig()
        assert cfg.api_key is None
        assert cfg.max_output_tokens is None
        assert cfg.reasoning_effort is ReasoningEffort.NONE
        assert cfg.extra_headers == {}
        assert cfg.store is True
        assert cfg.parse_think_tags is True
        assert cfg.extra_body is None

    def test_explicit_values_held(self) -> None:
        cfg = ProtocolConfig(
            api_key="sk-secret",
            max_output_tokens=4096,
            reasoning_effort=ReasoningEffort.HIGH,
            extra_headers={"X-Custom": "v"},
            store=False,
            parse_think_tags=False,
            extra_body={"thinking": {"budget_tokens": 1024}},
        )
        assert cfg.api_key == "sk-secret"
        assert cfg.max_output_tokens == 4096
        assert cfg.reasoning_effort is ReasoningEffort.HIGH
        assert cfg.extra_headers == {"X-Custom": "v"}
        assert cfg.store is False
        assert cfg.parse_think_tags is False
        assert cfg.extra_body == {"thinking": {"budget_tokens": 1024}}

    def test_frozen_rejects_mutation(self) -> None:
        cfg = ProtocolConfig()
        with pytest.raises(ValidationError):
            cfg.store = False  # type: ignore[misc]

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProtocolConfig(unknown=True)
