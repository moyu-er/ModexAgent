"""Per-workspace resource construction and teardown (re-home of _on_workspace_activate)."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.kb.provider import KbProvider
    from bot.service.core import BotService
    from modex_agent.persistence.managers import WorkspacePersistenceManager

from bot.service.builders import (
    _build_hook_runner,
    _build_main_command_processor,
    resolve_declared_root_prompt,
)
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool.declaration import (
    DeclaredPoolBuild,
    ScopeBoot,
    apply_workspace_resource_selection,
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.session_pool_index import SessionPoolIndex
from bot.workspace.background import BackgroundTaskRunner
from bot.workspace.dynamic_workspaces import dynamic_workspace_declaration_path
from bot.workspace.handle import (
    PoolWorkspaceResources,
    WorkspaceHandle,
    WorkspaceResolverCell,
)
from bot.workspace.pool_data import build_pool_data
from bot.workspace.wiring.stack import declared_assembly_deps
from modex_agent.approval.ui import IMUserInterface
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.session_id import SessionInfo, session_id_prefix_of
from modex_agent.hook.builtin import CurrentTimeInjectionHook
from modex_agent.hook.builtin.control_drain import (
    ControlDrainInterceptor,
    LlmCancelInterceptor,
)
from modex_agent.hook.builtin.knowledge_hook import KnowledgeHook
from modex_agent.interceptor.builtin import ToolResultLimitInterceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.communication.peer_resolution import (
    peer_links_from_declaration,
    resolve_peer_targets,
)
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_router import PoolRouter, agent_pool_ownership
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.overflow.store import ToolOverflowStore
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.tools.terminal.persistent_bash import PersistentBashTool
from modex_agent.workspace.context import WorkspaceContext

logger = logging.getLogger(__name__)


class _PoolShutdownIncompleteError(RuntimeError):
    pass


class ScopeBootRequiredError(RuntimeError):
    """No scope declaration found — the legacy roster road is deleted.

    The old ``config/pools/<name>/pool.yml`` + ``templates/*.yml`` format
    is no longer read; pools are declared in the scope declaration
    (``config/scopes/bot.yml``). The message points the operator at the
    migration path instead of silently mis-reading the old format.
    """

    def __init__(self, declaration_path: Path, project_dir: Path) -> None:
        old_pools_dir = project_dir / "config" / "pools"
        detail = (
            f"no scope declaration at {declaration_path} — pools are declared "
            "in the scope declaration and the legacy roster format is no "
            "longer read"
        )
        if old_pools_dir.exists():
            detail += (
                f" (found legacy {old_pools_dir} — migrate its pools into the"
                " declaration: each config/pools/<name>/pool.yml main agent"
                " and templates/*.yml subagent become one pool tree under"
                " the declaration's workspace.pools)"
            )
        super().__init__(detail)


def _declaration_road_pools(scope_boot: ScopeBoot) -> list[str]:
    """Every pool the declaration hosts, in declaration order (the pool
    list source since ticket 11 — the PoolStore disk scan is deleted).
    """
    if scope_boot.spec.pool is not None:
        return [scope_boot.spec.pool.name]
    if scope_boot.spec.workspace is not None:
        return [pool.name for pool in scope_boot.spec.workspace.pools]
    return []


# ──────────────────────────────────────────────────────────────────────────
# Per-workspace resource construction (re-home of _on_workspace_activate +
# the per-pool body of _initialize_pool, bound to ONE workspace)
# ──────────────────────────────────────────────────────────────────────────


class _WorkspaceBuildState:
    def __init__(self) -> None:
        self.persistence: WorkspacePersistenceManager | None = None
        self.resources: PoolWorkspaceResources | None = None

    async def rollback(self) -> None:
        if self.resources is not None:
            await _stop_resources(self.resources)
        elif self.persistence is not None:
            with contextlib.suppress(BaseException):
                await self.persistence.close()


async def _build_resources(service: BotService, ctx: WorkspaceContext) -> PoolWorkspaceResources:
    state = _WorkspaceBuildState()
    try:
        return await _assemble_resources(service, ctx, state)
    except BaseException:
        await state.rollback()
        raise


async def _assemble_resources(
    service: BotService,
    ctx: WorkspaceContext,
    state: _WorkspaceBuildState,
) -> PoolWorkspaceResources:
    """Build one workspace's full resource bundle (the business ``R``).

    Re-homes FAITHFULLY the per-workspace construction the old
    ``_on_workspace_activate`` + ``_initialize_pool`` did. Uses
    PER-WORKSPACE broker/inbox/bus/interceptor rooted at ``ctx.paths``.

    Ticket 14 contract: ``service._app_config`` is the declaration-resolved
    config view (``apply_workspace_resource_selection`` ran once at
    ``BotService.initialize``). Ticket 17 re-applies the selection for THIS
    workspace's declaration below — idempotent for the primary declaration,
    effective for a dynamic workspace's backend override.
    """
    from bot.service.builders import build_pool_routing_store, build_session_store
    from bot.service.pool import create_pool
    from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

    app_config = service._app_config
    if app_config is None:
        raise RuntimeError("AppConfig must be loaded before workspace resource assembly")

    # Ticket 11 — the scope declaration is the single pool source and the
    # single boot road: load + validate (V1-V11 incl. the V10 graph
    # cross-check) + compile the FULL declaration once per workspace
    # build; every declared pool consumes the products. A deployment
    # WITHOUT a declaration fails loudly — the legacy roster road
    # (config/pools/<name>/pool.yml + templates/*.yml) is deleted and no
    # longer read.
    # Ticket 17 — a runtime-created workspace boots ITS OWN declaration
    # (config/scopes/workspaces/<name>.yml); every other target (home,
    # /cd'd directories) boots the primary declaration as before.
    workspace_graphs_dir = ctx.target / "config" / "graphs"
    global_graphs_dir = service._project_dir / "config" / "graphs"
    declaration_path = service._project_dir / "config" / "scopes" / "bot.yml"
    if not declaration_path.exists():
        raise ScopeBootRequiredError(declaration_path, service._project_dir)
    dynamic_declaration = dynamic_workspace_declaration_path(
        service._project_dir, ctx.target
    )
    if dynamic_declaration is not None:
        declaration_path = dynamic_declaration
    scope_boot = boot_scope_declaration(
        declaration_path=declaration_path,
        project_dir=service._project_dir,
        data_dir=ctx.paths.root,
        graphs_dirs=(workspace_graphs_dir, global_graphs_dir),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )
    # Ticket 17 — per-workspace resource selection: apply THIS workspace's
    # declaration overrides onto the service config view (idempotent for
    # the primary declaration, whose overrides were resolved once at
    # service boot; a dynamic workspace's backend selection takes effect
    # here). A per-workspace paths override is refused loudly — the
    # WorkspaceContext is built with the service-level layout, so a
    # differing data_dir_name would silently split the workspace's stores.
    effective_app_config = apply_workspace_resource_selection(app_config, scope_boot.spec)
    if effective_app_config.paths.data_dir_name != app_config.paths.data_dir_name:
        raise RuntimeError(
            f"{declaration_path.name}: declares paths.data_dir_name "
            f"{effective_app_config.paths.data_dir_name!r} but the workspace "
            "context is built with the service-level layout "
            f"({app_config.paths.data_dir_name!r}) — a per-workspace path "
            "layout would silently split this workspace's stores"
        )
    app_config = effective_app_config
    pool_names = sorted(_declaration_road_pools(scope_boot))

    logger.info(
        "[workspace-build] target=%s mcp_registry=%s pools=%s",
        ctx.target,
        "set" if service._mcp_registry is not None else "None",
        pool_names,
    )

    # T26: open the workspace SQLite DB when backend is SQLITE. The
    # ConnectionManager is shared by all SQLite adapters in this workspace;
    # it closes at evict time (after producers/pools/broker stop) in
    # _stop_resources. FILE backend leaves persistence=None.
    persistence: WorkspacePersistenceManager | None = None
    owns_persistence = False
    if app_config is not None and app_config.persistence.backend is PersistenceBackend.SQLITE:
        from modex_agent.persistence.managers import WorkspacePersistenceManager

        if ctx.target == service._project_dir.resolve():
            persistence = service._home_persistence
            assert persistence is not None, "Home persistence must open before materialization"
        else:
            db_path = ctx.paths.root / "state.db"
            persistence = WorkspacePersistenceManager(db_path)
            await persistence.open()
            state.persistence = persistence
            owns_persistence = True
            logger.info("[workspace-build] SQLite workspace DB opened at %s", db_path)

    max_context_tokens = (
        service._bot_model_config.max_context_tokens
        if service._bot_model_config is not None
        else None
    )
    declared_builds: dict[str, DeclaredPoolBuild] = {}
    for name in pool_names:
        declared_builds[name] = declared_pool_build(scope_boot, name)
    # ONE assembly-deps road: position-derived defaults from each pool's
    # compiled root (ticket 14; the pool.yml fallback synthesis died with
    # the legacy road).
    assembly_deps: dict[str, PoolAssemblyDeps] = {}
    for name in pool_names:
        assembly_deps[name] = declared_assembly_deps(
            declared_builds[name].root,
            max_context_tokens=max_context_tokens,
        )

    # 1. Workspace-level stores.
    ctx.paths.mkdir_skeleton()
    overflow_store = LocalFileToolOverflowStore(workspace=ctx.paths.overflow_dir)
    session_index_store = build_session_store(
        app_config,
        persistence,
        session_index_dir=ctx.paths.session_index_dir,
        pool_resolver=lambda session: (
            (
                service._pool_session_store.get(session.session_id_prefix, "")
                if service._pool_session_store is not None
                else ""
            )
            or "main"
        ),
        data_dir_name=app_config.paths.data_dir_name,
    )
    from modex_agent.core.session_registry import InMemorySessionRegistry

    _routing_store = service._pool_session_store

    async def _on_session_registered(session: SessionInfo) -> None:
        if _routing_store is None:
            return
        prefix = session.session_id_prefix
        if _routing_store.get(prefix, None) is not None:
            return
        pool = session.metadata.get("pool")
        if pool is None and session.parent_session_id is not None:
            parent_prefix = session_id_prefix_of(session.parent_session_id)
            pool = _routing_store.get(parent_prefix, None)
        if pool is not None:
            _routing_store.set(prefix, str(pool))

    session_registry = InMemorySessionRegistry(
        store=session_index_store, on_register=_on_session_registered
    )
    await session_registry.load_all()

    # 2. Per-workspace broker (cross-process wakeup). The inbox/bus are now
    #    per-pool (Task 7) — built inside create_pool, one set per pool.
    broker = InMemoryMessageBroker()
    await broker.start()

    pool_data: dict[str, Any] = {}
    pools: dict[str, Any] = {}
    workspace_transcript_store = None
    if persistence is not None:
        from bot.persistence.transcript import build_database_transcript_store

        workspace_transcript_store = await build_database_transcript_store(persistence.connection)
    kb_provider: KbProvider | None = None
    if persistence is not None:
        from bot.kb.builder import build_default_kb_provider

        kb_provider = await build_default_kb_provider(persistence.connection)
    # Per-workspace session→pool attribution index: each pool registers its
    # session-tree stores into it at create_pool time; it is released with
    # the resources bundle on eviction.
    session_pool_index = SessionPoolIndex()
    resources = PoolWorkspaceResources(
        target=ctx.target,
        ctx=ctx,
        overflow_store=overflow_store,
        session_index_store=session_index_store,
        broker=broker,
        pool_data=pool_data,
        pools=pools,
        pool_router=None,
        background=None,
        persistence=persistence,
        owns_persistence=owns_persistence,
        transcript_store=service._transcript_store,
        workspace_transcript_store=workspace_transcript_store,
        kb_provider=kb_provider,
        session_pool_index=session_pool_index,
    )
    state.resources = resources
    # 3. Per-workspace interceptor chain, rooted at THIS workspace's overflow dir.
    shared_interceptor_chain = build_tool_overflow_interceptor_chain(
        overflow_store,
        control_channel=service.control_channel,
    )

    # Shared (service-level) infra reused across this workspace's pools.
    shared_hooks = [
        CurrentTimeInjectionHook(),  # StartNodeTurnHook
        KnowledgeHook(),  # BeforeTurnHook + AfterTurnHook — independent, no renewal
    ]
    shared_hook_runner = _build_hook_runner(shared_hooks)
    im_ui = IMUserInterface(
        output_adapter=service.output_adapter,
    )
    retention_cfg = app_config.multi_agent.session_retention
    retention = SessionRetentionPolicy(
        max_sessions_per_subagent=retention_cfg.max_sessions_per_subagent,
        max_sessions_global=retention_cfg.max_sessions_global,
        ttl_seconds=retention_cfg.ttl_seconds,
        cleanup_interval_seconds=retention_cfg.cleanup_interval_seconds,
    )
    command_processor = service.command_processor or _build_main_command_processor()

    # 4. Per-pool data snapshots.
    for name in pool_names:
        pool_data[name] = await build_pool_data(
            ctx,
            name,
            declared_builds[name].pool.root_agent,
            service._default_provider,
            assembly_deps[name],
            await resolve_declared_root_prompt(
                declared_builds[name],
                service._project_dir,
                service._component_registry,
            ),
            app_config=app_config,
            persistence=persistence,
        )

    # 5. Pools — reproduce the OLD create_pool kwargs verbatim EXCEPT
    #    drop workspace_manager; add pool_data + workspace_handle; broker/
    #    inbox/bus come from THIS workspace. The resolver cell is filled with
    #    R after assembly so per-turn pool_data resolution lands back here.
    #    Every pool boots from the scope declaration (ticket 11).
    resolver_cell = WorkspaceResolverCell()
    for name in pool_names:
        pools[name] = await create_pool(
            pool_name=name,
            declared=declared_builds[name],
            assembly_deps=assembly_deps[name],
            project_dir=service._project_dir,
            data_dir=ctx.paths.root,
            broker=broker,
            output_adapter=service.output_adapter,
            safety=service.safety_policy,
            retention=retention,
            im_ui=im_ui,
            shared_hooks=shared_hooks,
            shared_hook_runner=shared_hook_runner,
            shared_interceptor_chain=shared_interceptor_chain,
            control_channel=service.control_channel,
            command_processor=command_processor,
            pool_data=pool_data[name],
            workspace_handle=WorkspaceHandle(target=ctx.target, data_root=ctx.paths.root),
            workspace_resolver=resolver_cell,
            media_store_resolver=(
                functools.partial(service._media_store.store_for, name)
                if service._media_store is not None
                else None
            ),
            emitter_factory=service.emitter_factory,
            output_adapter_factory=service._output_adapter_factory,
            on_subagent_created=service._on_subagent_created,
            session_registry=session_registry,
            session_store=session_index_store,
            transcript_store=service._transcript_store,
            bot_model_config=service._bot_model_config,
            model_choice_registry=service._model_choice_registry
            if service._model_choice_registry is not None
            else ModelChoiceRegistry(),
            mcp_registry=service._mcp_registry,
            persistence=persistence,
            app_config=app_config,
            kb_provider=resources.kb_provider,
            strategy_registry=service._strategy_registry,
            session_pool_index=session_pool_index,
            workspace_registry=service.workspace_stack.registry
            if service.workspace_stack is not None
            else None,
            workspace_resources=resources,
            component_registry=service._component_registry,
            workspace_spec=scope_boot.spec.workspace,
        )

    # Phase 2: cross-pool peer wiring. Must run after all Phase 1 pools are
    # built so subagent targets precede peer targets in each store's
    # insertion order. Ticket 13: peer targets resolve through the FW
    # resolution service; the links arrive over the scope path (the single
    # link source since ticket 11 deleted the legacy pool.yml feeding).
    resolve_peer_targets(resources.pools, peer_links_from_declaration(scope_boot.spec))

    # Wire each pool's main pipeline + communication service to THIS workspace
    # (R). Subagent pipelines pick up R via the resolver cell through the
    # factory wrap.
    resolver_cell.set(resources)
    # Task 7: each pool now owns its per-poll InboxPoller (constructed + started
    # inside create_pool), so the workspace-level shared-bus signal fan-out is
    # superseded. The Drainer + idle poller (still spawned per pool until Task
    # 8 disables them) operate on each pool's own bus.
    for pi in pools.values():
        # Start this pool's output broker bridge so agent output published to
        # THIS workspace's broker reaches the output adapter. This MUST happen
        # at materialization for EVERY workspace — home and non-home alike —
        # otherwise a switched-to / newly-created workspace's turns run but
        # their output never leaves the broker (the agent looks silent).
        await pi.broker_bridge.start()

    # 8. Graph orchestrator -- static graph scheduling subsystem.
    #    The graph uses its OWN sync sqlite3.Connection pointing to the same
    #    state.db file as the workspace async aiosqlite connection. Graph
    #    stores manage their own schema on this connection, separate from the
    #    workspace's message/kv/cursor/archive tables.
    import sqlite3 as _sqlite3

    from bot.graph.agent_node_factory import BotAgentNodeFactory
    from bot.graph.output_adapter import WebUIGraphOutputAdapter
    from bot.graph.spec_loader import GraphSpecLoader
    from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
    from modex_agent.plugins.assembly.graph_schema import build_state_schema_compiler
    from modex_graph import (
        DefaultGraphState,
        DelayNodeFactory,
        FunctionNodeFactory,
        GraphOutput,
        HumanInputNodeFactory,
        NodeRegistry,
        SqliteGraphInstanceStore,
        SqliteGraphIORecordStore,
        SqliteGraphSpecStore,
    )

    node_registry = NodeRegistry()
    node_registry.register("agent", BotAgentNodeFactory(resolver_cell))
    node_registry.register("function", FunctionNodeFactory())
    node_registry.register("delay", DelayNodeFactory())
    node_registry.register("human_input", HumanInputNodeFactory())
    state_classes = {"default": DefaultGraphState}

    graph_conn = _sqlite3.connect(str(ctx.paths.state_db), check_same_thread=False)
    graph_spec_store = SqliteGraphSpecStore(graph_conn)
    graph_instance_store = SqliteGraphInstanceStore(graph_conn)
    graph_io_store = SqliteGraphIORecordStore(graph_conn)
    coordinator_factory = SqliteCoordinatorFactory(connection=graph_conn)

    graph_event_store: dict[int, list[GraphOutput]] = {}
    graph_event_subscribers: dict[int, list[asyncio.Queue[GraphOutput]]] = {}
    output_adapter = WebUIGraphOutputAdapter(graph_event_store, graph_event_subscribers)

    graph_orchestrator = GraphOrchestrator(
        node_registry=node_registry,
        state_classes=state_classes,
        spec_store=graph_spec_store,
        instance_store=graph_instance_store,
        coordinator_factory=coordinator_factory,
        output_adapter=output_adapter,
        io_store=graph_io_store,
        # Lets declarative state_schema specs resolve plugin data-namespace types.
        state_schema_compiler=build_state_schema_compiler(service._component_registry),
    )

    # Per-workspace config/graphs: first materialize copies from the global
    # template; subsequent boots load the workspace-local copy so PUT edits
    # survive restart. ctx.target (== resources.target) converges with
    # handle_put_spec; ctx.paths.root would be the .modex data subdir instead.
    if not workspace_graphs_dir.exists() and global_graphs_dir.exists():
        shutil.copytree(global_graphs_dir, workspace_graphs_dir)
    if workspace_graphs_dir.exists():
        GraphSpecLoader(graph_spec_store, compiler=graph_orchestrator._compiler).load_from_dir(
            workspace_graphs_dir
        )

    resources.graph_orchestrator = graph_orchestrator
    resources.graph_output_adapter = output_adapter
    resources.graph_event_store = graph_event_store
    resources.graph_event_subscribers = graph_event_subscribers
    resources.graph_conn = graph_conn

    default_pool = service._default_pool_name
    if default_pool is None:
        # No nominated default — derive from the runtime pools dict (first
        # pool, or None when zero pools exist). The zero-pool case is
        # expected (the user hasn't created any pool yet); PoolRouter and
        # ResolvePoolStage guard it downstream, so stay silent.
        default_pool = next(iter(pools), None)
    elif default_pool not in pools:
        fallback = next(iter(pools), default_pool)
        if fallback != default_pool:
            logger.warning(
                "[pool-routing] nominated default pool %r not found; falling back to %r (pools=%s)",
                default_pool,
                fallback,
                list(pools),
            )
            default_pool = fallback

    # 6. Background tasks (dream/curator) — per workspace.
    background = BackgroundTaskRunner(
        pool_data=pool_data,
        assembly_deps=assembly_deps,
        default_pool_name=default_pool,
    )
    await background.start()
    resources.background = background

    # 7. PoolRouter rooted at this workspace, but its session→pool mapping
    #    store is shared service-wide so mappings survive workspace switches.
    #    The store MUST be the service-level singleton — a per-workspace store
    #    here silently loses session→pool mappings on every workspace switch
    #    (the bug that sent coding-pool memory to memory/main/). If the service
    #    store is None we keep running (test/materialize paths that skip
    #    BotService.initialize) but log loudly so a misconfigured production
    #    startup is immediately diagnosable.
    session_store = service._pool_session_store
    if session_store is None:
        logger.warning(
            "[pool-routing] service._pool_session_store is None — workspace %s "
            "is getting a PER-WORKSPACE PoolSessionStore at %s. Session→pool "
            "mappings written here will NOT be visible to other workspaces' "
            "PoolRouters, so pool routing will default to '%s' on switches. "
            "This is expected only in tests; production must run "
            "BotService.initialize() first.",
            ctx.target,
            ctx.paths.root / "pool_sessions",
            service._default_pool_name,
        )
        session_store = build_pool_routing_store(
            app_config,
            persistence,
            data_dir=ctx.paths.root,
            db_path=ctx.paths.root / "state.db",
        )
        resources.owned_pool_routing_store = session_store
    resources.pool_router = PoolRouter(
        input_adapter=service.input_adapter,
        broker=broker,
        pools=pools,
        session_store=session_store,
        default_pool=default_pool,
        agent_pool_ownership=agent_pool_ownership(scope_boot.spec),
    )

    return resources


async def _stop_resources(resources: PoolWorkspaceResources) -> None:
    """Tear down one workspace's resources (re-home of _on_workspace_deactivate).

    Stop order: background tasks → terminals → pools (MCP release + shutdown +
    broker bridges) → broker → per-pool trace stores (bounded OTLP flush) →
    graph orchestrator → graph connection. The workspace DB closes LAST
    (after all DB-writing producers have stopped and final flushes complete)
    so no write races a closing connection.
    """
    pools_ok = False
    try:
        await _stop_pools(resources)
        pools_ok = True
    finally:
        try:
            if resources.graph_orchestrator is not None:
                try:
                    await resources.graph_orchestrator.pause_all_active()
                finally:
                    await resources.graph_orchestrator.cleanup()
        finally:
            try:
                if resources.graph_conn is not None:
                    resources.graph_conn.close()
            finally:
                if resources.persistence is not None and resources.owns_persistence and pools_ok:
                    await resources.persistence.close()


async def _stop_pools(resources: PoolWorkspaceResources) -> None:
    if resources.background is not None:
        with contextlib.suppress(BaseException):
            await resources.background.stop()
    tasks: list[asyncio.Task[None]] = []
    for pi in resources.pools.values():
        mgr = pi.terminal_manager
        if mgr is not None:
            for term_name in list(mgr.list_names()):
                tasks.append(asyncio.create_task(_close_terminal(mgr, term_name)))
        # Fallback persistent bash (no terminal manager): the registered
        # "bash" tool IS the shell owner — close it so the PTY child is
        # reaped at pool shutdown. Idempotent (safe with the eval roster's
        # own trial-teardown close).
        bash_tool = pi.tool_manager.get_tool("bash")
        if isinstance(bash_tool, PersistentBashTool):
            tasks.append(asyncio.create_task(_close_persistent_bash(bash_tool)))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    pools_stopped = True
    cancellation: asyncio.CancelledError | None = None
    for pi in resources.pools.values():
        if pi.mcp_manager is not None:
            with contextlib.suppress(BaseException):
                await pi.mcp_manager.release()
        try:
            pools_stopped = await pi.pool.shutdown_all() and pools_stopped
        except asyncio.CancelledError as exc:
            pools_stopped = False
            cancellation = exc
        except Exception:
            pools_stopped = False
            logger.exception("pool shutdown failed; workspace retained for retry")
        with contextlib.suppress(BaseException):
            await pi.broker_bridge.stop()
    with contextlib.suppress(BaseException):
        await resources.broker.stop()
    if cancellation is not None:
        raise cancellation
    if not pools_stopped:
        raise _PoolShutdownIncompleteError("pool shutdown incomplete")
    if resources.transcript_store is not None:
        resources.transcript_store.release_workspace(resources.ctx.paths.sessions_dir)
    for pool_data in resources.pool_data.values():
        if pool_data.trace_store is not None:
            with contextlib.suppress(BaseException):
                pool_data.trace_store.close()
    if resources.owned_pool_routing_store is not None:
        with contextlib.suppress(BaseException):
            resources.owned_pool_routing_store.close()


async def _close_terminal(mgr: TerminalManagerBase, name: str) -> None:
    try:
        await mgr.close(name)
    except BaseException:
        logger.debug("terminal close failed for %s", name, exc_info=True)


async def _close_persistent_bash(bash: PersistentBashTool) -> None:
    try:
        await bash.close()
    except BaseException:
        logger.debug("persistent bash close failed", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────
# Workspace-shared interceptor chain + legacy-road pool wiring (folded in
# from the deleted pool_wiring.py — ticket 14)
# ──────────────────────────────────────────────────────────────────────────


def build_tool_overflow_interceptor_chain(
    overflow_store: ToolOverflowStore,
    *,
    control_channel: InMemoryControlChannel | None = None,
) -> InterceptorChain:
    """Build one overflow chain, optionally including control interceptors."""
    chain = InterceptorChain()
    overflow_cleaner = OverflowCleaner(overflow_store)
    overflow_handler = ToolResultOverflowHandler(store=overflow_store, cleaner=overflow_cleaner)
    chain.add(ToolResultLimitInterceptor(overflow_handler=overflow_handler, max_chars=50_000))
    if control_channel is not None:
        chain.add(ControlDrainInterceptor(channel=control_channel))
        chain.add(LlmCancelInterceptor(channel=control_channel))
    return chain
