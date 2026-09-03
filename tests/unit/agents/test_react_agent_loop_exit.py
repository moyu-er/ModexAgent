"""ReActAgent turns a LoopDetectedError into a LOOP_DETECTED AgentResult."""
import pytest

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.core.emitter import StopReason
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage, ToolCall
from modex_agent.core.provider import CallbackStreamProvider


def _make_ctx():
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.hook import HookRunner
    from modex_agent.memory.history import ListMessageHistory
    from modex_agent.runtime.enums import AgentKind, TurnPhase
    from modex_agent.runtime.models import TurnIdentity
    from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
    from modex_agent.tools.manager import InMemoryToolManager

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


class _ScriptedProvider(CallbackStreamProvider):
    """chat-only scripted mock riding the callback→event bridge."""

    def __init__(self, response: LLMResponse):
        super().__init__()
        self._response = response

    def get_default_model(self) -> str:
        return "mock"

    async def chat_stream(self, messages, *, on_content_delta=None, on_reasoning_delta=None, **kw):
        return self._response


@pytest.mark.asyncio
async def test_loop_detected_renders_loop_result(monkeypatch):
    # Wire LoopDetectionHook into the context's hook runner so the real path fires.
    from modex_agent.hook import HookErrorPolicy, HookSpec
    from modex_agent.hook.builtin.loop_detection import LoopDetectionHook

    provider = _ScriptedProvider(LLMResponse(
        content="I am stuck doing the same thing.", finish_reason=FinishReason.STOP.value,
        tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"})],
    ))
    agent = ReActAgent(provider=provider)

    ctx = _make_ctx()
    # Seed one prior assistant round with the same tool call. The scripted
    # provider keeps returning it, so the two-stage detector injects a
    # reminder once the trailing run hits window_size (2) and force-exits
    # after observation_rounds (2) more rounds.
    await ctx.history.append(
        ChatMessage(
            role="assistant",
            content="I am stuck doing the same thing.",
            tool_calls=[ToolCall(tool_name="read", arguments={"path": "/a"}, call_id="c0")],
        )
    )
    ctx.runtime.services.hooks.add(
        HookSpec(hook=LoopDetectionHook(window_size=2, observation_rounds=2),
                 on_error=HookErrorPolicy.LOG)
    )
    emitter = _FakeEmitter()

    result = await agent.run(ctx, emitter)

    assert result.stop_reason == StopReason.LOOP_DETECTED
    assert "Loop detected" in (result.content or "")
    assert "read" in (result.content or "")
    assert emitter.completed is result
