"""Tests verifying DispatchDeadline per-chunk renewal during LLM streaming.

Verifies that:
1. renew_dispatch_deadline() is called on every content/reasoning delta
2. The default renew amount is 3.0 seconds (DispatchDeadline.DEFAULT_RENEW_SECONDS)
3. The sliding ceiling (DEFAULT_MAX_AHEAD_SECONDS = 1200s) caps each renew's
   forward reach, but slides forward with each renew — so continuous activity
   can keep the turn alive indefinitely.
4. Both the chained (LLM_STREAM interceptor) and plain event-loop paths renew
   per-event
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.stream_events import LLMStreamEvent
from modex_agent.core.types import LLMResponse
from modex_agent.interceptor.abc import InterceptorScope
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.dispatch import (
    DispatchDeadline,
    current_dispatch_deadline,
    renew_dispatch_deadline,
)
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


def _make_ctx():
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.tool_manager import InMemoryToolManager

    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


class _PassthroughInterceptorChain:
    def has_scope(self, scope: InterceptorScope) -> bool:
        return scope == InterceptorScope.LLM_STREAM

    async def around_llm_stream(
        self, ctx, call, events: AsyncIterator[LLMStreamEvent]
    ) -> AsyncIterator[LLMStreamEvent]:
        async for event in events:
            yield event


class _FakeEmitter:
    def wants_streaming(self) -> bool:
        return True

    async def emit(self, event, data=None):
        pass

    async def emit_delta(self, delta: str):
        pass

    async def emit_stream_end(self, resuming: bool = False):
        pass


class _RenewCountingDeadline(DispatchDeadline):
    """Wraps DispatchDeadline to count renew() calls and their arguments."""

    def __init__(
        self,
        initial_timeout: float,
        *,
        max_ahead_seconds: float | None = None,
        default_renew_seconds: float | None = None,
    ):
        super().__init__(
            initial_timeout,
            max_ahead_seconds=max_ahead_seconds,
            default_renew_seconds=default_renew_seconds,
        )
        self.renew_count = 0
        self.renew_args: list[float | None] = []

    def renew(self, seconds: float | None = None) -> None:
        self.renew_count += 1
        self.renew_args.append(seconds)
        super().renew(seconds)


class _TrackingStreamProvider(CallbackStreamProvider):
    """Streams deltas through callbacks, mirroring real provider behavior."""

    def __init__(
        self,
        deltas: list[str],
        reasoning_deltas: list[str] | None = None,
        delay: float = 0.0,
    ):
        self._deltas = deltas
        self._reasoning_deltas = reasoning_deltas or []
        self._delay = delay

    def get_default_model(self) -> str:
        return "mock"

    async def chat(self, messages, **kw):  # pragma: no cover
        raise RuntimeError("not used")

    async def chat_stream(self, messages, *, on_content_delta, on_reasoning_delta, **kw):
        for delta in self._deltas:
            await on_content_delta(delta)
            if self._delay > 0:
                await asyncio.sleep(self._delay)
        for delta in self._reasoning_deltas:
            await on_reasoning_delta(delta)
            if self._delay > 0:
                await asyncio.sleep(self._delay)
        return LLMResponse(content="".join(self._deltas), finish_reason="stop")


class TestDispatchDeadlineDefaults:
    def test_default_renew_seconds_is_3(self):
        assert DispatchDeadline.DEFAULT_RENEW_SECONDS == 3.0

    def test_default_max_ahead_seconds_is_1200(self):
        assert DispatchDeadline.DEFAULT_MAX_AHEAD_SECONDS == 1200.0

    def test_renew_dispatch_deadline_noop_when_unset(self):
        assert current_dispatch_deadline.get() is None
        renew_dispatch_deadline()


class TestPerChunkRenewalStreamWithControl:
    """The chained event-loop path must call renew_dispatch_deadline() on every delta."""

    @pytest.mark.asyncio
    async def test_content_delta_renews_deadline_each_chunk(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _TrackingStreamProvider(["a", "b", "c"])
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        deadline = _RenewCountingDeadline(initial_timeout=0.01, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(deadline)

        try:
            await ReactLlmClient(provider).call([], ctx)
        finally:
            current_dispatch_deadline.reset(token)

        assert deadline.renew_count == 3
        assert all(s is None for s in deadline.renew_args)  # instance default (3.0)

    @pytest.mark.asyncio
    async def test_reasoning_delta_renews_deadline_each_chunk(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _TrackingStreamProvider(
            deltas=["text"],
            reasoning_deltas=["think1", "think2"],
        )
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        deadline = _RenewCountingDeadline(initial_timeout=0.01, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(deadline)

        try:
            await ReactLlmClient(provider).call([], ctx)
        finally:
            current_dispatch_deadline.reset(token)

        assert deadline.renew_count == 3
        assert all(s is None for s in deadline.renew_args)  # instance default (3.0)

    @pytest.mark.asyncio
    async def test_renew_uses_default_3_seconds(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _TrackingStreamProvider(["x"])
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        deadline = DispatchDeadline(initial_timeout=0.0, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(deadline)

        try:
            assert deadline.is_expired
            await ReactLlmClient(provider).call([], ctx)
            assert not deadline.is_expired
            assert 2.5 <= deadline.remaining <= 3.1
        finally:
            current_dispatch_deadline.reset(token)

    @pytest.mark.asyncio
    async def test_renew_keeps_deadline_alive_across_slow_chunks(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _TrackingStreamProvider(["a", "b", "c"], delay=0.02)
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        deadline = DispatchDeadline(initial_timeout=0.05, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(deadline)

        try:
            await ReactLlmClient(provider).call([], ctx)
            assert not deadline.is_expired
        finally:
            current_dispatch_deadline.reset(token)


class TestPerChunkRenewalStreamPlain:
    """The plain event-loop path must also call renew_dispatch_deadline() on every delta."""

    @pytest.mark.asyncio
    async def test_content_delta_renews_in_plain_stream(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _TrackingStreamProvider(["a", "b"])

        deadline = _RenewCountingDeadline(initial_timeout=0.01, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(deadline)

        try:
            await ReactLlmClient(provider).call([], ctx)
        finally:
            current_dispatch_deadline.reset(token)

        assert deadline.renew_count == 2
        assert all(s is None for s in deadline.renew_args)  # instance default (3.0)

    @pytest.mark.asyncio
    async def test_reasoning_delta_renews_in_plain_stream(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _TrackingStreamProvider(
            deltas=["text"],
            reasoning_deltas=["think1", "think2"],
        )

        deadline = _RenewCountingDeadline(initial_timeout=0.01, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(deadline)

        try:
            await ReactLlmClient(provider).call([], ctx)
        finally:
            current_dispatch_deadline.reset(token)

        assert deadline.renew_count == 3


class TestCeilingDuringStreaming:
    """Repeated per-chunk renewals must not exceed the hard ceiling."""

    @pytest.mark.asyncio
    async def test_many_chunks_capped_by_ceiling(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        many_deltas = [f"chunk{i}" for i in range(200)]
        provider = _TrackingStreamProvider(many_deltas)
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        deadline = DispatchDeadline(
            initial_timeout=0.5,
            max_ahead_seconds=1.0,
        )
        token = current_dispatch_deadline.set(deadline)

        try:
            await ReactLlmClient(provider).call([], ctx)
        finally:
            current_dispatch_deadline.reset(token)

        assert deadline.remaining <= 1.0

    @pytest.mark.asyncio
    async def test_streaming_extends_past_initial_timeout_without_exceeding_ceiling(self):
        ctx = _make_ctx()
        ctx.emitter = _FakeEmitter()
        provider = _TrackingStreamProvider(["a", "b", "c", "d", "e"], delay=0.02)
        ctx.runtime.services.interceptors = _PassthroughInterceptorChain()

        deadline = DispatchDeadline(
            initial_timeout=0.05,
            max_ahead_seconds=0.3,
        )
        token = current_dispatch_deadline.set(deadline)

        try:
            await ReactLlmClient(provider).call([], ctx)
            assert not deadline.is_expired
            assert deadline.remaining <= 0.3
        finally:
            current_dispatch_deadline.reset(token)


class TestRenewDispatchDeadlineHelper:
    def test_renew_with_default_3s(self):
        d = DispatchDeadline(initial_timeout=0.0, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(d)
        try:
            time.sleep(0.01)
            assert d.is_expired
            renew_dispatch_deadline()
            assert not d.is_expired
            assert d.remaining > 2.5
        finally:
            current_dispatch_deadline.reset(token)

    def test_renew_with_explicit_seconds(self):
        d = DispatchDeadline(initial_timeout=0.0, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(d)
        try:
            time.sleep(0.01)
            renew_dispatch_deadline(0.1)
            assert 0.05 < d.remaining <= 0.11
        finally:
            current_dispatch_deadline.reset(token)
