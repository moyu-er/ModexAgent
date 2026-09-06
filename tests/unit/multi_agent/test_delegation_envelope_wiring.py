"""Delegation-boundary envelope wiring — the CALLER envelope, not workspace alone.

A declared subagent ``sandbox`` block (the unified two-class shape) is
validated against the caller's envelope — workspace plus the caller's
``exclusive.writable_roots``. A declared root under a configured caller
root must pass; a root under no envelope root fails fast (a delegation
can only narrow, never amplify).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.sandbox.settings import ExclusiveConfig, SandboxSettings
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath


class _StaticRootProvider(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _pool_assembly(tmp_path: Path, root_spec: AgentSpec) -> PoolAssemblyContext:
    from modex_agent.multi_agent.pool import SessionRetentionPolicy
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

    return PoolAssemblyContext(
        pool_name="main",
        pool_spec=PoolSpec(name="main", agents=[root_spec, AgentSpec(name="scout", parent="main")]),
        project_dir=tmp_path, data_dir=tmp_path,
        broker=MagicMock(), inbox_server=MagicMock(), agent_bus=MagicMock(),
        output_adapter=MagicMock(), safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(), registry=TurnSessionRegistry(),
    )


async def _loaded_registry() -> ComponentRegistry:
    """A registry with the DefaultPlugin loaded (async factory flush)."""
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import ComponentRegistryLoader, PluginDiscoveryConfig

    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(bundled_factories=(DefaultPlugin(),), project_plugin_paths=()),
    )
    return registry


async def _deps(tmp_path: Path, pool_assembly: PoolAssemblyContext, ws: Path) -> AgentMaterializeDeps:
    from modex_agent.plugins.defaults.capabilities.skills.supply import build_skills_supply
    from modex_agent.plugins.defaults.capabilities.subagents import SubagentsSupply

    registry = await _loaded_registry()
    fake_instance = MagicMock()
    from modex_agent.runtime.services import AgentRuntimeServices

    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline._turn_runner.turn_context_builder.runtime_services = (
        AgentRuntimeServices()
    )
    fake_instance.stop = AsyncMock()
    factory = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    pool = MagicMock()
    pool.register_resident = AsyncMock()
    pool.get = MagicMock(return_value=None)
    return AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=MagicMock(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        safety=RuntimeSafetyPolicy(),
        llm_model="gpt-4o",
        llm_provider=MagicMock(),
        project_dir=None,
        root_provider=_StaticRootProvider(ws),
        component_registry=registry,
        pool_assembly_ctx=pool_assembly,
        capability_supply={
            "subagents": SubagentsSupply(service=MagicMock()),
            "skills": build_skills_supply(
                pool_name="main", skill_root_for_agent={"scout": []}
            ),
        },
    )


def _compiled_template(name: str, **agent_kwargs: object) -> AgentTemplate:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)

    declared = AgentSpec(name=name, parent="main", **agent_kwargs)  # type: ignore[arg-type]
    spec = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="main", agents=[AgentSpec(name="main"), declared]),
    )
    workspace_ctx = WorkspaceContext(target=Path("/ws"), paths=WorkspacePaths(root=Path("/ws")), is_home=False)
    compilation = compile_scope(spec, workspace_ctx=workspace_ctx, registry=registry)
    del workspace_ctx
    compiled = next(a for a in compilation.agents if a.provenance.agent == name)
    return AgentTemplate(
        spec=declared,
        toolset_profile=compiled.defaults.toolset_profile,
        compiled_spec=compiled.spec,
    )


@pytest.mark.asyncio
async def test_allowed_dir_under_pool_writable_root_materializes(tmp_path: Path) -> None:
    """A declared allowed_dir under the pool's configured writable root is
    inside the POOL envelope — validation must pass and materialization
    must succeed (workspace-only validation wrongly rejects it)."""
    ws = tmp_path / "ws"
    vendor = tmp_path / "vendor"
    ws.mkdir()
    vendor.mkdir()
    root_spec = AgentSpec(
        name="main",
        interceptors=["sandbox_guard"],
        interceptor_configs={
            "sandbox_guard": {
                "sandbox": {
                    "backend": "host",
                    "exclusive": {"writable_roots": [str(vendor)]},
                }
            }
        },
    )
    pool_assembly = _pool_assembly(tmp_path, root_spec)
    deps = await _deps(tmp_path, pool_assembly, ws)
    deps.scope_path = ScopePath(workspace_root=ws, pool_name="main")
    template = _compiled_template(
        "scout",
        sandbox=SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[vendor / "libs"])
        ),
    )

    with patch(
        "modex_agent.plugins.defaults.hooks.resolve_modexctl_bin_dir",
        return_value=Path("/fake/bin"),
    ):
        instance = await template.materialize(
            parent_session=MagicMock(), invocation_id="inv1", deps=deps
        )
    assert instance is not None


@pytest.mark.asyncio
async def test_allowed_dir_outside_all_pool_roots_still_fails(tmp_path: Path) -> None:
    """Extension is not escape: a dir under NO pool envelope root still
    fails fast."""
    ws = tmp_path / "ws"
    vendor = tmp_path / "vendor"
    elsewhere = tmp_path / "elsewhere"
    for d in (ws, vendor, elsewhere):
        d.mkdir()
    root_spec = AgentSpec(
        name="main",
        interceptors=["sandbox_guard"],
        interceptor_configs={
            "sandbox_guard": {
                "sandbox": {
                    "backend": "host",
                    "exclusive": {"writable_roots": [str(vendor)]},
                }
            }
        },
    )
    pool_assembly = _pool_assembly(tmp_path, root_spec)
    deps = await _deps(tmp_path, pool_assembly, ws)
    deps.scope_path = ScopePath(workspace_root=ws, pool_name="main")
    template = _compiled_template(
        "scout",
        sandbox=SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[elsewhere])
        ),
    )

    with (
        patch(
            "modex_agent.plugins.defaults.hooks.resolve_modexctl_bin_dir",
            return_value=Path("/fake/bin"),
        ),
        pytest.raises(ValueError, match="can only narrow, never amplify"),
    ):
        await template.materialize(
            parent_session=MagicMock(), invocation_id="inv1", deps=deps
        )
