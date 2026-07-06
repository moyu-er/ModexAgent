"""Unit tests for TodoCompletionProbeHook."""
from __future__ import annotations

import pytest

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import LLMResponse, TodoStatus, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import JsonFileTodoStore, TodoItem
from modex_agent.tools.standard import TodoReadTool
from modex_agent.tools.standard.todo_probe import (
    PROBE_XML,
    TodoCompletionProbeHook,
)


def _tm(with_todo_read: bool, store: JsonFileTodoStore) -> InMemoryToolManager:
    tm = InMemoryToolManager()
    if with_todo_read:
        tm.register(TodoReadTool(store))
    return tm


def _make_ctx(tm: InMemoryToolManager, session_id: str = "s1") -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str(session_id), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test", history=ListMessageHistory(), tool_manager=tm,
        identity=state.identity, runtime=runtime,
        session=SessionInfo.from_str(session_id),
    )


def _response(*, content: str = "I'm done.", tool_calls=None) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=list(tool_calls or []), finish_reason="stop")


@pytest.mark.asyncio
async def test_no_injection_when_response_already_has_tool_calls(tmp_path):
    store = JsonFileTodoStore(tmp_path)
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(True, store))
    ctx = _make_ctx(hook._tool_manager)
    sid = ctx.session.session_id
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING)])
    resp = _response(tool_calls=[ToolCall(tool_name="other", call_id="x", arguments={})])

    await hook.after_llm_response(ctx, resp)

    assert resp.tool_calls == [ToolCall(tool_name="other", call_id="x", arguments={})]
    assert PROBE_XML not in (resp.content or "")
    assert TurnCustomKey.TODO_PROBE not in ctx.runtime.state.custom


@pytest.mark.asyncio
async def test_no_injection_when_todo_read_not_registered(tmp_path):
    store = JsonFileTodoStore(tmp_path)
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(False, store))
    ctx = _make_ctx(hook._tool_manager)
    sid = ctx.session.session_id
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING)])
    resp = _response()

    await hook.after_llm_response(ctx, resp)

    assert resp.tool_calls == []
    assert PROBE_XML not in (resp.content or "")


@pytest.mark.asyncio
async def test_no_injection_when_active_list_empty(tmp_path):
    """Normal end: only completed/cancelled items -> nothing to probe."""
    store = JsonFileTodoStore(tmp_path)
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(True, store))
    ctx = _make_ctx(hook._tool_manager)
    sid = ctx.session.session_id
    await store.save(sid, [TodoItem("done", TodoStatus.COMPLETED),
                           TodoItem("skipped", TodoStatus.CANCELLED)])
    resp = _response()

    await hook.after_llm_response(ctx, resp)

    assert resp.tool_calls == []
    assert PROBE_XML not in (resp.content or "")


@pytest.mark.asyncio
async def test_first_ending_with_todos_injects_todo_read(tmp_path):
    """Scenario: end with todos -> probe once."""
    store = JsonFileTodoStore(tmp_path)
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(True, store))
    ctx = _make_ctx(hook._tool_manager)
    sid = ctx.session.session_id
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING),
                           TodoItem("b", TodoStatus.IN_PROGRESS)])
    resp = _response(content="I'm done.")

    await hook.after_llm_response(ctx, resp)

    assert len(resp.tool_calls) == 1
    probe = resp.tool_calls[0]
    assert probe.tool_name == "todo_read"
    assert probe.arguments == {}
    assert probe.call_id.startswith("todo-probe-")
    assert "I'm done." in resp.content          # original preserved
    assert resp.content.endswith(PROBE_XML)      # XML appended
    st = ctx.runtime.state.custom[TurnCustomKey.TODO_PROBE]
    assert st["count"] == 1
    assert st["fp"]              # fingerprint recorded


@pytest.mark.asyncio
async def test_second_ending_same_todos_does_not_inject(tmp_path):
    """Scenario: end with the SAME todos a second time -> let it end."""
    store = JsonFileTodoStore(tmp_path)
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(True, store))
    ctx = _make_ctx(hook._tool_manager)
    sid = ctx.session.session_id
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING)])

    await hook.after_llm_response(ctx, _response())          # 1st: inject
    resp2 = _response(content="still done.")
    await hook.after_llm_response(ctx, resp2)                # 2nd same: skip

    assert resp2.tool_calls == []
    assert PROBE_XML not in (resp2.content or "")
    assert ctx.runtime.state.custom[TurnCustomKey.TODO_PROBE]["count"] == 2


