"""Tests for InjectionDrainer.drain — message loss prevention (P1-2r2).

Migrated from tests/unit/agents/test_drain_injections.py (which tested
ReActAgent._drain_injections). Behaviour and assertions are identical; only
the call site changed from `agent._drain_injections(ctx)` to
`InjectionDrainer().drain(ctx)`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.core.session_id import SessionInfo
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelCapabilities


class _FakeHistory:
    """History that can be configured to fail on append."""

    def __init__(self, fail_on_append: bool = False):
        self.messages: list = []
        self._fail_on_append = fail_on_append
        self._append_calls = 0

    async def append(self, message):
        self._append_calls += 1
        if self._fail_on_append and self._append_calls == 1:
            raise RuntimeError("history append failed")
        self.messages.append(message)

    async def replace_all(self, messages):
        self.messages = list(messages)

    def __iter__(self):
        return iter(self.messages)


class _FakeContext:
    def __init__(self, *, history=None, injection_queue=None):
        self.messages = [{"role": "user", "content": "hello"}]
        self.history = history or _FakeHistory()
        self.max_iterations = 5
        self.attachments: list = []
        self.tool_manager = None
        self.temperature = 0.7
        self.max_output_tokens = None
        self.session_id = "test-session"
        state = TurnStateBase(
            identity=TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"),
            agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING,
        )
        services = AgentRuntimeServices(
            pending_input_queue=injection_queue,
        )
        self.runtime: Any = AgentRuntime(services=services, state=state)

    async def to_messages(self):
        return list(self.messages)

    def get_tool_descriptions(self, caps: ModelCapabilities | None = None):
        return None


class TestDrainInjectionsMessagePreservation:
    """P1-2r2: Injected messages must not be lost when history.append fails."""

    async def test_message_returned_to_queue_on_append_failure(self):
        """When history.append fails, the message is put back into the queue."""
        injection_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5)
        await injection_queue.put("msg-to-inject")
        assert injection_queue.qsize() == 1

        # History that fails on first append
        history = _FakeHistory(fail_on_append=True)

        # Call drain directly to isolate the test
        ctx = _FakeContext(history=history, injection_queue=injection_queue)
        injected = await InjectionDrainer().drain(ctx)

        # Assert: message should be back in queue (put_back on failure)
        assert injection_queue.qsize() >= 1, (
            f"Expected message to be returned to queue, but qsize={injection_queue.qsize()}"
        )

    async def test_messages_not_lost_during_normal_operation(self):
        """Multiple messages injected correctly without loss."""
        injection_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5)
        await injection_queue.put("msg-1")
        await injection_queue.put("msg-2")
        assert injection_queue.qsize() == 2

        history = _FakeHistory(fail_on_append=False)
        ctx = _FakeContext(history=history, injection_queue=injection_queue)

        injected = await InjectionDrainer().drain(ctx)

        # Both messages should be in history
        assert len(injected) == 2
        assert injected == ["msg-1", "msg-2"]
        assert injection_queue.qsize() == 0

        # History should have the injected messages
        history_messages = [m for m in history.messages
                           if "Injected during execution" in m.get("content", "")]
        assert len(history_messages) == 2
