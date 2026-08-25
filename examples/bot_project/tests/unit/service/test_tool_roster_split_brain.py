"""Ticket 05 — tool-roster split-brain (terminal trio + todo via roster).

Old road vs new road, same default configuration (SPEC §5.1 / §5.4):

- **Old road** — builders construction: the legacy ``_build_tools``
  branch that directly constructed preset/supplement tools, the bash
  SubprocessTool factory, and the Command/Process/Terminal trio. Deleted
  by the migration commit; its products live on in the frozen goldens.
- **New road** — roster + factories: tool names resolved through the
  real ``ComponentRegistry`` (BashToolFactory / ProcessToolFactory /
  TerminalToolFactory / TodoToolFactory / aci_edit), reading
  ``PoolRuntimeDeps`` from the context chain.

The goldens under ``fixtures/split_brain_05/`` were frozen BEFORE the
migration (baseline commit 4c857aac) by running the OLD road. The
migration reproduces them with the NEW road — any intentional difference
is listed in ``ALLOWED_DIFFERENCES`` (with a reason); any unlisted
difference turns red. Refreshing a golden is forbidden.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.pool.declaration import (
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER
from bot.workspace.pool_data import build_pool_data

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.memory.presets import main_agent_memory
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import PoolRuntimeDeps, resolution_context
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.tools.terminal import ProcessRegistry
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

from .assembly_manifest import (
    AgentManifest,
    AssemblyManifest,
    ToolEntry,
    assert_bash_wave_parity,
    dump_assembly_manifest,
    dump_tool_roster,
    roster_source_map,
    trio_registry_shared,
)

sys.path.insert(0, str(Path(__file__).parents[3]))

BOT_BASE = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "split_brain_05"

TRIO_NAMES = ("bash", "process", "terminal")


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



def _boot_declaration(data_dir: Path):
    """The real production boot: load + validate (V1-V11) + compile bot.yml."""
    return boot_scope_declaration(
        declaration_path=BOT_BASE / "config" / "scopes" / "bot.yml",
        project_dir=BOT_BASE,
        data_dir=data_dir,
        graphs_dirs=(BOT_BASE / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )


async def _create_default_pool(
    tmp_path: Path,
) -> tuple[Any, ComponentRegistry, Any, InMemoryMessageBroker]:
    """Production driver: full create_pool on the REAL shipped default pool."""
    registry = await _load_registry()
    declared = declared_pool_build(_boot_declaration(tmp_path / ".modex"), "default")
    deps = PoolAssemblyDeps(memory=main_agent_memory(max_context_tokens=200000))
    pool_data = await build_pool_data(
        WorkspaceContext(
            target=tmp_path, paths=WorkspacePaths(root=tmp_path / ".modex"), is_home=False
        ),
        "default",
        declared.pool.root_agent,
        MagicMock(spec=LLMProvider),
        deps,
        "",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
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
                pool_data=pool_data,
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
            )
    except BaseException:
        if instance is not None:
            await instance.pool.shutdown_all()
        await broker.stop()
        await pool_data.context_manager.memory_system.close()
        raise
    # NOTE: broker stop deferred — caller shuts the pool down (see tests).
    return instance, registry, broker, pool_data


async def _production_manifest(tmp_path: Path) -> AssemblyManifest:
    instance, registry, broker, pool_data = await _create_default_pool(tmp_path)
    try:
        declared = declared_pool_build(
            _boot_declaration(tmp_path / ".modex"), "default"
        )
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
        await pool_data.context_manager.memory_system.close()


# ── OLD road (deleted with the migration; products frozen in goldens) ───
#
# The baseline commit (4c857aac) drove the legacy ``_build_tools``
# branch — preset+supplement direct construction, the ``_make_bash``
# SubprocessTool(timeout=90) factory, and the directly constructed
# Command/Process/Terminal trio — and froze its products as
# ``old_road_default_tools.json`` / ``old_road_trio_tools.json``. The
# drivers are gone with the code; the goldens are the comparison record.


# ── NEW road drivers (roster + factories) ────────────────────────────────


async def _new_road_trio_tools() -> dict[str, Any]:
    """New road: trio names resolved through the real registry factories.

    ``use_terminal`` governs only the manager (infrastructure); the trio
    tools themselves are explicit roster opt-ins resolved against
    ``PoolRuntimeDeps`` — the SPEC §5.1 after-state.
    """
    registry = await _load_registry()
    manager = MagicMock(spec=TerminalManagerBase)
    pool_registry = ProcessRegistry()
    ctx = resolution_context(
        registry,
        _workspace_ctx(),
        PoolRuntimeDeps(terminal_manager=manager, process_registry=pool_registry),
    )
    tm = InMemoryToolManager(config=ToolManagerConfig())
    roster = dict.fromkeys(TRIO_NAMES, "roster")
    for name in TRIO_NAMES:
        factory = registry.resolve(ComponentSlot.TOOL, name)
        config = factory.config_model.model_validate({})
        tm.register(await factory.create(config, ctx))
    trio = [
        e for e in dump_tool_roster(tm, source_of=roster) if e.name in TRIO_NAMES
    ]
    return {"tools": trio, "trio_registry_shared": trio_registry_shared(tm)}


# ── Allowlist (intentional differences; every entry needs a reason) ─────


@dataclass(frozen=True)
class AllowedDifference:
    tool: str
    field: str
    old: Any
    new: Any
    reason: str


ALLOWED_DIFFERENCES: tuple[AllowedDifference, ...] = (
    AllowedDifference(
        tool="bash",
        field="timeout",
        old=90,
        new=300,
        reason=(
            "Legacy branch pinned SubprocessTool(timeout=90); the roster "
            "BashToolFactory — the production path since ADR-0041 — builds "
            "timeout=300. The roster value is authoritative: shipped "
            "(production) behavior is unchanged by ticket 05; only the "
            "dead legacy branch differed. No SPEC errata — pre-existing "
            "dead-branch divergence, roster is the shipped behavior."
        ),
    ),
)


def _assert_toolsets_equal(
    new_entries: list[ToolEntry],
    golden_entries: list[ToolEntry],
    allowlist: tuple[AllowedDifference, ...],
) -> None:
    """Tool-by-tool comparison: name + class + params, modulo allowlist.

    The persistent-bash wave's presence-derived divergence is allowed
    uniformly (POSIX no-terminal pools: PersistentBashTool + bash_input
    companion; terminal pools and Windows hosts: exact comparison).
    """
    new_by_name = {e.name: e for e in new_entries}
    golden_by_name = {e.name: e for e in golden_entries}
    assert_bash_wave_parity(new_by_name, golden_by_name)
    wave_replaced = "bash_input" in new_by_name
    for name in sorted(golden_by_name):
        golden, new = golden_by_name[name], new_by_name[name]
        if name == "bash" and wave_replaced:
            continue  # wave-replaced slot — asserted in assert_bash_wave_parity
        assert new.tool_class == golden.tool_class, (
            f"{name}: tool class {golden.tool_class} -> {new.tool_class}"
        )
        allowed_fields = {a.field for a in allowlist if a.tool == name}
        for key in sorted(set(golden.params) | set(new.params)):
            if key in allowed_fields:
                continue
            assert new.params.get(key) == golden.params.get(key), (
                f"{name}.{key}: {golden.params.get(key)!r} -> {new.params.get(key)!r} "
                "(not in allowlist)"
            )
    # Stale-allowlist guard: every allowed difference must match the actual
    # old/new values — an allowlist entry that no longer describes reality
    # is itself a red. The wave-replaced bash slot keeps the entry armed
    # for the Windows road (SubprocessTool), where it still applies.
    for diff in allowlist:
        if diff.tool == "bash" and "bash_input" in new_by_name:
            continue
        assert diff.tool in golden_by_name, f"allowlist names absent tool {diff.tool!r}"
        if diff.tool == "bash" and wave_replaced:
            continue
        assert golden_by_name[diff.tool].params.get(diff.field) == diff.old, (
            f"allowlist stale for {diff.tool}.{diff.field}: golden has "
            f"{golden_by_name[diff.tool].params.get(diff.field)!r}, expected {diff.old!r}"
        )
        assert new_by_name[diff.tool].params.get(diff.field) == diff.new, (
            f"allowlist stale for {diff.tool}.{diff.field}: new road has "
            f"{new_by_name[diff.tool].params.get(diff.field)!r}, expected {diff.new!r}"
        )


def _load_golden(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── Tests ───────────────────────────────────────────────────────────────


async def test_new_road_default_tools_match_old_road_golden(
    tmp_path: Path,
) -> None:
    """Split-brain: the production (roster) toolset is item-for-item the
    old road's toolset — names, classes, and key params — modulo the
    allowlisted bash timeout."""
    manifest = await _production_manifest(tmp_path)
    derived = {"task", "send_to_peer"}
    new_entries = [t for t in manifest.agents[0].tools if t.name not in derived]
    golden = _load_golden("old_road_default_tools.json")
    golden_entries = [ToolEntry.model_validate(e) for e in golden["tools"]]
    _assert_toolsets_equal(new_entries, golden_entries, ALLOWED_DIFFERENCES)


async def test_new_road_trio_tools_match_old_road_golden() -> None:
    """Split-brain: the roster-resolved trio is the old trio, and the
    pool-registry identity is shared in both roads."""
    dump = await _new_road_trio_tools()
    golden = _load_golden("old_road_trio_tools.json")
    golden_entries = [ToolEntry.model_validate(e) for e in golden["tools"]]
    _assert_toolsets_equal(dump["tools"], golden_entries, ())
    assert dump["trio_registry_shared"] is True


async def test_production_manifest_matches_golden(tmp_path: Path) -> None:
    """Production assembly manifest vs the frozen 05 baseline (the legacy
    road's products): identical modulo the allowlisted differences the
    07/09 migrations introduced — the derived communication entries
    (task/send_to_peer at Stage 4, send_to_agent in the lazy spec), the
    trace-span + experience-review hooks (pool_data supply), the
    length-guard hook (register_tree_aware_hooks — the shared seam on both
    roads, postdating the freeze), and the comm-tool source relabeling."""
    manifest = await _production_manifest(tmp_path)
    golden = AssemblyManifest.model_validate_json(
        (FIXTURES / "production_default.json").read_text(encoding="utf-8")
    )

    for field in (
        "pool_name",
        "execution_strategy",
        "terminal_manager",
        "trio_registry_shared",
        "todo_store",
        "interceptors",
        "commands",
        "comm_targets",
        "memory_hooks",
    ):
        assert getattr(manifest, field) == getattr(golden, field), (
            f"{field} diverged"
        )

    new_main = next(a for a in manifest.agents if a.materialized)
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

    new_hook_names = [h.name for h in new_main.hooks]
    golden_hook_names = [h.name for h in golden_main.hooks]
    assert set(golden_hook_names) <= set(new_hook_names)
    assert set(new_hook_names) - set(golden_hook_names) == {
        "experience_review_hook",
        "RootSpanHook",
        "ChatSpanHook",
        "ToolSpanHook",
        "HandoffSpanHook",
        "ApprovalSpanHook",
        # The LengthGuardHook wave (register_tree_aware_hooks — the shared
        # seam on BOTH roads) postdates the frozen 05 baseline.
        "length_guard",
    }

    new_tools = {t.name: t for t in new_main.tools}
    golden_tools = {t.name: t for t in golden_main.tools}
    assert_bash_wave_parity(
        new_tools, golden_tools, allowed_extra=frozenset({"task", "send_to_peer", "bash_input"})
    )
    assert set(new_tools) - set(golden_tools) <= {"task", "send_to_peer", "bash_input"}
    wave_replaced = "bash_input" in new_tools
    for name in sorted(golden_tools):
        if name == "bash" and wave_replaced:
            continue
        assert new_tools[name].tool_class == golden_tools[name].tool_class
        assert new_tools[name].params == golden_tools[name].params
        if name in ("task", "send_to_peer"):
            continue
        assert new_tools[name].source == golden_tools[name].source, (
            f"{name}.source diverged"
        )

    new_lazy = next(a for a in manifest.agents if not a.materialized)
    golden_lazy = next(a for a in golden.agents if not a.materialized)
    assert new_lazy.agent_name == golden_lazy.agent_name
    assert set(new_lazy.effective_spec_tools or []) == set(
        (golden_lazy.effective_spec_tools or []) + ["send_to_agent"]
    )


async def test_default_pool_aci_and_degradation_shapes(tmp_path: Path) -> None:
    """AC (b) + (c): aci supplement yields AciEditTool as the effective
    edit; use_terminal=false degrades bash with no manager and no
    process/terminal tools — the pool's PersistentBashTool + bash_input
    companion on POSIX, SubprocessTool on Windows hosts."""
    manifest = await _production_manifest(tmp_path)
    main = next(a for a in manifest.agents if a.materialized)
    tools = {t.name: t for t in main.tools}
    assert tools["edit"].tool_class == "AciEditTool"
    assert manifest.terminal_manager is None
    assert "process" not in tools
    assert "terminal" not in tools
    if "bash_input" in tools:
        assert tools["bash"].tool_class == "PersistentBashTool"
    else:
        assert tools["bash"].tool_class == "SubprocessTool"
    # Lazy subagent: todo supplement reaches the compiled effective spec.
    lazy = next(a for a in manifest.agents if not a.materialized)
    assert lazy.agent_name == "office-expert"
    assert "todo_write" in (lazy.effective_spec_tools or [])
    assert "todo_read" in (lazy.effective_spec_tools or [])
