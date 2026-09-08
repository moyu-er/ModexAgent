# ruff: noqa: ANN401
"""Shared fixtures for ReAct node tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from modex_agent.agents.react.context import ReActGraphContext
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import (
    AgentRuntime,
    AgentRuntimeServices,
    require_runtime_state,
)
from modex_agent.tools.manager import InMemoryToolManager
from modex_graph import (
    GraphPersistenceCoordinator,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)

type StateFactory = Callable[[], ReActTurnState]
type CoordinatorFactory = Callable[[], GraphPersistenceCoordinator]
type RuntimeFactory = Callable[[], AgentRuntime]
type GraphContextFactory = Callable[[AgentRuntime | None, ReActTurnState | None], ReActGraphContext]
type ResponseFactory = Callable[..., LLMResponse]


class _AutoRegCoord(GraphPersistenceCoordinator):
    def collect_consumable_delivers(self, node_id: str, invocation_id: int) -> list[Any]:
        if self.get_deliver_store(node_id) is None:
            self.register_node(node_id)
        return super().collect_consumable_delivers(node_id, invocation_id)

    def route_deliver(
        self,
        target_node_id: str,
        content: Any,
        source_node_id: str,
        source_invocation_id: int,
        source_node_name: str | None = None,
        stage: bool = False,
    ) -> int | None:
        if self.get_deliver_store(target_node_id) is None:
            self.register_node(target_node_id)
        return super().route_deliver(
            target_node_id,
            content,
            source_node_id,
            source_invocation_id,
            source_node_name,
            stage,
        )


@pytest.fixture
def auto_reg_coord() -> type[_AutoRegCoord]:
    return _AutoRegCoord


@pytest.fixture
def make_state() -> StateFactory:
    def factory() -> ReActTurnState:
        return ReActTurnState(
            identity=TurnIdentity(
                agent_id="test",
                session=SessionInfo.from_str("s1"),
                turn_id="t1",
            ),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        )

    return factory


@pytest.fixture
def make_coordinator(
    auto_reg_coord: type[_AutoRegCoord],
) -> CoordinatorFactory:
    def factory() -> GraphPersistenceCoordinator:
        return auto_reg_coord(
            graph_instance_id=0,
            instance_store=NullGraphInstanceStore(),
            node_state_store=NullNodeStateStore(0),
            default_deliver_store_factory=NullDeliverStoreFactory(),
        )

    return factory


@pytest.fixture
def make_runtime(make_state: StateFactory) -> RuntimeFactory:
    def factory() -> AgentRuntime:
        return AgentRuntime(
            services=AgentRuntimeServices(),
            state=make_state(),
        )

    return factory


@pytest.fixture
def make_graph_ctx(
    make_runtime: RuntimeFactory,
    make_coordinator: CoordinatorFactory,
) -> GraphContextFactory:
    def factory(
        runtime: AgentRuntime | None = None,
        state: ReActTurnState | None = None,
    ) -> ReActGraphContext:
        test_runtime = runtime if runtime is not None else make_runtime()
        test_state = (
            state if state is not None else require_runtime_state(test_runtime, ReActTurnState)
        )
        agent_ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            identity=test_state.identity,
            runtime=test_runtime,
            session=SessionInfo.from_str("test.agent"),
        )
        return ReActGraphContext(
            state=test_state,
            runtime=ReactGraphRuntime(),
            user_data=agent_ctx,
            coordinator=make_coordinator(),
        )

    return factory


@pytest.fixture
def make_response() -> ResponseFactory:
    def factory(
        content: str | None = "Done!",
        reasoning_content: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        finish_reason: str = "stop",
        error: str | None = None,
        reasoning_signature: str | None = None,
        reasoning_item_id: str | None = None,
        reasoning_encrypted_content: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls if tool_calls is not None else [],
            finish_reason=FinishReason(finish_reason),
            error=error,
            reasoning_signature=reasoning_signature,
            reasoning_item_id=reasoning_item_id,
            reasoning_encrypted_content=reasoning_encrypted_content,
        )

    return factory
