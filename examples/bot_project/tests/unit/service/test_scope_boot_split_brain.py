"""Ticket 07 — scope-declaration boot split-brain (the pivot pool).

Old road vs new road, same default configuration (SPEC §3.2 / §5.2 / the
07 ticket): the ``default`` pool is the pivot — the one shipped pool that
exercises the full derivation table (subagent + peers + approval + aci
supplement + mcp). After the migration commit it boots from
``config/scopes/bot.yml`` via load → validate (V1-V11) → compile (ticket
06) → the existing ADR-0041 assembly pipeline; the other three pools keep
the legacy roster boot (dual-boot tolerated until ticket 11).

Split-brain discipline (plan §Verification strategy, same as ticket 05):

- The BASELINE commit freezes the OLD road's products — including the
  communication registration the ticket-05 baseline did not cover
  (Phase-2 peer injection + ``register_communication_tools``) — as a
  golden JSON fixture. The migration commit re-drives the SAME
  configuration down the NEW road and compares field by field.
- Any intentional difference must be listed in ``ALLOWED_DIFFERENCES``
  (each entry names its reason); any unlisted difference turns red.
  Refreshing the golden is forbidden.

The NEW road driver boots through the real boot sequence
(``boot_scope_declaration`` → ``declared_pool_build`` → ``create_pool``
with the declaration products) and performs the same Phase-2 peer
injection — minus ``register_communication_tools``, which the derived
entries supersede at Stage 4.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.pool.declaration import (
    ScopeBoot,
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
from modex_agent.multi_agent.tools import (
    SendToAgentTool,
    SendToPeerTool,
    TaskDispatchTool,
)
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.tools.terminal.persistent_bash import persistent_bash_supported
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

from .assembly_manifest import (
    AgentManifest,
    AssemblyManifest,
    assert_bash_wave_parity,
    dump_assembly_manifest,
    roster_source_map,
)

sys.path.insert(0, str(Path(__file__).parents[3]))

BOT_BASE = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "split_brain_07"


# ── Shared scaffolding ──────────────────────────────────────────────────


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


def _workspace_ctx() -> WorkspaceContext:
    return WorkspaceContext(
        target=BOT_BASE,
        paths=WorkspacePaths(root=BOT_BASE / ".modex"),
        is_home=False,
    )




# ── NEW road drivers (scope declaration) ────────────────────────────────


def _boot_declaration(data_dir: Path):
    """The real production boot: load + validate (V1-V11, V10 graph
    cross-check against the shipped graphs) + compile bot.yml."""
    return boot_scope_declaration(
        declaration_path=BOT_BASE / "config" / "scopes" / "bot.yml",
        project_dir=BOT_BASE,
        data_dir=data_dir,
        graphs_dirs=(BOT_BASE / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )


def _stub_peer_instance(root_agent_name: str) -> Any:
    """A minimal bundle entry for a peer pool this driver does not boot:
    the booted root's name + a mock tree (the manifest observes target
    name/kind only)."""
    stub = MagicMock()
    stub.root_agent_name = root_agent_name
    stub.tree_manager = MagicMock()
    return stub


def _phase2_declared_peer_resolution(instance: Any, boot: ScopeBoot) -> None:
    """The production Phase-2 tail for a DECLARED pool (ticket 13): links
    extracted from the declaration over the scope path, resolved against
    the workspace bundle (stub entries for the peers this driver does not
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


async def _create_declared_pool(
    tmp_path: Path,
    *,
    declared,
    registry: ComponentRegistry | None = None,
    boot: ScopeBoot | None = None,
):
    """create_pool on the declaration road + the Phase-2 peer resolution.

    ``register_communication_tools`` is deliberately NOT called: the
    derived entries registered the communication tools at Stage 4
    (the resources.py Phase-2 gate skips it for derived pools).

    Ticket 09: pool_data + the position-derived deps ride along so the
    roster-referenced HOOK-slot components (user_notice_cleanup,
    experience_review) resolve their chain-supplied infra at Stage 4 —
    the same supply shape resources.py produces in production.

    Ticket 13: the peer targets resolve through the FW service against
    the declaration's links (the boot products; pass ``boot`` — the
    caller of ``declared_pool_build`` already holds it).
    """
    registry = registry or await _load_registry()
    deps = declared_assembly_deps(declared.root, max_context_tokens=200000)
    pool_data = await build_pool_data(
        WorkspaceContext(
            target=tmp_path,
            paths=WorkspacePaths(root=tmp_path / ".modex"),
            is_home=False,
        ),
        "default",
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
                pool_name="default",
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
        if boot is not None:
            _phase2_declared_peer_resolution(instance, boot)
    except BaseException:
        if instance is not None:
            await instance.pool.shutdown_all()
        await broker.stop()
        raise
    return instance, registry, broker


async def _new_road_manifest(tmp_path: Path) -> AssemblyManifest:
    """NEW road: scope-declaration boot + Phase-2 peer injection."""
    boot = _boot_declaration(tmp_path / ".modex")
    declared = declared_pool_build(boot, "default")
    instance, registry, broker = await _create_declared_pool(
        tmp_path, declared=declared, boot=boot
    )
    try:
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
        )
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()


# ── Allowlist (intentional differences; every entry needs a reason) ─────

_OLD_ROAD_TOOL_ORDER = [
    "send_file_to_user",
    "experience",
    "read",
    "write",
    "ls",
    "grep",
    "glob",
    "bash",
    "edit",
    "task",
    "send_to_peer",
]
# Task 8 (2b): the builders.py hardcode is gone — the new road's manager
# registers the roster in compiled order (the two glue names now sit at
# their roster positions; `aci_edit` registers under its LLM-facing name
# `edit`), instead of the hardcoded pair leading the dict.
_NEW_ROAD_TOOL_ORDER = [
    "read",
    "write",
    "ls",
    "grep",
    "glob",
    "bash",
    "task",
    "send_to_peer",
    "experience",
    "send_file_to_user",
    "edit",
]

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

_LAZY_TOOLS_OLD = [
    "read",
    "write",
    "ls",
    "grep",
    "glob",
    "bash",
    "todo_write",
    "todo_read",
    "aci_edit",
]
_LAZY_TOOLS_NEW = [
    "read",
    "write",
    "ls",
    "grep",
    "glob",
    "bash",
    "send_to_agent",
    "todo_write",
    "todo_read",
    "aci_edit",
]


# ── Tests ──────────────────────────────────────────────────────────────



async def test_split_brain_manifest_field_by_field(tmp_path: Path) -> None:
    """AC (c): the SAME default configuration, old boot road vs scope
    declaration road, produce identical manifests field by field —
    modulo the allowlisted differences below (stale-guarded: each pinned
    value must match reality on BOTH roads or the test turns red)."""
    new = await _new_road_manifest(tmp_path)
    golden = AssemblyManifest.model_validate_json(
        (FIXTURES / "old_road_default_manifest.json").read_text(encoding="utf-8")
    )

    # Exact-equality fields: identity, strategy, infra, pipeline wiring,
    # and the communication TARGET SET (names + kinds, incl. Phase-2 peers).
    for field in (
        "pool_name",
        "execution_strategy",
        "terminal_manager",
        "trio_registry_shared",
        "todo_store",
        "interceptors",
        "commands",
        "comm_targets",
    ):
        assert getattr(new, field) == getattr(golden, field), f"{field} diverged"

    new_main = next(a for a in new.agents if a.materialized)
    golden_main = next(a for a in golden.agents if a.materialized)
    for field in (
        "agent_name",
        "memory_config",
        "system_prompt_provider",
        "system_prompt_sha256",
        "llm_provider_class",
    ):
        assert getattr(new_main, field) == getattr(golden_main, field), (
            f"main agent {field} diverged"
        )

    # Hooks: same set modulo the ticket-09 allowlist — the declaration road
    # dispatches `experience_review` at Stage 4 (roster reference +
    # chain-supplied infra; the old-road driver builds no pool_data so the
    # post-boot `_wire_pool_to_resources` wiring never runs), and the
    # pool_data the new driver now builds wires the trace span hooks (the
    # old-road driver has no trace store). The old road's frozen baseline
    # registers the same hook classes in production (see the ticket-09
    # split-brain suite, which drives both roads WITH pool_data). The
    # LengthGuardHook wave (register_tree_aware_hooks — the shared seam on
    # BOTH roads) postdates the frozen golden: tolerated as a live extra,
    # presence-derived. The nudge wave (bot.yml roster reference for
    # task_delegation_nudge — declaration-road-only by design, self-gating
    # at runtime) likewise postdates the golden.
    new_hook_names = [h.name for h in new_main.hooks]
    golden_hook_names = [h.name for h in golden_main.hooks]
    assert set(new_hook_names) - set(golden_hook_names) == {
        "experience_review_hook",
        "RootSpanHook",
        "ChatSpanHook",
        "ToolSpanHook",
        "HandoffSpanHook",
        "ApprovalSpanHook",
        "length_guard",
        "task_delegation_nudge",
    }
    assert set(golden_hook_names) <= set(new_hook_names)

    # Toolsets: same names, same classes, same params — modulo the
    # allowlisted source relabeling (glue → roster-derived) and the
    # persistent-bash wave's presence-derived divergence (see
    # assert_bash_wave_parity).
    new_tools = {t.name: t for t in new_main.tools}
    golden_tools = {t.name: t for t in golden_main.tools}
    assert_bash_wave_parity(new_tools, golden_tools)
    for name in sorted(golden_tools):
        if name == "bash" and "bash_input" in new_tools:
            continue  # wave-replaced slot — asserted in assert_bash_wave_parity
        assert new_tools[name].tool_class == golden_tools[name].tool_class
        assert new_tools[name].params == golden_tools[name].params
        if name in ("task", "send_to_peer"):
            assert golden_tools[name].source == _COMM_SOURCE_OLD
            assert new_tools[name].source == _COMM_SOURCE_NEW
        elif name in _GLUE_ROSTER_SOURCES:
            assert golden_tools[name].source == "glue"
            assert new_tools[name].source == _GLUE_ROSTER_SOURCES[name]
        else:
            assert new_tools[name].source == golden_tools[name].source, (
                f"{name}.source diverged"
            )

    # Lazy subagent: the compiled effective spec gains the derived
    # send_to_agent entry (SPEC §5.2 — it appears in spec.tools); the
    # legacy road registers the same tool at materialize time via the
    # baked per-subagent default.
    new_lazy = next(a for a in new.agents if not a.materialized)
    golden_lazy = next(a for a in golden.agents if not a.materialized)
    assert new_lazy.agent_name == golden_lazy.agent_name == "office-expert"
    assert new_lazy.effective_spec_tools == _LAZY_TOOLS_NEW
    assert golden_lazy.effective_spec_tools == _LAZY_TOOLS_OLD

    # Stale-guard: the pinned orders must describe the actual roads. The
    # new road appends the bash_input companion after the roster block on
    # POSIX (the structural pair registers post-roster in native_core).
    assert [t.name for t in golden_main.tools] == _OLD_ROAD_TOOL_ORDER
    assert [t.name for t in new_main.tools] == _NEW_ROAD_TOOL_ORDER + (
        ["bash_input"] if persistent_bash_supported() else []
    )


async def test_declared_communication_registration_parity(tmp_path: Path) -> None:
    """AC (b): tree-derived registration, product parity with today.

    The root gets ``task`` (target = office-expert, its only direct child)
    and ``send_to_peer`` (opencode + review links, targets supplied by the
    Phase-2 peer wiring); the tools resolve through the TOOL-slot
    factories against the root's per-agent store — the pool-level store
    population (templates loop) is gone. office-expert gets
    ``send_to_agent`` at materialization (see the lazy-materialization
    test)."""
    boot = _boot_declaration(tmp_path / ".modex")
    declared = declared_pool_build(boot, "default")
    instance, _registry, broker = await _create_declared_pool(
        tmp_path, declared=declared, boot=boot
    )
    try:
        assert instance.comm_tools_derived is True

        task = instance.tool_manager.get_tool("task")
        peer = instance.tool_manager.get_tool("send_to_peer")
        assert isinstance(task, TaskDispatchTool)
        assert isinstance(peer, SendToPeerTool)

        # Root store: task targets BEFORE Phase 2 == the direct children.
        assert [t.name for t in task._store.list_subagents()] == ["office-expert"]  # noqa: SLF001
        assert task._store is instance.target_store  # noqa: SLF001
        assert [t.name for t in peer._store.list_peers()] == ["opencode", "reviewer"]  # noqa: SLF001

        # The full store is the per-agent root store (office-expert +
        # Phase-2 peers) — the legacy pool-level population is gone.
        assert [(t.name, t.kind.value) for t in instance.target_store.list()] == [
            ("office-expert", "subagent"),
            ("opencode", "normal"),
            ("reviewer", "normal"),
        ]
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()


async def test_lazy_materialization_from_compiled_spec(tmp_path: Path) -> None:
    """AC (e): office-expert's first dispatch materializes from the
    compiled spec — the template carries ``compiled_spec`` (the frozen
    legacy ``templates/*.yml`` is not its data source) and the
    materialized toolset matches the compilation: roster tools, the ACI
    edit, todo supplements, and ``send_to_agent`` resolved through the
    TOOL-slot factory."""
    boot = _boot_declaration(tmp_path / ".modex")
    declared = declared_pool_build(boot, "default")
    instance, _registry, broker = await _create_declared_pool(
        tmp_path, declared=declared, boot=boot
    )
    try:
        template = instance.pool.get_template("office-expert")
        assert template is not None
        assert template.compiled_spec is declared.subagents[0].spec
        assert template.toolset_profile.value == "read_write"

        materialized = await template.materialize(
            None, "inv-smoke", instance.pool.materialize_deps
        )
        assert materialized.descriptor.address.name == "office-expert"
        tool_manager = materialized.pipeline.tool_manager
        assert tool_manager is not None
        # The manager lists LLM-facing names — the aci_edit spec entry's
        # product is the AciEditTool named "edit". POSIX adds the
        # bash_input companion post-roster (structural pair with the
        # roster's bash slot).
        assert sorted(tool_manager.list_tools()) == sorted(
            [n if n != "aci_edit" else "edit" for n in _LAZY_TOOLS_NEW]
            + (["bash_input"] if persistent_bash_supported() else [])
        )
        assert isinstance(tool_manager.get_tool("send_to_agent"), SendToAgentTool)
        assert type(tool_manager.get_tool("edit")).__name__ == "AciEditTool"
        # Session-only subagent memory (the compiled spec's memory face).
        assert materialized.descriptor.memory_config.archive is None
        assert materialized.descriptor.memory_config.core is None
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()


async def test_restart_round_trip(tmp_path: Path) -> None:
    """AC (d): boot → exercise state (lazy materialization — the poller's
    first-dispatch half) → restart on the SAME data dir → identical
    behavior. The declaration is static per process: the second boot
    recompiles the same YAML, recovers the persisted session-tree state,
    and assembles an identical manifest. (A full LLM turn is not feasible
    in this driver — the placeholder provider owns model config — so the
    materialization + persisted-state recovery is the exercised surface.)"""
    data_dir = tmp_path / ".modex"

    boot = _boot_declaration(data_dir)
    declared = declared_pool_build(boot, "default")
    instance, registry, broker = await _create_declared_pool(
        tmp_path, declared=declared, boot=boot
    )
    first_manifest: AssemblyManifest
    try:
        lazy_agents = [
            AgentManifest(
                agent_name=sub.provenance.agent,
                materialized=False,
                effective_spec_tools=list(sub.spec.tools),
            )
            for sub in declared.subagents
        ]
        first_manifest = dump_assembly_manifest(
            instance,
            data_dir=data_dir,
            source_of=roster_source_map(registry, list(declared.root.spec.tools)),
            lazy_agents=lazy_agents,
        )
        # Exercise state past the boot: the poller's first-dispatch half
        # (lazy materialization) mutates pool + session state before the
        # restart below.
        template = instance.pool.get_template("office-expert")
        assert template is not None
        materialized = await template.materialize(
            None, "inv-roundtrip", instance.pool.materialize_deps
        )
        assert materialized is not None
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()

    # Restart on the same data dir: fresh boot, same declaration.
    boot2 = _boot_declaration(data_dir)
    declared2 = declared_pool_build(boot2, "default")
    instance2, registry2, broker2 = await _create_declared_pool(
        tmp_path, declared=declared2, boot=boot2
    )
    try:
        lazy_agents = [
            AgentManifest(
                agent_name=sub.provenance.agent,
                materialized=False,
                effective_spec_tools=list(sub.spec.tools),
            )
            for sub in declared2.subagents
        ]
        second_manifest = dump_assembly_manifest(
            instance2,
            data_dir=data_dir,
            source_of=roster_source_map(registry2, list(declared2.root.spec.tools)),
            lazy_agents=lazy_agents,
        )
        assert second_manifest == first_manifest
    finally:
        await instance2.pool.shutdown_all()
        await broker2.stop()
