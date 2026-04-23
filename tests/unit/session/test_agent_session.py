"""Unit tests for session/agent_session.py.

TDD: verify AgentSession.process_message, clear_session, startup, shutdown
and correct integration with context_manager and tool_manager.
"""

import asyncio
from typing import Any

import pytest

from framework.core.agent import Agent, AgentContext
from framework.core.context import InMemoryContextManager
from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.events import AgentEvent, EmitterConfig
from framework.core.tool_manager import FunctionalTool, InMemoryToolManager
from framework.core.types import InputMessage
from framework.memory.history import ListMessageHistory
from framework.session.agent_session import AgentSession


async def _history_to_list(history):
    if hasattr(history, "to_list"):
        return await history.to_list()
    return list(history)


class _FakeEvent(AgentEvent):
    TEST = "test"


class _FakeEmitter(ContentEmitter[_FakeEvent]):
    def __init__(self):
        super().__init__(EmitterConfig())
        self.deltas = []
        self.errors = []

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)

    async def emit_complete(self, result: AgentResult) -> None:
        pass


class _FakeAgent(Agent[_FakeEvent]):
    event_enum = _FakeEvent
    max_iterations = 3

    @property
    def name(self) -> str:
        return "fake_agent"

    async def run(self, context: AgentContext, emitter: ContentEmitter[_FakeEvent], streaming: bool = True):
        self.last_context = context
        await emitter.emit_delta("Hello")
        await context.history.append({"role": "assistant", "content": "Hello"})
        return AgentResult(content="Hello", stop_reason="complete")


class _FailingAgent(Agent[_FakeEvent]):
    event_enum = _FakeEvent
    max_iterations = 3

    @property
    def name(self) -> str:
        return "failing_agent"

    async def run(self, context: AgentContext, emitter: ContentEmitter[_FakeEvent], streaming: bool = True):
        raise RuntimeError("agent boom")


