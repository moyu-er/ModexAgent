"""Tests for context construction — system prompt, multi-agent context, routing."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from modex_agent.core.agent import AgentContext
from modex_agent.core.context import InMemoryContextManager
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory


class TestContextManagerConstruction:
    """验证不同 context_manager 类型的构造。"""

    async def test_inmemory_context_manager_save_load(self) -> None:
        cm = InMemoryContextManager(base_system_prompt="test prompt")
        state = await cm.load("s1")
        assert state.system_prompt == "test prompt"

    async def test_inmemory_context_system_prompt_construction(self) -> None:
        cm = InMemoryContextManager(base_system_prompt="You are helpful")
        prompt = await cm.build_system_prompt(tool_manager=MagicMock())
        assert "You are helpful" in (prompt or "")

    async def test_inmemory_context_is_reusable(self) -> None:
        cm = InMemoryContextManager(base_system_prompt="inmemory")
        state1 = await cm.load("any_id")
        state2 = await cm.load("any_id")
        assert state1.system_prompt == "inmemory"
        assert state2.system_prompt == "inmemory"


class TestAgentContextConstruction:
    """验证 AgentContext 构造和字段。"""

    def test_minimal_agent_context(self) -> None:
        """Minimal AgentContext has correct defaults."""
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("s1"),
        )
        assert str(ctx.session) == "s1"
        assert ctx.system_prompt == "test"
        assert ctx.max_iterations == 10
        assert ctx.runtime is None

    def test_agent_context_with_runtime_context_manager(self) -> None:
        """AgentContext with runtime passes RuntimeContextManager through services."""
        from modex_agent.core.runtime_context import RuntimeContextManager
        from modex_agent.runtime.enums import AgentKind, TurnPhase
        from modex_agent.runtime.models import TurnIdentity, TurnStateBase
        from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

        mgr = RuntimeContextManager()
        identity = TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1")
        state = TurnStateBase(
            identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED
        )
        services = AgentRuntimeServices(runtime_context_manager=mgr)
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("s1"),
            runtime=AgentRuntime(services=services, state=state),
            identity=identity,
        )
        assert ctx.runtime is not None
        assert ctx.runtime.services.runtime_context_manager is mgr

    def test_agent_context_with_safety_policy(self) -> None:
        """AgentContext passes safety policy through services."""
        from modex_agent.runtime.enums import AgentKind, TurnPhase
        from modex_agent.runtime.models import TurnIdentity, TurnStateBase
        from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

        safety = MagicMock()
        identity = TurnIdentity(agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1")
        state = TurnStateBase(
            identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED
        )
        services = AgentRuntimeServices(safety=safety)
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("s1"),
            runtime=AgentRuntime(services=services, state=state),
            identity=identity,
        )
        assert ctx.runtime.services.safety is safety


class TestAgentContextIsolation:
    """验证不同 session 之间 AgentContext 的隔离性。"""

    def test_different_sessions_have_independent_contexts(self) -> None:
        """Different sessions have independent state."""
        ctx1 = AgentContext(
            system_prompt="prompt1",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("s1"),
        )
        ctx2 = AgentContext(
            system_prompt="prompt2",
            history=ListMessageHistory([]),
            tool_manager=MagicMock(),
            session=SessionInfo.from_str("s2"),
        )
        assert str(ctx1.session) != str(ctx2.session)
        assert ctx1.system_prompt != ctx2.system_prompt
