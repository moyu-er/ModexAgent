"""D-5 per-agent LLM pins through ``AgentTemplate.materialize``.

The deps-carried ``agent_llm_pins`` map (assembly-resolved values, keyed by
agent name) takes precedence over BOTH the ``llm_provider`` slot name and
the pool-default provider reuse — and replaces the descriptor model profile
wholesale. No pin → the pre-change path byte-for-byte: the pool-default
provider instance is reused and ``LlmDefaults`` come from the deps fields.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.llm_request import ReasoningEffort
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.materialize_deps import AgentLLMPin, AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.plugins.assembly.native_core import LlmDefaults
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath


async def _deps(*, pool: MagicMock, pins: dict[str, AgentLLMPin]) -> AgentMaterializeDeps:
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig
    from modex_agent.plugins.registry import ComponentRegistry

    component_registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        component_registry,
        PluginDiscoveryConfig(bundled_factories=(DefaultPlugin(),), project_plugin_paths=()),
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
        llm_model="pool-default-model",
        llm_temperature=0.7,
        llm_max_output_tokens=32_000,
        llm_reasoning_effort=ReasoningEffort.NONE,
        llm_model_info=ModelInfo(model_name="pool-default-model"),
        llm_provider=MagicMock(),
        context_fork_builder=ContextForkBuilder(),
        scope_path=ScopePath(workspace_root=Path("/ws"), pool_name="main"),
        component_registry=component_registry,
        agent_llm_pins=pins,
    )


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_agent_template_llm_pin")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _compiled_template(name: str) -> AgentTemplate:
    """Compile a two-agent tree (root + named sub) and seed the sub's
    template exactly as the declaration road does."""
    declared = AgentSpec(name=name, parent="root")
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
async def test_pin_replaces_provider_and_descriptor_profile() -> None:
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    template = _compiled_template("explore")
    pinned_provider = MagicMock()
    pin = AgentLLMPin(
        provider=pinned_provider,
        defaults=LlmDefaults(
            model="pinned-model",
            temperature=0.2,
            max_output_tokens=8_000,
            reasoning_effort=ReasoningEffort.HIGH,
            model_info=ModelInfo(
                model_name="pinned-model", context_limit=65_536, max_output_tokens=8_000
            ),
        ),
    )
    deps = await _deps(pool=pool, pins={"explore": pin})

    await template.materialize(
        parent_session=SessionInfo.from_str("inv1.main"),
        invocation_id="inv1",
        deps=deps,
    )

    create = deps.agent_factory.create_agent.await_args
    assert create is not None
    # The pinned provider instance is the one the factory receives.
    assert create.kwargs["llm_provider"] is pinned_provider
    descriptor = create.args[0]
    # The descriptor model profile is the PINNED model's, not the pool's.
    assert descriptor.llm_config.model == "pinned-model"
    assert descriptor.llm_config.temperature == 0.2
    assert descriptor.llm_config.max_output_tokens == 8_000
    assert descriptor.llm_config.reasoning_effort == ReasoningEffort.HIGH
    assert descriptor.llm_config.model_info == ModelInfo(
        model_name="pinned-model", context_limit=65_536, max_output_tokens=8_000
    )


@pytest.mark.asyncio
async def test_no_pin_keeps_pool_default_path_unchanged() -> None:
    """Default = inherit the caller: the pool-default provider INSTANCE is
    reused (identity, not a copy) and LlmDefaults come from the deps fields
    — the pre-change behavior byte-for-byte."""
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    template = _compiled_template("explore")
    deps = await _deps(pool=pool, pins={})

    await template.materialize(
        parent_session=SessionInfo.from_str("inv1.main"),
        invocation_id="inv1",
        deps=deps,
    )

    create = deps.agent_factory.create_agent.await_args
    assert create is not None
    assert create.kwargs["llm_provider"] is deps.llm_provider
    descriptor = create.args[0]
    assert descriptor.llm_config.model == "pool-default-model"
    assert descriptor.llm_config.temperature == 0.7
    assert descriptor.llm_config.max_output_tokens == 32_000
    assert descriptor.llm_config.model_info == ModelInfo(model_name="pool-default-model")


@pytest.mark.asyncio
async def test_pin_for_another_agent_is_ignored() -> None:
    """The map is keyed per agent — a sibling's pin never leaks."""
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    template = _compiled_template("explore")
    sibling_pin = AgentLLMPin(
        provider=MagicMock(),
        defaults=LlmDefaults(model="sibling-model"),
    )
    deps = await _deps(pool=pool, pins={"other": sibling_pin})

    await template.materialize(
        parent_session=SessionInfo.from_str("inv1.main"),
        invocation_id="inv1",
        deps=deps,
    )

    create = deps.agent_factory.create_agent.await_args
    assert create is not None
    assert create.kwargs["llm_provider"] is deps.llm_provider
    assert create.args[0].llm_config.model == "pool-default-model"
