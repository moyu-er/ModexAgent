"""Pool glue on the declaration road — memory / experience / notification.

The ticket-09 split-brain golden was removed: shipped ``bot.yml`` is
user-customizable configuration, and unit tests must not pin its contents.
What remains verifies the glue MECHANISMS on the declared road: Stage-4
roster dispatch of ``ExperienceReviewHook`` / ``UserNoticeCleanupHook``
(against chain-supplied infra) and position-derived subagent memory.
"""

from __future__ import annotations

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

from modex_agent.adapters.output import OutputAdapter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

from .assembly_manifest import (
    dump_memory_hooks,
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


def _compile_registry() -> ComponentRegistry:
    """DefaultPlugin-only registry for the compile step (the shipped
    declaration's ``capabilities:`` blocks resolve against it)."""
    from modex_agent.plugins.loader import PluginRegistrationContext

    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
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
        registry=_compile_registry(),
    )


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
        from modex_agent.plugins.defaults.capabilities.experience.review_hook import (
            ExperienceReviewHook,
        )

        main_instance = instance.pool._agents.get(  # noqa: SLF001
            instance.root_agent_name
        )
        assert main_instance is not None
        react_hooks = [s.hook for s in main_instance.pipeline.hook_runner.hook_specs]
        review_hooks = [h for h in react_hooks if isinstance(h, ExperienceReviewHook)]
        assert len(review_hooks) == 1, "exactly one ExperienceReviewHook (Stage-4 dispatch)"
        hook = review_hooks[0]
        # The chain-supplied infra: the pool's memory system + the experience
        # capability supply (the retired pool_data carrier died with the
        # supply face, SPEC §8.3). The reviewer is registered on the supply
        # (built on the bot-global default provider).
        assert hook._memory_system is pool_data.context_manager.memory_system  # noqa: SLF001
        supply = instance.pool.materialize_deps.capability_supply["experience"]  # noqa: SLF001
        assert supply.review_agent_for(instance.root_agent_name) is not None
        assert hook._catalog.experience_dir == supply.experience_dir  # noqa: SLF001

        memory_hooks = dump_memory_hooks(pool_data)
        # The default pool's MAIN does not declare the todo capability:
        # ``todo_reorientation`` rides the roster→memory-runner dispatch,
        # which registers it only on todo-capable agents (the retired
        # unconditional create_pool injection died with the todo supply
        # convergence, SPEC §8.2 — office-expert, the pool's todo sub,
        # carries the hook on its own session-only memory system).
        assert [h.hook_class for h in memory_hooks] == [
            "UserNoticeCleanupHook",
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
            None,
            "inv-glue",
            instance.pool.materialize_deps,  # noqa: SLF001
        )
        assert materialized.descriptor.memory_config == subagent_memory()
    finally:
        await instance.pool.shutdown_all()
        await broker.stop()
