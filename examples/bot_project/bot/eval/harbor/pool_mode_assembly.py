"""Scope-declaration pool assembly for one harbor pool-mode eval trial.

Assembles through the SAME declaration road production uses (SPEC §3/§4):
load ``config/scopes/bot.yml`` → apply the selected eval overlay plus the
runtime approval choice → validate + compile (``boot_scope_spec``) → partition
(``declared_pool_build``) → ``create_pool`` with eval-appropriate
substitutions: in-memory broker, null output adapter, no MCP/KB, and
approval stripped from the target root when ``MODEX_APPROVAL=off``.
Persistence follows production wiring: with the SQLITE backend selected,
the assembly opens the job-scoped ``state.db``
(``WorkspacePersistenceManager``) so the pool's memory runs the hybrid
scheme (ADR-0023) and the .db lands under the job's pool-data for local
inspection; ``execute_pool_entry`` closes it at trial end.

``MODEX_EVAL_ROSTER=benchmark`` selects the declarative benchmark arm. Its
single-agent topology, reduced tool roster, memory override, and file-backed
prompt are all applied before validation and compilation.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from plugins.bot_strategies import BotDefaultLLMConfig

from bot.eval.agent_harness import static_system_prompt
from bot.eval.harbor.eval_overlay import EvalArmName, load_eval_arm
from bot.eval.harbor.pool_mode_types import (
    PoolApprovalMode,
    PoolModeConfig,
    build_model_config,
)
from bot.service.builders import (
    _build_hook_runner,
    build_session_store,
    resolve_declared_root_prompt,
)
from bot.service.model_config import BotModelConfig
from bot.service.pool.declaration import (
    DeclaredPoolBuild,
    ScopeBoot,
    boot_scope_spec,
    declared_pool_build,
)
from bot.workspace.handle import PoolWorkspaceResources, WorkspaceResolverCell
from bot.workspace.pool_data import PoolData, build_pool_data
from bot.workspace.wiring import build_tool_overflow_interceptor_chain
from bot.workspace.wiring.stack import declared_assembly_deps
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.hook import Hook, HookRunner
from modex_agent.hook.builtin import CurrentTimeInjectionHook
from modex_agent.hook.builtin.knowledge_hook import KnowledgeHook
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.observability import TraceBackend
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.pipeline.adapters import NullOutputAdapter, OutputAdapter
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import AgentOverlay, PoolOverlay, apply_scope_overlay
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

logger = logging.getLogger(__name__)

_BOT_DEFAULT_LLM_PROVIDER = "bot_default"
# Benchmark roster switch. The env name is mirrored in agent.py's
# POOL_MODE_ENV_VARS so host_runtime/installed_agent forward it to trials.
MODEX_EVAL_ROSTER: Final = "MODEX_EVAL_ROSTER"
BENCHMARK_ROSTER_VALUE: Final = "benchmark"


def _benchmark_roster_enabled(environment: Mapping[str, str]) -> bool:
    return environment.get(MODEX_EVAL_ROSTER, "").strip().lower() == BENCHMARK_ROSTER_VALUE


@dataclass(frozen=True)
class EvalPoolAssembly:
    """One trial's declaration-road assembly inputs plus its resources."""

    declared: DeclaredPoolBuild
    assembly_deps: PoolAssemblyDeps
    pool_data: PoolData
    resources: PoolWorkspaceResources
    resolver_cell: WorkspaceResolverCell
    app_config: AppConfig
    bot_model_config: BotModelConfig | None
    output_adapter: OutputAdapter
    shared_hooks: list[Hook]
    shared_hook_runner: HookRunner
    shared_interceptor_chain: InterceptorChain
    session_store: SessionStore | None
    session_registry: InMemorySessionRegistry
    retention: SessionRetentionPolicy
    persistence: WorkspacePersistenceManager | None = None


