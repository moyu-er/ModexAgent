"""Tests for AgentTemplate.materialize — the single construction path."""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver


def _make_deps() -> tuple[AgentMaterializeDeps, MagicMock]:
    """Build deps with a mocked agent_factory + pool."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.stop = AsyncMock()
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    pool.get = MagicMock(return_value=None)  # no parent instance
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        safety=RuntimeSafetyPolicy(),
        llm_model="gpt-4o",
        project_dir=None,  # skip prompt file read + MCP + skills
    )
    deps = dataclasses.replace(
        deps,
        context_fork_builder=ContextForkBuilder(),
        workspace_path_resolver=WorkspacePathResolver(workspace_manager=None, pool_name="main"),
    )
    return deps, factory


@pytest.mark.asyncio
async def test_materialize_subagent_only_comm_kind_subagent():
    deps, factory = _make_deps()
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv123", deps=deps)
    call_kwargs = factory.create_agent.call_args
    descriptor = call_kwargs.kwargs.get("descriptor") or call_kwargs.args[0]
    assert descriptor.comm_kind == AgentCommKind.SUBAGENT


@pytest.mark.asyncio
async def test_materialize_subagent_builds_own_tool_manager():
    """Subagents (parent_session set) DO get a materialize-built tool_manager
    + context_manager (session-scoped, preset tools)."""
    deps, factory = _make_deps()
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    kwargs = factory.create_agent.call_args.kwargs
    assert kwargs["tool_manager"] is not None
    assert kwargs["context_manager"] is not None


@pytest.mark.asyncio
async def test_materialize_parent_none_still_builds_subagent_tool_manager():
    """Design B: materialize is subagent-only. Even with parent_session=None
    (a subagent with no parent context, e.g. a cold-started template), it
    builds a tool_manager from the template rather than passing None through
    to the factory."""
    deps, factory = _make_deps()
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    await template.materialize(parent_session=None, invocation_id=None, deps=deps)
    kwargs = factory.create_agent.call_args.kwargs
    assert kwargs["tool_manager"] is not None  # subagent-style, not factory default


@pytest.mark.asyncio
async def test_materialize_subagent_inherits_reasoning_effort() -> None:
    """AgentLLMConfig on the subagent descriptor receives llm_reasoning_effort from deps."""
    deps, factory = _make_deps()
    deps = dataclasses.replace(deps, llm_reasoning_effort=ReasoningEffort.HIGH)
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.llm_config.reasoning_effort == ReasoningEffort.HIGH


@pytest.mark.asyncio
async def test_materialize_subagent_wires_hooks_to_hook_runner():
    """ADR-0015 D5: SubagentAutoSendHook must reach pipeline.hook_runner
    (factory's hooks= param only stores on pipeline.hooks, which isn't
    dispatched). materialize adds hooks to hook_runner post-creation."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.hook_runner = MagicMock()  # truthy → add() is callable
    fake_instance.stop = AsyncMock()
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    pool.get = MagicMock(return_value=None)
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        agent_bus=MagicMock(),  # wired → SubagentAutoSendHook added
        project_dir=None,
    )
    deps = dataclasses.replace(
        deps,
        context_fork_builder=ContextForkBuilder(),
        workspace_path_resolver=WorkspacePathResolver(workspace_manager=None, pool_name="main"),
    )
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    # SubagentAutoSendHook must be added to hook_runner (not just pipeline.hooks)
    assert fake_instance.pipeline.hook_runner.add.call_count >= 1
