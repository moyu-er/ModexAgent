"""ReActAgent turns a LoopDetectedError into a LOOP_DETECTED AgentResult."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.control.exceptions import LoopDetectedError
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import LLMResponse, ToolCall


def _make_ctx():
    from modex_agent.core.agent import AgentContext
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.runtime.enums import AgentKind, TurnPhase
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_agent.hook import HookRunner

    state = ReActTurnState(
        identity=TurnIdentity(agent_id="t", session=SessionInfo.from_str("s"), turn_id="u"),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    return AgentContext(
        system_prompt="", history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(), session=SessionInfo.from_str("s.a"),
        max_iterations=5,
        identity=state.identity, runtime=AgentRuntime(services=AgentRuntimeServices(hooks=HookRunner()), state=state),
    )


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


@pytest.mark.asyncio
async def test_loop_detected_renders_loop_result(monkeypatch):
    # Wire LoopDetectionHook into the context's hook runner so the real path fires.
    from modex_agent.hook import HookErrorPolicy, HookSpec
    from modex_agent.hook.builtin.loop_detection import LoopDetectionHook

    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LLMResponse(
        content="I am stuck doing the same thing.", finish_reason=FinishReason.STOP.value,
        tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"})],
    ))
    provider.get_default_model = lambda: "mock"
    agent = ReActAgent(provider=provider)

    ctx = _make_ctx()
    # Seed a prior assistant step with the same content AND the same tool call,
    # so the AND-based loop detector fires on the first LLM response.
    await ctx.history.append(
        ChatMessage(
            role="assistant",
            content="I am stuck doing the same thing.",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c0")],
        )
    )
    ctx.runtime.services.hooks.add(
        HookSpec(hook=LoopDetectionHook(window_size=2, content_similarity_threshold=0.85),
                 on_error=HookErrorPolicy.LOG)
    )
    emitter = _FakeEmitter()

    result = await agent.run(ctx, emitter)

    assert result.stop_reason == StopReason.LOOP_DETECTED
    assert "<loop_detected" in (result.content or "")
    assert emitter.completed is result
