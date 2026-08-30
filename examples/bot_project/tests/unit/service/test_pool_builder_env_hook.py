"""Env-hook spec derivation from the pool assembly context — complete pool_map/targets.

Pins that ``NativeEnvInjectionHookFactory`` (the W6 position-default roster
row ``native_env``, dispatched by Stage 4) derives an ``env_spec_template``
whose ``agent_pool_map`` carries main + every subagent + every peer pool's
main agent and whose ``targets`` carries every subagent + every peer main,
so native main agents can use ``modexctl send --to <any agent>`` and
``modexctl agents`` as a send_to_agent alternative.

The retired inline construction in ``_wire_main_pipeline`` died with the W6
glue eradication (ADR-0047): the hook is a compiler position-default roster
entry and the factory derives the template from the pool assembly context
on the assembly chain (ADR-0022 D6 unchanged — the pool_map/targets must be
complete at wiring time).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Bot tests resolve ``bot.*`` via the repo root inserted into sys.path.
sys.path.insert(0, str(Path(__file__).parents[3]))

from modex_agent.hook.builtin import NativeEnvInjectionHook
from modex_agent.multi_agent.communication.peer_resolution import PeerLink
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.assembly.context import (
    PoolRuntimeDeps,
    agent_context_chain,
    resolution_context,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.defaults.hooks import NativeEnvInjectionHookFactory
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

pytestmark = pytest.mark.skipif(
    shutil.which("modexctl") is None,
    reason="modexctl CLI not available",
)


def _peer_link(peer_name: str, peer_main: str) -> PeerLink:
    return PeerLink(
        peer_pool=peer_name,
        peer_agent=peer_main,
        peer_description=f"{peer_main} peer",
    )


def _main_spec(workspace_ctx: WorkspaceContext) -> AssemblySpec:
    return AssemblySpec(
        agent_type=AgentType.native_main,
        agent_name="main",
        pool_name="main",
        tools=[],
        hooks=["native_env"],
        llm_provider="default",
        system_prompt_provider="file_prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="react",
        workspace_ctx=workspace_ctx,
    )


async def _env_hook(
    pool_spec: PoolSpec, peer_links: tuple[PeerLink, ...]
) -> NativeEnvInjectionHook:
    pool_assembly_ctx = PoolAssemblyContext(
        pool_name=pool_spec.name,
        pool_spec=pool_spec,
        project_dir=Path("/tmp/bot"),
        data_dir=Path("/tmp/bot/.modex"),
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=MagicMock(),
        retention=MagicMock(),
        registry=MagicMock(),
        peer_links=peer_links,
    )
    workspace_ctx = WorkspaceContext(
        target=Path("/tmp/bot"),
        paths=WorkspacePaths(root=Path("/tmp/bot/.modex")),
        is_home=False,
    )
    spec = _main_spec(workspace_ctx)
    component_ctx = resolution_context(
        MagicMock(),
        workspace_ctx,
        PoolRuntimeDeps(pool_assembly_ctx=pool_assembly_ctx),
    )
    chain = agent_context_chain(component_ctx, spec=spec)
    return await NativeEnvInjectionHookFactory().create(
        NativeEnvInjectionHookFactory.config_model(), chain
    )


async def test_env_spec_builds_complete_pool_map_and_targets() -> None:
    """The factory's env_spec_template carries main + every subagent + every
    peer main in pool_map, and every subagent + peer main in targets."""
    main_name = "main"
    sub_name = "explore"
    peer_name = "peer_pool"
    peer_main = "peer_main"

    pool_spec = PoolSpec(
        name="default",
        agents=[
            AgentSpec(name=main_name),
            AgentSpec(name=sub_name, parent=main_name, description="explore subagent"),
        ],
        peers=[peer_name],
    )

    hook = await _env_hook(pool_spec, (_peer_link(peer_name, peer_main),))
    spec = hook._template  # noqa: SLF001 — read the frozen template for verification
    assert spec.agent_pool_map == {
        main_name: "default",
        sub_name: "default",
        peer_main: peer_name,
    }
    # Subagent description is the declaration's; peer description rides the
    # declared link face.
    assert spec.targets == [
        (sub_name, "explore subagent"),
        (peer_main, f"{peer_main} peer"),
    ]


async def test_env_spec_without_peer_links_omits_peers() -> None:
    """No declared peer links → the pool map and targets carry only the
    pool's own agents (the legacy missing-peer-on-disk fail-soft road is
    gone: links arrive validated from the declaration, so the no-links
    case is the only empty-peer shape)."""
    main_name = "main"
    sub_name = "explore"

    pool_spec = PoolSpec(
        name="default",
        agents=[
            AgentSpec(name=main_name),
            AgentSpec(name=sub_name, parent=main_name, description="explore subagent"),
        ],
    )

    hook = await _env_hook(pool_spec, ())
    spec = hook._template  # noqa: SLF001
    assert spec.agent_pool_map == {main_name: "default", sub_name: "default"}
    assert spec.targets == [(sub_name, "explore subagent")]
