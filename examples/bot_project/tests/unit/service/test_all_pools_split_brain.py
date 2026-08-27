"""Ticket 11 — all pools on scope declarations: the final split-brain.

``coder`` and ``review`` are the last two shipped pools still booting the
legacy roster road at ``create_pool`` (``default``/``opencode`` switched in
tickets 07/10). This suite freezes their OLD road's products — with
``pool_data`` (memory/notification faces, ticket-09 driver shape) and the
full Phase-2 tail (legacy peer feeding + ``register_communication_tools`` +
``_wire_pool_to_resources``) — as golden fixtures BEFORE the switch; the
switch commit re-drives the SAME pools down the declaration road and
compares field by field.

Split-brain discipline (plan §Verification strategy, same as tickets
05/07/09/10/14): the BASELINE commit freezes the old products; bare golden
refreshes are forbidden; every intentional difference must be listed in the
explicit allowlist (each entry names its reason).

The old-road driver mirrors what ``resources.py`` performs for these pools
TODAY: deps from the compiled root (ticket 14's single assembly-deps road —
coder/review are declared pools even while their create_pool road is
legacy), legacy ``create_pool`` (``pool_spec``, no ``declared``), legacy
Phase-2 peer feeding from the frozen ``pool.yml``, and the legacy post-boot
experience wiring.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.pool.declaration import (
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER
from bot.workspace.pool_data import build_pool_data
from bot.workspace.wiring.stack import declared_assembly_deps

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.communication.peer_resolution import (
    peer_links_from_declaration,
    resolve_peer_targets,
)
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

from .assembly_manifest import (
    AgentManifest,
    assert_bash_wave_parity,
    dump_assembly_manifest,
    dump_memory_hooks,
    roster_source_map,
)

sys.path.insert(0, str(Path(__file__).parents[3]))

BOT_BASE = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "split_brain_11"

_MAX_CONTEXT_TOKENS = 200000

# The two pools still on the legacy create_pool road at the baseline freeze.
LEGACY_POOLS = ("coder", "review")


async def _load_registry() -> ComponentRegistry:
    """DefaultPlugin + bot project plugins — the production factory set."""
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(BOT_BASE / "plugins",),
        ),
    )
    return registry


def _workspace_ctx(root: Path) -> WorkspaceContext:
    return WorkspaceContext(target=root, paths=WorkspacePaths(root=root), is_home=False)


def _boot_declaration(data_dir: Path):
    """The real production boot: load + validate (V1-V11) + compile bot.yml."""
    return boot_scope_declaration(
        declaration_path=BOT_BASE / "config" / "scopes" / "bot.yml",
        project_dir=BOT_BASE,
        data_dir=data_dir,
        graphs_dirs=(BOT_BASE / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )



async def _create_declared_pool(pool_name: str, tmp_path: Path):
    """create_pool on the declaration road + the Phase-2 peer resolution.

    Mirrors the old-road driver's supply shape (pool_data) MINUS the
    retired glue: no ``register_communication_tools`` (derived entries),
    no ``_wire_pool_to_resources`` (the roster references
    ``experience_review`` — Stage 4 dispatches it against the
    chain-supplied infra).
    """
    declared = declared_pool_build(_boot_declaration(tmp_path / ".modex"), pool_name)
    registry = await _load_registry()
    deps = declared_assembly_deps(declared.root, max_context_tokens=_MAX_CONTEXT_TOKENS)
    pool_data = await build_pool_data(
        _workspace_ctx(tmp_path / ".modex"),
        pool_name,
        declared.pool.root_agent,
        MagicMock(spec=LLMProvider),
        deps,
        "",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()
    instance = None
    try:
        with (
            patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}),
            patch(
                "modex_agent.tools.mcp_loader.load_per_agent_mcp",
                new=AsyncMock(return_value=None),
            ),
        ):
            instance = await create_pool(
                pool_name=pool_name,
                declared=declared,
                assembly_deps=deps,
                project_dir=BOT_BASE,
                workspace_registry=object(),
                workspace_resources=object(),
                data_dir=tmp_path / ".modex",
                broker=broker,
                output_adapter=MagicMock(spec=OutputAdapter),
                safety=RuntimeSafetyPolicy(),
                retention=SessionRetentionPolicy(),
                im_ui=MagicMock(),
                shared_hooks=[],
                shared_hook_runner=HookRunner(),
                shared_interceptor_chain=InterceptorChain(),
                workspace_resolver=None,
                bot_model_config=None,
                model_choice_registry=ModelChoiceRegistry(),
                component_registry=registry,
                pool_data=pool_data,
            )
        _phase2_declared_peer_resolution(instance, _boot_declaration(tmp_path / ".modex"))
    except BaseException:
        if instance is not None:
            await instance.pool.shutdown_all()
        await broker.stop()
        await pool_data.context_manager.memory_system.close()
        raise
    return instance, registry, broker, pool_data


def _stub_peer_instance(root_agent_name: str) -> Any:
    """A minimal bundle entry for a peer pool this driver does not boot."""
    stub = MagicMock()
    stub.root_agent_name = root_agent_name
    stub.tree_manager = MagicMock()
    return stub


def _phase2_declared_peer_resolution(instance: Any, boot: Any) -> None:
    """The production Phase-2 tail for a DECLARED pool (ticket 13): links
    extracted from the declaration over the scope path, resolved against
    the workspace bundle (stub entries for every pool this driver does not
    boot — root names from the declaration, matching the booted roots the
    real bundle carries)."""
    links = peer_links_from_declaration(boot.spec)
    if instance.name not in links:
        return
    pools = {instance.name: instance}
    assert boot.spec.workspace is not None
    spec_pools = {pool.name: pool for pool in boot.spec.workspace.pools}
    for pool_name, pool in spec_pools.items():
        if pool_name == instance.name:
            continue
        peer_root = next(agent for agent in pool.agents if agent.parent is None)
        pools[pool_name] = _stub_peer_instance(peer_root.name)
    resolve_peer_targets(pools, links)


async def _new_road_manifest(pool_name: str, tmp_path: Path):
    """NEW road manifest: declaration boot + pool_data + declared Phase-2."""
    instance, registry, broker, pool_data = await _create_declared_pool(pool_name, tmp_path)
    try:
        declared = declared_pool_build(_boot_declaration(tmp_path / ".modex"), pool_name)
        lazy_agents = [
            AgentManifest(
                agent_name=sub.provenance.agent,
                materialized=False,
                effective_spec_tools=list(sub.spec.tools),
            )
            for sub in declared.subagents
        ]
        return dump_assembly_manifest(
            instance,
            data_dir=tmp_path / ".modex",
            source_of=roster_source_map(registry, list(declared.root.spec.tools)),
            lazy_agents=lazy_agents,
            memory_hooks=dump_memory_hooks(pool_data),
        )
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()
        await pool_data.context_manager.memory_system.close()


_COMM_SOURCE_OLD = "glue"
_COMM_SOURCE_NEW = "roster:bundled"

# Task 7 (2a/2): the glue tools are roster-declared on the shipped roots,
# so the manifest provenance flips from hardcoded glue ("glue") to
# roster-derived — send_file_to_user resolves through the bot project
# plugin, experience through the FW bundled factory. Dual registration
# keeps every other observable (class, params, position) byte-identical;
# only the provenance label follows the declaration.
_GLUE_ROSTER_SOURCES = {
    "send_file_to_user": "roster:project",
    "experience": "roster:bundled",
}


@pytest.mark.parametrize("pool_name", LEGACY_POOLS)
async def test_split_brain_manifest_field_by_field(pool_name: str, tmp_path: Path) -> None:
    """The SAME configuration, legacy boot road vs declaration road,
    produce identical manifests field by field — modulo the allowlisted
    structural differences below (same classes as the 07/09 tickets,
    stale-guarded on both roads)."""
    new = await _new_road_manifest(pool_name, tmp_path)
    golden = json.loads(
        (FIXTURES / f"old_road_{pool_name}_manifest.json").read_text(encoding="utf-8")
    )

    for field in (
        "pool_name",
        "execution_strategy",
        "terminal_manager",
        "trio_registry_shared",
        "todo_store",
        "interceptors",
        "commands",
        # Peer target set: same names + kinds (review's `default` peer).
        "comm_targets",
        # The notification face: same hook pair, same order.
        "memory_hooks",
    ):
        assert new.model_dump(mode="json")[field] == golden[field], f"{pool_name}.{field} diverged"

    new_main = next(a for a in new.agents if a.materialized)
    golden_main = next(a for a in golden["agents"] if a["materialized"])
    for field in (
        "agent_name",
        "memory_config",
        "system_prompt_provider",
        "system_prompt_sha256",
        "llm_provider_class",
    ):
        assert getattr(new_main, field) == golden_main[field], (
            f"{pool_name} main agent {field} diverged"
        )

    # React hooks: same SET (both roads register ExperienceReviewHook —
    # old road via post-boot wiring, new road via Stage-4 roster dispatch
    # from the bot.yml hooks reference); only the POSITION differs. The
    # LengthGuardHook wave (register_tree_aware_hooks — the shared seam on
    # BOTH roads) postdates the frozen goldens: tolerated as a live extra,
    # presence-derived. The nudge wave (bot.yml roster references for
    # task_delegation_nudge / todo_planning_nudge — declaration-road-only
    # by design, self-gating at runtime) likewise postdates the goldens.
    new_hook_names = [h.name for h in new_main.hooks]
    golden_hook_names = [h["name"] for h in golden_main["hooks"]]
    assert set(new_hook_names) - set(golden_hook_names) == {
        "length_guard",
        "task_delegation_nudge",
        "todo_planning_nudge",
    }
    assert set(golden_hook_names) <= set(new_hook_names)
    assert new_hook_names.index("experience_review_hook") < new_hook_names.index(
        "turn_outcome_notify"
    )
    assert golden_hook_names.index("experience_review_hook") > golden_hook_names.index(
        "turn_outcome_notify"
    )

    # Toolsets: same names, classes, params — modulo the comm-tool source
    # relabeling (glue → roster-derived registration) and the
    # persistent-bash wave's presence-derived divergence.
    new_tools = {t.name: t for t in new_main.tools}
    golden_tools = {t["name"]: t for t in golden_main["tools"]}
    assert_bash_wave_parity(new_tools, golden_tools)
    for name in sorted(golden_tools):
        if name == "bash" and "bash_input" in new_tools:
            continue  # wave-replaced slot — asserted in assert_bash_wave_parity
        assert new_tools[name].tool_class == golden_tools[name]["tool_class"]
        assert new_tools[name].params == golden_tools[name]["params"]
        if name in ("task", "send_to_peer"):
            assert golden_tools[name]["source"] == _COMM_SOURCE_OLD
            assert new_tools[name].source == _COMM_SOURCE_NEW
        elif name in _GLUE_ROSTER_SOURCES:
            assert golden_tools[name]["source"] == "glue"
            assert new_tools[name].source == _GLUE_ROSTER_SOURCES[name]
        else:
            assert new_tools[name].source == golden_tools[name]["source"], (
                f"{pool_name}.{name}.source diverged"
            )

    # Lazy subagents: same set; the compiled effective specs gain the
    # derived send_to_agent entry (SPEC §5.2 — injected before supplements,
    # so the comparison is set-based with the entry appended).
    new_lazy = {a.agent_name: a for a in new.agents if not a.materialized}
    golden_lazy = {a["agent_name"]: a for a in golden["agents"] if not a["materialized"]}
    assert set(new_lazy) == set(golden_lazy)
    for name in sorted(golden_lazy):
        assert set(new_lazy[name].effective_spec_tools) == (
            set(golden_lazy[name]["effective_spec_tools"]) | {"send_to_agent"}
        ), f"{pool_name} lazy {name} effective tools diverged"


@pytest.mark.parametrize(
    ("pool_name", "expect_send_to_peer"),
    [("coder", False), ("review", True)],
)
async def test_declared_pool_comm_tools_derived(
    pool_name: str, expect_send_to_peer: bool, tmp_path: Path
) -> None:
    """The switched pools boot the derived communication registration
    (comm_tools_derived) — the legacy conditional registration no longer
    covers them. review carries a peer link (default), so its root also
    gets the derived send_to_peer."""
    instance, _registry, broker, pool_data = await _create_declared_pool(pool_name, tmp_path)
    try:
        assert instance.comm_tools_derived is True
        assert instance.tool_manager.get_tool("task") is not None
        assert (instance.tool_manager.get_tool("send_to_peer") is not None) is (expect_send_to_peer)
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()
        await pool_data.context_manager.memory_system.close()
