"""Tests for AgentTemplate.materialize — the single construction path."""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

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

from pathlib import Path


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
async def test_materialize_subagent_inherits_llm_model_info() -> None:
    """AgentLLMConfig on the subagent descriptor receives llm_model_info from deps.

    Without this threading, the factory gets a descriptor with model_info=None,
    so runtime_services.model_info is None and tools (e.g. ReadFileTool image
    path) degrade to text-only even when the LLM supports IMAGE.
    """
    from modex_agent.core.capabilities import ModelCapabilities, ModelInfo, Modality

    vision_info = ModelInfo(
        model_name="test-vision",
        capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
    )
    deps, factory = _make_deps()
    deps = dataclasses.replace(deps, llm_model_info=vision_info)
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    parent = SessionIdFactory().create(agent_name="main")
    with patch(
        "modex_agent.agents.external.cli_resolver.resolve_modexctl_bin_dir",
        return_value=Path("/fake/bin"),
    ):
        await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.llm_config.model_info is not None
    assert descriptor.llm_config.model_info is vision_info
    assert descriptor.llm_config.model_info.capabilities.supports(Modality.IMAGE)


@pytest.mark.asyncio
async def test_materialize_subagent_passes_roles_to_descriptor() -> None:
    """T1 data-layer透传: SubagentSpec.roles lands on AgentDescriptor.roles.

    The materialize call constructs the descriptor from the spec; the
    ``roles`` field must round-trip verbatim. Preset values (AgentRole
    members) collapse to their plain string value via StrEnum; custom
    strings are preserved as-is.
    """
    deps, factory = _make_deps()
    template = AgentTemplate(
        spec=SubagentSpec(agent_name="scout", roles=["planner", "custom-role"])
    )
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.roles == ["planner", "custom-role"]


@pytest.mark.asyncio
async def test_materialize_subagent_roles_default_empty() -> None:
    """When SubagentSpec omits roles, descriptor.roles defaults to []."""
    deps, factory = _make_deps()
    template = AgentTemplate(spec=SubagentSpec(agent_name="scout"))
    parent = SessionIdFactory().create(agent_name="main")
    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)
    call_kwargs = factory.create_agent.call_args.kwargs
    descriptor = call_kwargs.get("descriptor") or factory.create_agent.call_args.args[0]
    assert descriptor.roles == []


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


# ---------------------------------------------------------------------------
# EXTERNAL subagent dispatch — emitter_factory post-build wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_external_injects_emitter_factory_into_turn_runner():
    """Regression: external subagents bypass pool_builder's
    ``_create_with_emitter`` wrapper (which injects the WebUI emitter for
    react subagents + external main agent). The framework must inject the
    emitter in ``_materialize_external`` so the external subagent's turns
    write transcript events. Without it, ``ExternalTurnRunner`` keeps the
    default ``StreamingAwareEmitter``+``BrokerOutputAdapter`` from
    ``assemble_pipeline`` and turns are invisible in the WebUI history.
    """
    from modex_agent.agents.external.subagent_builder import (
        SubagentExternalBuilder,
    )
    from modex_agent.core.constants import ExecutionStrategyKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import (
        AgentDescriptor,
        AgentInstance,
    )
    from modex_agent.multi_agent.pool_config.specs import ProviderKind

    sentinel_emitter_factory = MagicMock(name="webui_emitter_factory")
    fake_turn_runner = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline._turn_runner = fake_turn_runner
    fake_instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=fake_pipeline,
    )

    class _StubBuilder(SubagentExternalBuilder):
        async def build(self, spec, descriptor, parent_session, invocation_id, deps):
            return fake_instance

    pool = MagicMock()
    pool.register_resident = AsyncMock()
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        subagent_external_builder=_StubBuilder(),
        emitter_factory=sentinel_emitter_factory,
    )
    spec = SubagentSpec(
        agent_name="coder",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )
    template = AgentTemplate(spec=spec)
    parent = SessionInfo.from_str("inv1.main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    fake_turn_runner.set_emitter_factory.assert_called_once_with(sentinel_emitter_factory)


@pytest.mark.asyncio
async def test_materialize_external_skips_emitter_injection_when_deps_emitter_none():
    """No emitter_factory in deps → no set_emitter_factory call; the
    external subagent keeps the default factory from assemble_pipeline."""
    from modex_agent.agents.external.subagent_builder import (
        SubagentExternalBuilder,
    )
    from modex_agent.core.constants import ExecutionStrategyKind
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import (
        AgentDescriptor,
        AgentInstance,
    )
    from modex_agent.multi_agent.pool_config.specs import ProviderKind

    fake_turn_runner = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline._turn_runner = fake_turn_runner
    fake_instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=MagicMock(),
        pipeline=fake_pipeline,
    )

    class _StubBuilder(SubagentExternalBuilder):
        async def build(self, spec, descriptor, parent_session, invocation_id, deps):
            return fake_instance

    pool = MagicMock()
    pool.register_resident = AsyncMock()
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        subagent_external_builder=_StubBuilder(),
        emitter_factory=None,
    )
    spec = SubagentSpec(
        agent_name="coder",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )
    template = AgentTemplate(spec=spec)
    parent = SessionInfo.from_str("inv1.main")

    await template.materialize(parent_session=parent, invocation_id="inv1", deps=deps)

    fake_turn_runner.set_emitter_factory.assert_not_called()
