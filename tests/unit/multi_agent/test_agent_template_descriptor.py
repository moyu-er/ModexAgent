"""Description propagation + external dispatch through the strategy registry."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    ExecutionStrategyRegistry,
    SubagentAssembly,
)
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.assembly.context import AgentContext
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath


async def _deps(
    *,
    pool: MagicMock,
    strategy_registry: ExecutionStrategyRegistry | None = None,
) -> AgentMaterializeDeps:
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
    from modex_agent.plugins.registry import ComponentRegistry

    component_registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        component_registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(),
        ),
    )
    fake_instance = MagicMock()
    fake_instance.pipeline = None
    fake_instance.stop = AsyncMock()
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    return AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        safety=RuntimeSafetyPolicy(),
        llm_model="gpt-4o",
        llm_provider=MagicMock(),
        context_fork_builder=ContextForkBuilder(),
        scope_path=ScopePath(workspace_root=Path("/ws"), pool_name="main"),
        strategy_registry=strategy_registry,
        component_registry=component_registry,
    )


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_agent_template_descriptor")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compiled_template(name: str, **agent_kwargs: object) -> AgentTemplate:
    """Compile a two-agent tree (root + named sub) and seed the sub's
    template exactly as the declaration road does (declared spec +
    position-derived profile + compiled assembly spec)."""
    declared = AgentSpec(name=name, parent="root", **agent_kwargs)
    spec = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="main", agents=[AgentSpec(name="root"), declared]),
    )
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx())
    compiled = next(a for a in compilation.agents if a.provenance.agent == name)
    return AgentTemplate(
        spec=declared,
        toolset_profile=compiled.defaults.toolset_profile,
        compiled_spec=compiled.spec,
    )


@pytest.mark.asyncio
async def test_materialize_passes_description_to_react_descriptor() -> None:
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    template = _compiled_template("explore", description="Read-only scout")

    await template.materialize(
        parent_session=SessionInfo.from_str("inv1.main"),
        invocation_id="inv1",
        deps=await _deps(pool=pool),
    )

    descriptor = pool.register_resident.await_args.args[0]
    assert descriptor.role_description == "Read-only scout"


class _CapturingExternalStrategy(ExecutionStrategy):
    """Captures the per-invocation AgentContext + deps; fixed assembly."""

    def __init__(self) -> None:
        self.invocations: list[AgentContext] = []

    @property
    def name(self) -> str:
        return "external"

    async def assemble_main(self, ctx):  # type: ignore[no-untyped-def]
        raise AssertionError("assemble_main must not be called for a subagent")

    def validate_pool_spec(self, spec: PoolSpec) -> None:
        return None

    async def assemble_sub(
        self,
        ctx: AgentContext,
        deps: AgentMaterializeDeps,
    ) -> SubagentAssembly:
        self.invocations.append(ctx)
        descriptor = AgentDescriptor.model_construct()  # opaque carrier
        instance = AgentInstance.__new__(AgentInstance)  # opaque carrier
        return SubagentAssembly(descriptor=descriptor, instance=instance)


@pytest.mark.asyncio
async def test_materialize_external_dispatches_to_strategy_assemble_sub() -> None:
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    strategy = _CapturingExternalStrategy()
    registry = ExecutionStrategyRegistry()
    registry.register(strategy)
    template = _compiled_template(
        "explore",
        description="Read-only scout",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )

    await template.materialize(
        parent_session=SessionInfo.from_str("inv1.main"),
        invocation_id="inv1",
        deps=await _deps(pool=pool, strategy_registry=registry),
    )

    assert len(strategy.invocations) == 1
    inv_ctx = strategy.invocations[0]
    # Per-invocation data rides the AgentContext chain (ticket 10).
    assert inv_ctx.agent_name == "explore"
    assert inv_ctx.invocation_id == "inv1"
    assert str(inv_ctx.parent_session) == "inv1.main"
    # The per-agent spec reference rides the chain's agent layer.
    assert inv_ctx.spec is not None
    assert inv_ctx.spec.agent_type is AgentType.external_sub
    assert inv_ctx.spec.provider_kind == "opencode"
    assert inv_ctx.spec.description == "Read-only scout"
    registered_args = pool.register_resident.await_args.args
    assert len(registered_args) == 2
    # registration carries the pair returned by assemble_sub
    assert inv_ctx.agent_name == "explore"


@pytest.mark.asyncio
async def test_materialize_external_without_registry_raises() -> None:
    pool = MagicMock()
    template = _compiled_template(
        "explore",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )

    with pytest.raises(ValueError, match="strategy_registry"):
        await template.materialize(
            parent_session=SessionInfo.from_str("inv1.main"),
            invocation_id="inv1",
        deps=await _deps(pool=pool),
        )
