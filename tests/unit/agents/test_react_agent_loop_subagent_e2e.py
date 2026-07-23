"""Subagent loop detection end-to-end: LoopDetectionHook -> ReActAgent.run -> SubagentAutoSendHook -> parent inbox."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.hook import HookErrorPolicy, HookSpec, HookRunner
from modex_agent.hook.builtin import SubagentAutoSendHook
from modex_agent.hook.builtin.loop_detection import LoopDetectionHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


class _FakeEmitter:
    def __init__(self):
        self.completed = None
        self.contents = []

    def wants_streaming(self):
        return False

    async def emit(self, *a, **k):
        pass

    async def emit_delta(self, d):
        pass

    async def emit_content(self, full):
        if full:
            self.contents.append(full)

    async def emit_stream_end(self, resuming=False):
        pass

    async def emit_complete(self, result):
        self.completed = result

    async def emit_error(self, error):
        pass


class _FakeBus:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []

    async def send(self, key: str, envelope: object) -> None:
        self.sent.append((key, envelope))


def _make_subagent_ctx(parent_session_id: str = "conv123.main"):
    session = SessionInfo(
        session_id="inv1.scout",
        agent_name="scout",
        parent_session_id=parent_session_id,
    )
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="t", session=session, turn_id="u"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        max_iterations=5,
        comm_kind=AgentCommKind.SUBAGENT,
        identity=state.identity,
        runtime=AgentRuntime(
            services=AgentRuntimeServices(hooks=HookRunner()),
            state=state,
        ),
    )


@pytest.mark.asyncio
async def test_subagent_loop_routes_to_parent_inbox():
    """A subagent that hits LOOP_DETECTED must notify its parent, not the user."""
    bus = _FakeBus()
    ctx = _make_subagent_ctx()
    # Seed a prior assistant step with the same content AND the same tool call,
    # so the AND-based loop detector fires on the first LLM response.
    await ctx.history.append(
        ChatMessage(
            role="assistant",
            content="I am stuck doing the same thing.",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c0")],
        )
    )

    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content="I am stuck doing the same thing.",
            finish_reason=FinishReason.STOP.value,
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"})],
        )
    )
    provider.get_default_model = lambda: "mock"
    agent = ReActAgent(provider=provider)

    ctx.runtime.services.hooks.add(
        HookSpec(
            hook=LoopDetectionHook(window_size=2, content_similarity_threshold=0.85),
            on_error=HookErrorPolicy.LOG,
        )
    )
    ctx.runtime.services.hooks.add(
        HookSpec(
            hook=SubagentAutoSendHook(
                agent_bus=bus,
                self_name="scout",
                parent_name="main",
            ),
            on_error=HookErrorPolicy.LOG,
        )
    )

    emitter = _FakeEmitter()
    result = await agent.run(ctx, emitter)

    # ReActAgent should surface LOOP_DETECTED.
    assert result.stop_reason == StopReason.LOOP_DETECTED
    assert "<loop_detected" in (result.content or "")

    # SubagentAutoSendHook should route the notification to the parent inbox.
    assert len(bus.sent) == 1
    key, envelope = bus.sent[0]
    assert key == "conv123.main"
    assert envelope.message_type == AgentMessageType.AGENT_RESULT
    xml = envelope.payload["content"]
    assert "<subagent_result>" in xml
    assert "loop" in xml.lower()
    assert "<success>false</success>" in xml


# Import ReActTurnState here to avoid a top-level import cycle in some test runners.
from modex_agent.agents.react.state import ReActTurnState  # noqa: E402