class TestAgentSession:
    @pytest.fixture
    def components(self):
        cm = InMemoryContextManager(base_system_prompt="You are a tester")
        tm = InMemoryToolManager()
        return cm, tm

    @pytest.mark.asyncio
    async def test_process_message_basic_flow(self, components):
        cm, tm = components
        session = AgentSession(agent=_FakeAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        msg = InputMessage(content="hi")
        result = await session.process_message(msg, emitter, session_id="u1")

        assert result.content == "Hello"
        assert result.stop_reason == "complete"
        assert "Hello" in emitter.deltas

        # Verify context persisted
        state = await cm.load("u1")
        history = await _history_to_list(state.history)
        assert len(history) == 2  # user + assistant
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_process_message_builds_system_prompt(self, components):
        cm, tm = components
        session = AgentSession(agent=_FakeAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        msg = InputMessage(content="hello")
        await session.process_message(msg, emitter, session_id="u2")

        state = await cm.load("u2")
        assert "You are a tester" in state.system_prompt

    @pytest.mark.asyncio
    async def test_process_message_error_handling(self, components):
        cm, tm = components
        session = AgentSession(agent=_FailingAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        msg = InputMessage(content="hi")
        result = await session.process_message(msg, emitter, session_id="u3")

        assert result.stop_reason == "error"
        assert "agent boom" in result.error.lower()
        assert len(emitter.errors) == 1
        assert "agent boom" in emitter.errors[0].lower()

    @pytest.mark.asyncio
    async def test_clear_session(self, components):
        cm, tm = components
        session = AgentSession(agent=_FakeAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        await session.process_message(InputMessage(content="h"), emitter, session_id="u4")
        await session.clear_session("u4")

        state = await cm.load("u4")
        history = await _history_to_list(state.history)
        assert history == []

    @pytest.mark.asyncio
    async def test_startup_shutdown(self, components):
        cm, tm = components
        session = AgentSession(agent=_FakeAgent(), context_manager=cm, tool_manager=tm)
        await session.startup()
        assert tm._thread_pool is not None
        await session.shutdown()
        assert tm._thread_pool is None

    @pytest.mark.asyncio
    async def test_process_message_with_tool_manager(self, components):
        cm, tm = components
        tm.register(
            FunctionalTool(
                name="echo",
                description="Echo",
                parameters={"type": "object", "properties": {}},
                func=lambda x: x,
            )
        )
        session = AgentSession(agent=_FakeAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        msg = InputMessage(content="use echo")
        result = await session.process_message(msg, emitter, session_id="u5")

        assert result.content == "Hello"

    @pytest.mark.asyncio
    async def test_process_message_includes_metadata(self, components):
        cm, tm = components
        # Use empty base prompt so system_prompt is rebuilt and runtime_info is merged
        cm = InMemoryContextManager(base_system_prompt="")
        session = AgentSession(agent=_FakeAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        msg = InputMessage(content="m")
        await session.process_message(
            msg, emitter, session_id="u6", runtime_info={"platform": "test_os"}
        )
        state = await cm.load("u6")
        assert "test_os" in state.system_prompt

    @pytest.mark.asyncio
    async def test_process_message_passes_runtime_info_to_save_metadata(self, components):
        cm, tm = components
        session = AgentSession(agent=_FakeAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        msg = InputMessage(content="hello")
        await session.process_message(
            msg, emitter, session_id="u_meta", runtime_info={"user_id": "alice", "platform": "qq"}
        )
        state = await cm.load("u_meta")
        assert state.metadata["input_metadata"].get("user_id") == "alice"
        assert state.metadata["input_metadata"].get("platform") == "qq"
        assert state.metadata.get("finish_reason") == "complete"

    @pytest.mark.asyncio
    async def test_user_message_visible_in_agent_context(self, components):
        """Critical regression test: current user message must be in AgentContext.history."""
        cm, tm = components
        agent = _FakeAgent()
        session = AgentSession(agent=agent, context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()

        # Pre-populate history: save user, then agent appends its own assistant message
        await cm.save(
            session_id="u7",
            user_message={"role": "user", "content": "previous"},
            assistant_result=AgentResult(),
        )
        state = await cm.load("u7")
        await state.history.append({"role": "assistant", "content": "prev reply"})

        await session.process_message(InputMessage(content="current"), emitter, session_id="u7")

        assert agent.last_context is not None
        history = await _history_to_list(agent.last_context.history)
        # History now includes the assistant response saved after run completes
        assert len(history) == 4
        assert history[0]["role"] == "user" and history[0]["content"] == "previous"
        assert history[1]["role"] == "assistant" and history[1]["content"] == "prev reply"
        assert history[2]["role"] == "user" and history[2]["content"] == "current"
        assert history[3]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_injected_messages_are_saved(self, components):
        """Hook 注入的消息（如 subagent_result）应被保存到 context_manager。"""
        cm, tm = components

        class _InjectingAgent(Agent[_FakeEvent]):
            event_enum = _FakeEvent
            max_iterations = 3

            @property
            def name(self) -> str:
                return "injecting_agent"

            async def run(self, context: AgentContext, emitter: ContentEmitter[_FakeEvent], streaming: bool = True):
                # 模拟 InboxFlushHook 注入消息
                injected = {
                    "role": "user",
                    "content": "[From agent:worker]\n\nsub result",
                    "meta_source": "worker",
                }
                await context.history.append(injected)
                return AgentResult(content="done", stop_reason="completed")

        session = AgentSession(agent=_InjectingAgent(), context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()
        await session.process_message(InputMessage(content="hi"), emitter, session_id="u_injected")

        state = await cm.load("u_injected")
        history = await _history_to_list(state.history)
        injected_in_history = [m for m in history if m.get("meta_source") == "worker"]
        assert len(injected_in_history) == 1
        assert "sub result" in injected_in_history[0]["content"]

    @pytest.mark.asyncio
    async def test_process_message_passes_session_id_to_context(self, components):
        """AgentSession 应将 session_id 传递给 AgentContext。"""
        cm, tm = components
        agent = _FakeAgent()
        session = AgentSession(agent=agent, context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()

        await session.process_message(
            InputMessage(content="test"),
            emitter,
            session_id="test_sess_123",
        )

        assert agent.last_context is not None
        assert agent.last_context.session_id == "test_sess_123"

    @pytest.mark.asyncio
    async def test_process_message_injects_attachments_to_history(self, components):
        """AgentSession 应将 result.attachments 注入到最后一条 assistant 消息的 metadata。"""
        cm, tm = components

        class _AgentWithAttachment(Agent[_FakeEvent]):
            event_enum = _FakeEvent
            max_iterations = 3

            @property
            def name(self) -> str:
                return "agent_with_attachment"

            async def run(self, context: AgentContext, emitter: ContentEmitter[_FakeEvent], streaming: bool = True):
                self.last_context = context
                await emitter.emit_delta("file generated")
                await context.history.append({"role": "assistant", "content": "file generated"})
                return AgentResult(
                    content="file generated",
                    stop_reason="complete",
                    attachments=["/tmp/test.txt"],
                )

        agent = _AgentWithAttachment()
        session = AgentSession(agent=agent, context_manager=cm, tool_manager=tm)
        emitter = _FakeEmitter()

        await session.process_message(
            InputMessage(content="generate file"),
            emitter,
            session_id="test_attach",
        )

        state = await cm.load("test_attach")
        history = await _history_to_list(state.history)
        assistant_msgs = [m for m in history if m.get("role") == "assistant"]
        assert assistant_msgs
        assert assistant_msgs[-1].get("metadata", {}).get("attachments") == ["/tmp/test.txt"]


class _FakeDreamEngine:
    def __init__(self):
        self.runs: list[Any] = []
        self._delay = 0.0

    async def run(self, ctx: Any) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        self.runs.append(ctx)


@pytest.mark.asyncio
class TestAgentSessionDreamEngine:
    async def test_dream_engine_triggered_when_threshold_met(self, tmp_path):
        from pathlib import Path

        from framework.memory.system import MemorySystem, MemorySystemContextManager

        ms = MemorySystem(workspace=Path(tmp_path))
        await ms.initialize()
        adapter = MemorySystemContextManager(ms)

        dream = _FakeDreamEngine()
        agent = _FakeAgent()
        tm = InMemoryToolManager()
        session = AgentSession(agent=agent, context_manager=adapter, tool_manager=tm, dream_engine=dream, dream_threshold=2)

        emitter = _FakeEmitter()
        msg = InputMessage(content="hi")

        # Seed 2 unprocessed history entries before processing
        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="dream_session", user_id="default")
        await ms._managers.history.append(ctx, "summary 1", {"source": "test"})
        await ms._managers.history.append(ctx, "summary 2", {"source": "test"})

        await session.process_message(msg, emitter, session_id="dream_session")

        # Give the background task a moment to run
        await asyncio.sleep(0.05)

        assert len(dream.runs) == 1
        await ms.close()

    async def test_dream_engine_not_triggered_when_under_threshold(self, tmp_path):
        from pathlib import Path

        from framework.memory.system import MemorySystem, MemorySystemContextManager

        ms = MemorySystem(workspace=Path(tmp_path))
        await ms.initialize()
        adapter = MemorySystemContextManager(ms)

        dream = _FakeDreamEngine()
        agent = _FakeAgent()
        tm = InMemoryToolManager()
        session = AgentSession(agent=agent, context_manager=adapter, tool_manager=tm, dream_engine=dream, dream_threshold=5)

        emitter = _FakeEmitter()
        msg = InputMessage(content="hi")

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="dream_session2", user_id="default")
        await ms._managers.history.append(ctx, "summary 1", {"source": "test"})

        await session.process_message(msg, emitter, session_id="dream_session2")

        await asyncio.sleep(0.05)

        assert len(dream.runs) == 0
        await ms.close()

    async def test_dream_engine_scope_lock_prevents_concurrent_runs(self, tmp_path):
        from pathlib import Path

        from framework.memory.system import MemorySystem, MemorySystemContextManager

        ms = MemorySystem(workspace=Path(tmp_path))
        await ms.initialize()
        adapter = MemorySystemContextManager(ms)

        dream = _FakeDreamEngine()
        dream._delay = 0.1  # slow enough to overlap
        agent = _FakeAgent()
        tm = InMemoryToolManager()
        session = AgentSession(agent=agent, context_manager=adapter, tool_manager=tm, dream_engine=dream, dream_threshold=1)

        from framework.memory.core.scope import MemoryContext

        ctx = MemoryContext(session_id="lock_session", user_id="default")
        await ms._managers.history.append(ctx, "summary 1", {"source": "test"})

        # Fire two triggers in rapid succession for the same session
        await session._maybe_trigger_dream("lock_session")
        await session._maybe_trigger_dream("lock_session")

        await asyncio.sleep(0.3)

        # Because they share the same scope lock, the second call should wait
        # for the first to finish, so both run sequentially (total 2 runs).
        assert len(dream.runs) == 2
        await ms.close()

    @pytest.mark.asyncio
    async def test_process_message_recovers_from_checkpoint(self, tmp_path):
        from pathlib import Path

        from framework.memory.system import MemorySystem, MemorySystemContextManager

        ms = MemorySystem(workspace=Path(tmp_path))
        await ms.initialize()
        adapter = MemorySystemContextManager(ms)

        agent = _FakeAgent()
        tm = InMemoryToolManager()
        session = AgentSession(agent=agent, context_manager=adapter, tool_manager=tm)
        emitter = _FakeEmitter()

        # 预先写入 checkpoint（模拟崩溃残留）
        recovered_msgs = [
            {"role": "assistant", "content": "Partial response before crash"},
        ]
        await adapter.save_checkpoint("recover_session", recovered_msgs)

        result = await session.process_message(
            InputMessage(content="continue"), emitter, session_id="recover_session"
        )

        # 恢复后的消息应在历史中
        state = await adapter.load("recover_session")
        history = await _history_to_list(state.history)
        assert any("Partial response before crash" in str(m.get("content", "")) for m in history)

        # checkpoint 应被清除
        ck = await adapter.load_checkpoint("recover_session")
        assert ck is None or ck == []

        await ms.close()

    @pytest.mark.asyncio
    async def test_process_message_sanitizes_incomplete_tool_calls_on_recovery(self, tmp_path):
        from pathlib import Path

        from framework.memory.system import MemorySystem, MemorySystemContextManager

        ms = MemorySystem(workspace=Path(tmp_path))
        await ms.initialize()
        adapter = MemorySystemContextManager(ms)

        agent = _FakeAgent()
        tm = InMemoryToolManager()
        session = AgentSession(agent=agent, context_manager=adapter, tool_manager=tm)
        emitter = _FakeEmitter()

        # 模拟崩溃时 assistant 发出了 tool_calls 但尚未收到 tool 结果
        recovered_msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    }
                ],
            },
        ]
        await adapter.save_checkpoint("sanitize_session", recovered_msgs)

        await session.process_message(
            InputMessage(content="continue"), emitter, session_id="sanitize_session"
        )

        # Check raw short-term storage (bypasses filter_tool_messages)
        from framework.memory.core.scope import MemoryContext
        ctx = MemoryContext(session_id="sanitize_session")
        raw_msgs = await ms.get_history(ctx)
        tool_msgs = [m for m in raw_msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "tc_1"
        assert "interrupted" in tool_msgs[0]["content"].lower()

        await ms.close()