def _load_eval_app_config(project_dir: Path, environment: Mapping[str, str]) -> AppConfig:
    """Load the bot's ``bot_config.yml`` for one trial with env-driven observability.

    Uses the production loader (``AppConfig.from_yaml`` with ``${ENV}``
    interpolation) so eval and bot runtime share one config path. The loader
    resolves against ``os.environ``, which differs from the trial environment
    mapping under tests, so the observability fields are re-applied from
    ``environment`` afterwards, mirroring ``agent_harness._eval_observability``'s
    env contract (``OTEL_FORMAT``/``OTEL_TRACES_ENDPOINT``/``LANGFUSE_HOST``/
    ``LANGFUSE_BASIC_AUTH``).
    """
    app_config = AppConfig.from_yaml(project_dir / "config" / "bot_config.yml")
    observability = app_config.observability
    if observability is None:
        return app_config
    updates: dict[str, object] = {}
    raw_backend = environment.get("OTEL_FORMAT")
    if raw_backend is not None:
        try:
            updates["trace_backend"] = TraceBackend(raw_backend.lower())
        except ValueError:
            logger.warning("Unknown OTEL_FORMAT=%s; keeping configured backend", raw_backend)
    if otel_endpoint := environment.get("OTEL_TRACES_ENDPOINT"):
        updates["otel_endpoint"] = otel_endpoint
    if langfuse_host := environment.get("LANGFUSE_HOST"):
        updates["eval_ingestion_url"] = f"{langfuse_host}/api/public/ingestion"
    if basic_auth := environment.get("LANGFUSE_BASIC_AUTH"):
        updates["otel_headers"] = {
            "Authorization": f"Basic {basic_auth}",
            "x-langfuse-ingestion-version": "4",
        }
    if not updates:
        return app_config
    return app_config.model_copy(update={"observability": observability.model_copy(update=updates)})


def _declaration_path(config: PoolModeConfig) -> Path:
    return config.project_dir / "config" / "scopes" / "bot.yml"


