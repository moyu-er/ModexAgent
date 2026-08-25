"""Ticket 09 — pool glue A split-brain: memory / experience / notification.

Old road vs new road for the ``default`` pool, driven WITH pool_data so the
manifest observes the faces this ticket migrates:

- **memory** — the main agent's ``descriptor.memory_config`` (the two-preset
  caller branch vs position-derived defaults + node override) and the
  pool-level memory system built from it;
- **notification** — the memory runner's cleanup hooks (``UserNoticeCleanupHook``
  code-wired in ``create_pool`` vs dispatched at Stage 4 from the roster
  reference);
- **experience** — the react hook set (``ExperienceReviewHook`` wired
  post-boot by ``_wire_pool_to_resources`` vs dispatched at Stage 4 from the
  roster reference against chain-supplied infra).

Split-brain discipline (plan §Verification strategy, same as tickets 05/07):
the BASELINE commit freezes the OLD road's products (with the new
``memory_hooks`` face) as a golden fixture BEFORE any production change; the
migration commit re-drives the same configuration down the NEW road and
compares field by field. Intentional differences live in the explicit
allowlist (each entry names its reason); bare golden refreshes are forbidden.
"""

from __future__ import annotations

import json
import sys
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
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
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
    assert_bash_wave_parity,
    dump_assembly_manifest,
    dump_memory_hooks,
    roster_source_map,
)

sys.path.insert(0, str(Path(__file__).parents[3]))

BOT_BASE = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "split_brain_09"

_MAX_CONTEXT_TOKENS = 200000


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