@pytest.mark.asyncio
async def test_progress_then_end_again_re_injects(tmp_path):
    """Scenario: prompted -> continued (made progress, list changed) -> ends
    again with remaining todos -> probe again (budget reset by new fingerprint)."""
    store = JsonFileTodoStore(tmp_path)
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(True, store))
    ctx = _make_ctx(hook._tool_manager)
    sid = ctx.session.session_id
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING),
                           TodoItem("b", TodoStatus.PENDING)])
    await hook.after_llm_response(ctx, _response())          # probe fp1
    # agent advanced 'a' to in_progress -> fingerprint changes
    await store.save(sid, [TodoItem("a", TodoStatus.IN_PROGRESS),
                           TodoItem("b", TodoStatus.PENDING)])
    resp2 = _response(content="more to do.")
    await hook.after_llm_response(ctx, resp2)                # new fp -> probe again

    assert len(resp2.tool_calls) == 1
    assert resp2.tool_calls[0].tool_name == "todo_read"
    assert ctx.runtime.state.custom[TurnCustomKey.TODO_PROBE]["count"] == 1


@pytest.mark.asyncio
async def test_no_injection_on_llm_error(tmp_path):
    """An LLM error response must not be probed — the turn ends as LLM_ERROR
    before any injected tool call could run, so probing only dirties state."""
    from modex_agent.core.constants import FinishReason

    store = JsonFileTodoStore(tmp_path)
    sid = "s1"
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING)])
    tm = _tm(True, store)
    ctx = _make_ctx(tm, session_id=sid)
    hook = TodoCompletionProbeHook(store=store, tool_manager=tm)
    resp = LLMResponse(content="", tool_calls=[], finish_reason=FinishReason.ERROR.value)

    await hook.after_llm_response(ctx, resp)

    assert resp.tool_calls == []
    assert PROBE_XML not in (resp.content or "")
    assert TurnCustomKey.TODO_PROBE not in ctx.runtime.state.custom


@pytest.mark.asyncio
async def test_injection_is_in_memory_not_session(tmp_path):
    """The hook only mutates the response object — the XML lands on the
    response (which flows into ctx.history / LLM memory) but the user-facing
    stream is emitted earlier inside llm_client.call, so it can never reach it.
    The hook holds no emitter reference, so stream-safety is structural; here
    we assert the memory-side effect only. (Stream-safety is additionally
    proven by the LLMNode integration test.)"""
    store = JsonFileTodoStore(tmp_path)
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(True, store))
    ctx = _make_ctx(hook._tool_manager)
    sid = ctx.session.session_id
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING)])
    resp = _response(content="I'm done.")

    await hook.after_llm_response(ctx, resp)

    assert PROBE_XML in (resp.content or "")   # XML is on the response (memory)
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].tool_name == "todo_read"


@pytest.mark.asyncio
async def test_no_injection_when_store_never_written(tmp_path):
    """The "无todo的结束情况" scenario: a session that has never written a
    todo list (fresh store) must end without probing — the active list is
    empty, so there is nothing to remind the agent about."""
    store = JsonFileTodoStore(tmp_path)  # never saved to
    hook = TodoCompletionProbeHook(store=store, tool_manager=_tm(True, store))
    ctx = _make_ctx(hook._tool_manager)
    resp = _response()

    await hook.after_llm_response(ctx, resp)

    assert resp.tool_calls == []
    assert PROBE_XML not in (resp.content or "")
    assert TurnCustomKey.TODO_PROBE not in ctx.runtime.state.custom


@pytest.mark.asyncio
async def test_no_injection_and_no_crash_when_runtime_is_none(tmp_path):
    """``ctx.runtime`` is nullable (e.g. contexts built outside a turn). A
    non-empty todo list must NOT crash the hook and must NOT inject — the
    ``if ctx.runtime is None: return`` guard short-circuits before any state
    access."""
    store = JsonFileTodoStore(tmp_path)
    sid = "s1"
    await store.save(sid, [TodoItem("a", TodoStatus.PENDING)])
    tm = _tm(True, store)

    class _CtxNoRuntime:
        """Minimal context shape: session + runtime=None + tool_manager. The
        hook reads only ``ctx.runtime`` (and, after the guard, session id) —
        never reaches further because the None guard returns first."""
        session = SessionInfo.from_str(sid)
        runtime = None
        tool_manager = tm

    ctx = _CtxNoRuntime()
    hook = TodoCompletionProbeHook(store=store, tool_manager=tm)
    resp = _response()

    await hook.after_llm_response(ctx, resp)  # must not raise

    assert resp.tool_calls == []
    assert PROBE_XML not in (resp.content or "")