async def build_eval_pool_assembly(
    config: PoolModeConfig,
    *,
    trace_store: OtelSpanTraceStore,
    broker: InMemoryMessageBroker,
    component_registry: ComponentRegistry,
) -> EvalPoolAssembly:
    app_config = _load_eval_app_config(config.project_dir, config.budget_environment)
    benchmark = _benchmark_roster_enabled(config.budget_environment)
    spec = load_scope_declaration(_declaration_path(config))
    declared_pools = spec.workspace.pools if spec.workspace is not None else [spec.pool]
    target_pool = next(
        (pool for pool in declared_pools if pool is not None and pool.name == config.pool_name),
        None,
    )
    if target_pool is None:
        msg = f"eval target pool {config.pool_name!r} is absent from the scope declaration"
        raise ValueError(msg)
    root_agent_name = target_pool.root_agent.name
    arm_name = EvalArmName.BENCHMARK if benchmark else EvalArmName.DEFAULT
    overlay = load_eval_arm(
        config.project_dir / "config" / "scopes" / "eval" / "eval.yml",
        arm_name.value,
    ).to_scope_overlay(
        config.pool_name,
        root_agent_name,
        component_registry.names(ComponentSlot.TOOL),
    )
    if config.approval is PoolApprovalMode.OFF:
        pools = dict(overlay.pools)
        pool_overlay = pools.get(config.pool_name, PoolOverlay())
        agents = dict(pool_overlay.agents)
        root_overlay = agents.get(root_agent_name, AgentOverlay())
        agents[root_agent_name] = root_overlay.model_copy(update={"strip_approval": True})
        pools[config.pool_name] = pool_overlay.model_copy(update={"agents": agents})
        overlay = overlay.model_copy(update={"pools": pools})
    spec = apply_scope_overlay(spec, overlay)
    scope_boot: ScopeBoot = boot_scope_spec(
        spec,
        project_dir=config.project_dir,
        data_dir=config.data_dir,
        graphs_dirs=(),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
        registry=component_registry,
        observability=app_config.observability,
    )
    declared = declared_pool_build(scope_boot, config.pool_name)
    bot_model_config = build_model_config(config.entry)
    assembly_deps = declared_assembly_deps(
        declared.root,
        max_context_tokens=bot_model_config.max_context_tokens,
    )
    system_prompt = static_system_prompt(
        await resolve_declared_root_prompt(
            declared,
            config.project_dir,
            component_registry,
        )
    )
    workspace = WorkspaceContext(
        target=config.entry.task_workspace,
        paths=WorkspacePaths(root=config.data_dir),
        is_home=False,
    )
    default_provider: LLMProvider = await component_registry.resolve(
        ComponentSlot.LLM_PROVIDER,
        _BOT_DEFAULT_LLM_PROVIDER,
    ).create(
        BotDefaultLLMConfig(),
        AssemblyContext(registry=component_registry, workspace_ctx=workspace),
    )
    # Production wiring mirror (resources.py): SQLITE backend → hybrid scheme
    # (ADR-0023). build_pool_data builds the HybridMemoryStoreRegistry from
    # this manager; execute_pool_entry's finally closes it after the pools
    # stop, WAL-checkpointing <job>/agent/pool-data/state.db for inspection.
    persistence: WorkspacePersistenceManager | None = None
    if app_config.persistence.backend is PersistenceBackend.SQLITE:
        persistence = WorkspacePersistenceManager(workspace.paths.state_db)
        await persistence.open()
    # Everything after persistence.open() until the return is covered: a
    # raise anywhere in the tail (store/resource construction, benchmark
    # shell spawn) must not leak the open state.db handle — the caller only
    # closes persistence through the returned assembly's trial teardown.
    try:
        pool_data = await build_pool_data(
            workspace,
            config.pool_name,
            declared.pool.root_agent,
            default_provider,
            assembly_deps,
            system_prompt,
            app_config=app_config,
            persistence=persistence,
            trace_store=trace_store,
        )
        session_store = build_session_store(
            app_config,
            None,
            workspace.paths.session_index_dir,
            lambda _session: config.pool_name,
            app_config.paths.data_dir_name,
        )
        overflow_store = LocalFileToolOverflowStore(workspace=workspace.paths.overflow_dir)
        resources = PoolWorkspaceResources(
            target=config.entry.task_workspace,
            ctx=workspace,
            overflow_store=overflow_store,
            session_index_store=session_store,
            broker=broker,
            pool_data={config.pool_name: pool_data},
        )
        output_adapter = NullOutputAdapter()
        # Production wiring mirror (resources.py): the observability-driven
        # training hooks ride the shared runner (both read per-turn state
        # from ctx.runtime.services — stateless instances shared per pool).
        shared_hooks = [CurrentTimeInjectionHook(), KnowledgeHook()]
        if app_config.observability is not None:
            from modex_agent.hook.builtin.checkpoint import CheckpointHook
            from modex_agent.hook.builtin.training_data import TrainingDataHook

            if app_config.observability.checkpoint_per_iteration:
                shared_hooks.append(CheckpointHook())
            if app_config.observability.training_relevant:
                shared_hooks.append(
                    TrainingDataHook(
                        max_iterations=app_config.observability.training_max_iterations,
                        max_tokens=app_config.observability.training_max_tokens,
                    )
                )
        interceptor_chain = build_tool_overflow_interceptor_chain(
            overflow_store,
            control_channel=None,
        )
        # Control-drain/LLM-cancel are omitted because harbor eval has no control channel.
        resolver_cell = WorkspaceResolverCell()
        retention_cfg = app_config.multi_agent.session_retention
        return EvalPoolAssembly(
            declared=declared,
            assembly_deps=assembly_deps,
            pool_data=pool_data,
            resources=resources,
            resolver_cell=resolver_cell,
            app_config=app_config,
            bot_model_config=bot_model_config,
            output_adapter=output_adapter,
            shared_hooks=shared_hooks,
            shared_hook_runner=_build_hook_runner(shared_hooks),
            shared_interceptor_chain=interceptor_chain,
            session_store=session_store,
            session_registry=InMemorySessionRegistry(store=session_store),
            retention=SessionRetentionPolicy(
                max_sessions_per_subagent=retention_cfg.max_sessions_per_subagent,
                max_sessions_global=retention_cfg.max_sessions_global,
                ttl_seconds=retention_cfg.ttl_seconds,
                cleanup_interval_seconds=retention_cfg.cleanup_interval_seconds,
            ),
            persistence=persistence,
        )
    except BaseException:
        if persistence is not None:
            with contextlib.suppress(BaseException):
                await persistence.close()
        raise