async def _build_pool_data(tmp_path: Path, root_agent: Any, deps: PoolAssemblyDeps):
    """Build the default pool's PoolData bound to the tmp workspace."""
    return await build_pool_data(
        _workspace_ctx(tmp_path / ".modex"),
        "default",
        root_agent,
        MagicMock(spec=LLMProvider),
        deps,
        "",
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


async def _new_road_manifest(tmp_path: Path):
    """NEW road: scope-declaration boot with the ticket-09 glue.

    Supply shape: pool_data + the declaration-road Phase-2 (peer targets
    resolve through the FW service against the declared links). The
    retired glue never runs: no ``register_communication_tools`` (derived
    entries) and no post-boot experience wiring (the declaration
    references experience_review from the roster — Stage 4 dispatches it
    against the chain-supplied infra).
    """
    declared = declared_pool_build(_boot_declaration(tmp_path / ".modex"), "default")
    registry = await _load_registry()
    deps = declared_assembly_deps(declared.root, max_context_tokens=_MAX_CONTEXT_TOKENS)
    pool_data = await _build_pool_data(tmp_path, declared.pool.root_agent, deps)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()
    instance = None
    manifest = None
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
        boot = _boot_declaration(tmp_path / ".modex")
        links = peer_links_from_declaration(boot.spec)
        if instance.name in links:
            pools = {instance.name: instance}
            assert boot.spec.workspace is not None
            spec_pools = {pool.name: pool for pool in boot.spec.workspace.pools}
            for pool_name, pool in spec_pools.items():
                if pool_name == instance.name:
                    continue
                peer_root = next(a for a in pool.agents if a.parent is None)
                stub = MagicMock()
                stub.root_agent_name = peer_root.name
                stub.tree_manager = MagicMock()
                pools[pool_name] = stub
            resolve_peer_targets(pools, links)

        from .assembly_manifest import AgentManifest

        lazy_agents = [
            AgentManifest(
                agent_name=sub.provenance.agent,
                materialized=False,
                effective_spec_tools=list(sub.spec.tools),
            )
            for sub in declared.subagents
        ]
        manifest = dump_assembly_manifest(
            instance,
            data_dir=tmp_path / ".modex",
            source_of=roster_source_map(registry, list(declared.root.spec.tools)),
            lazy_agents=lazy_agents,
            memory_hooks=dump_memory_hooks(pool_data),
        )
    finally:
        if instance is not None:
            await instance.pool.shutdown_all()
        await broker.stop()
        await pool_data.context_manager.memory_system.close()
    assert manifest is not None
    return manifest


async def _declared_boot(tmp_path: Path):
    """Boot the declared pool WITHOUT tearing it down.

    The behavior tests need the LIVE instance (the pool's agent registry
    and templates are consumed by ``shutdown_all``), so this variant skips
    the manifest dump and returns the live products; the CALLER owns the
    teardown.
    """
    declared = declared_pool_build(_boot_declaration(tmp_path / ".modex"), "default")
    registry = await _load_registry()
    deps = declared_assembly_deps(declared.root, max_context_tokens=_MAX_CONTEXT_TOKENS)
    pool_data = await _build_pool_data(tmp_path, declared.pool.root_agent, deps)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()
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
    return instance, pool_data, broker


async def test_split_brain_glue_manifest_field_by_field(tmp_path: Path) -> None:
    """AC (a)+(c)+(e): the SAME default configuration, old glue vs declared
    glue, produce identical memory + notification faces — memory_config
    comes from position-derived defaults + node override (no two-preset
    caller branch), the memory cleanup hooks are the same pair in the same
    order (Stage-4 roster dispatch vs code-wiring)."""
    new = await _new_road_manifest(tmp_path)
    golden = json.loads((FIXTURES / "old_road_glue_manifest.json").read_text("utf-8"))

    for field in (
        "pool_name",
        "execution_strategy",
        "terminal_manager",
        "todo_store",
        "interceptors",
        "commands",
        "comm_targets",
        # The notification face: same hook pair, same order (UserNotice →
        # TodoReorientation) — the only difference is WHO registers
        # UserNotice (Stage-4 roster dispatch vs code-wired default).
        "memory_hooks",
    ):
        assert new.model_dump(mode="json")[field] == golden[field], (
            f"{field} diverged"
        )

    new_main = next(a for a in new.agents if a.materialized)
    golden_main = next(a for a in golden["agents"] if a["materialized"])
    # The memory face: position-derived defaults + override (new road) ≡
    # the two-preset caller branch (old road), byte-for-byte.
    assert new_main.memory_config == golden_main["memory_config"], (
        "main agent memory_config diverged"
    )

    # React hooks: same SET (both roads register ExperienceReviewHook —
    # the old road via post-boot wiring, the new road via Stage-4 roster
    # dispatch); only the POSITION differs (Stage-4 dispatch lands the hook
    # earlier). The LengthGuardHook wave (register_tree_aware_hooks — the
    # shared seam on BOTH roads) postdates the frozen golden: tolerated as
    # the single live extra, presence-derived. Stale-guard both orders.
    new_hook_names = [h.name for h in new_main.hooks]
    golden_hook_names = [h["name"] for h in golden_main["hooks"]]
    assert set(new_hook_names) - set(golden_hook_names) == {"length_guard"}
    assert set(golden_hook_names) <= set(new_hook_names)
    assert new_hook_names.index("experience_review_hook") < new_hook_names.index(
        "turn_outcome_notify"
    )
    assert golden_hook_names.index("experience_review_hook") > golden_hook_names.index(
        "turn_outcome_notify"
    )

    # Toolsets: same names (the 07 comparison proved the tool face; here
    # the set equality is the regression guard) — modulo the
    # persistent-bash wave's presence-derived divergence.
    new_tools = {t.name: t for t in new_main.tools}
    golden_tools = {t["name"]: t for t in golden_main["tools"]}
    assert_bash_wave_parity(new_tools, golden_tools)


async def test_declared_glue_components_are_roster_dispatched(
    tmp_path: Path,
) -> None:
    """AC (b)+(c): on the declaration road, ExperienceReviewHook rides the
    main pipeline's react hook_runner (Stage-4 dispatch of the roster
    reference, review agent built on the supplied bot-global provider) and
    UserNoticeCleanupHook rides the memory runner — the legacy-road /
    factory code-wired constructions never run for this pool."""
    instance, pool_data, broker = await _declared_boot(tmp_path)
    try:
        from modex_agent.hook.builtin.experience_review import ExperienceReviewHook

        main_instance = instance.pool._agents.get(  # noqa: SLF001
            instance.root_agent_name
        )
        assert main_instance is not None
        react_hooks = [s.hook for s in main_instance.pipeline.hook_runner.hook_specs]
        review_hooks = [h for h in react_hooks if isinstance(h, ExperienceReviewHook)]
        assert (
            len(review_hooks) == 1
        ), "exactly one ExperienceReviewHook (Stage-4 dispatch)"
        hook = review_hooks[0]
        # The chain-supplied infra: the bot-global default provider (not any
        # pool provider) + the pool's memory system + experience dir.
        assert hook._agent._provider is not None  # noqa: SLF001
        assert hook._memory_system is pool_data.context_manager.memory_system  # noqa: SLF001
        assert hook._get_dir() == pool_data.experience_dir  # noqa: SLF001

        memory_hooks = dump_memory_hooks(pool_data)
        assert [h.hook_class for h in memory_hooks] == [
            "UserNoticeCleanupHook",
            "TodoReorientationHook",
        ]
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()
        await pool_data.context_manager.memory_system.close()


async def test_lazy_subagent_memory_is_position_derived(tmp_path: Path) -> None:
    """AC (a) non-root half: the declared template carries memory=None — the
    materialized subagent's memory config derives from position (session-only
    preset + the compiled spec's memory overrides), identical to the legacy
    seeded preset."""
    instance, _pool_data, broker = await _declared_boot(tmp_path)
    try:
        from modex_agent.memory.presets import subagent_memory

        template = instance.pool.get_template("office-expert")  # noqa: SLF001
        assert template is not None
        assert template.memory is None, "declaration-road templates carry no preset"
        materialized = await template.materialize(
            None, "inv-glue", instance.pool.materialize_deps  # noqa: SLF001
        )
        assert materialized.descriptor.memory_config == subagent_memory()
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()
