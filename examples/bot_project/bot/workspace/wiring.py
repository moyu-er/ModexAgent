"""Per-workspace stack assembly + resource closures (CUTOVER).

``build_workspace_stack`` wires the generic registry/resolver/controller/
dispatcher with the business ``PoolResourceFactory`` whose ``build_resources``
/``stop_resources`` closures re-home — FAITHFULLY — the per-workspace
construction that the old single-active ``_on_workspace_activate`` +
``_initialize_pool`` did, bound to one workspace's ``WorkspaceContext`` and
using PER-WORKSPACE broker/inbox/bus/interceptor.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bot.service.core import BotService
    from modex_agent.multi_agent.pool_instance import PoolInstance
    from modex_agent.persistence.managers import WorkspacePersistenceManager

from bot.workspace.background import BackgroundTaskRunner
from bot.workspace.dispatch import WorkspaceMessageDispatcher
from bot.workspace.factory import PoolResourceFactory
from bot.workspace.handle import (
    PoolWorkspaceResources,
    WorkspaceHandle,
    WorkspaceResolverCell,
)
from bot.workspace.pool_data import build_pool_data
from modex_agent.approval.ui import IMUserInterface
from modex_agent.commands.models import CommandContext
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.types import InputMessage
from modex_agent.interceptor.builtin import ToolResultLimitInterceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.pool_config import PoolStore
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import PoolSpec
from modex_agent.multi_agent.pool_router import PoolRouter
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import WorkspaceRegistry, WorkspaceRegistryStore
from modex_agent.workspace.routing import WorkspaceResolver

logger = logging.getLogger(__name__)


class _PoolShutdownIncompleteError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceStack:
    """The assembled multi-live workspace stack held by BotService.

    BotService reads ``controller`` (compat ``workspace_context``) and eagerly
    materializes home via ``registry.materialize(home_context)``.
    """

    registry: WorkspaceRegistry[PoolWorkspaceResources]
    resolver: WorkspaceResolver[PoolWorkspaceResources]
    controller: WorkspaceController
    dispatcher: WorkspaceMessageDispatcher
    factory: PoolResourceFactory
    store: WorkspaceRegistryStore


def build_single_workspace_stack(service: BotService, *, data_dir_name: str) -> WorkspaceStack:
    """Wire a single-home (workspace disabled) stack against ``service``.

    Uses a WorkspaceController that rejects /cd /exit.
    """
    return build_workspace_stack(service, data_dir_name=data_dir_name, enabled=False)


def build_workspace_stack(
    service: BotService, *, data_dir_name: str, enabled: bool = True
) -> WorkspaceStack:
    from bot.service.builders import build_workspace_registry_store

    """Wire the full multi-live stack against ``service``.

    ``service`` is the BotService: read for app config, default provider/pool,
    hooks, control channel, emitter factory, and the shared input/output
    adapters. The per-workspace broker/inbox/bus/interceptor are built inside
    ``build_resources`` (one set per workspace), NOT shared here.

    ``enabled`` controls whether the WorkspaceController allows workspace
    switches (``/cd``); ``False`` gives a single-home (workspace disabled) stack.
    """

    async def build_resources(ctx: WorkspaceContext) -> PoolWorkspaceResources:
        return await _build_resources(service, ctx)

    async def stop_resources(resources: PoolWorkspaceResources) -> None:
        await _stop_resources(resources)

    factory = PoolResourceFactory(
        build_resources=build_resources, stop_resources=stop_resources
    )
    store = build_workspace_registry_store(
        service._app_config,
        service._registry_persistence,
        service._project_dir,
        data_dir_name,
    )
    registry: WorkspaceRegistry[PoolWorkspaceResources] = WorkspaceRegistry(
        home=service._project_dir,
        data_dir_name=data_dir_name,
        factory=factory,
        store=store,
    )
    resolver = WorkspaceResolver(registry=registry)
    controller = WorkspaceController(
        registry=registry,
        data_dir_name=data_dir_name,
        enabled=enabled,
    )
    dispatcher = _build_dispatcher(service, resolver)
    return WorkspaceStack(
        registry=registry,
        resolver=resolver,
        controller=controller,
        dispatcher=dispatcher,
        factory=factory,
        store=store,
    )


def _message_workspace_of(message: InputMessage) -> Path:
    """Workspace path carried on the message (filled by ResolveWorkspaceStage)."""
    return message.workspace


def _command_session_id_of(context: CommandContext) -> str:
    """Conversation id from a CommandContext (slash-command path)."""
    return session_id_prefix_of(context.session_id)


def _build_dispatcher(
    service: BotService,
    resolver: WorkspaceResolver,
) -> WorkspaceMessageDispatcher:
    from bot.workspace.dispatch import WorkspaceMessageDispatcher

    async def _route_one(resources: PoolWorkspaceResources, message: InputMessage) -> None:
        # Each workspace's pool_router is rooted at that workspace; route the
        # single already-received message into it.
        await resources.pool_router.route_message(message)  # type: ignore[union-attr]

    return WorkspaceMessageDispatcher(
        receive=service.input_adapter.receive,
        resolver=resolver,
        workspace_of=_message_workspace_of,
        route_one=_route_one,
    )


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


async def _build_resources(
    service: BotService, ctx: WorkspaceContext
) -> PoolWorkspaceResources:
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
    """
    from bot.service.builders import build_pool_routing_store, build_session_store
    from bot.service.pool_builder import create_pool

    app_config = service._app_config
    pool_store = PoolStore(base_dir=service._project_dir)
    pool_names = [s.name for s in pool_store.list_pools()]
    pool_specs: dict[str, PoolSpec] = {
        name: pool_store.read_pool(name) for name in pool_names
    }

    logger.info(
        "[workspace-build] target=%s mcp_registry=%s pools=%s",
        ctx.target,
        "set" if service._mcp_registry is not None else "None",
        list(pool_specs),
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

    from bot.config.memory_defaults import main_agent_memory
    from modex_agent.ioc.configs.memory import MemoryConfig

    def _main_agent_memory(max_context_tokens: int | None) -> MemoryConfig:
        cfg = main_agent_memory()
        if max_context_tokens is None:
            return cfg
        return cfg.model_copy(
            update={
                "session": cfg.session.model_copy(
                    update={"max_context_tokens": max_context_tokens}
                )
            }
        )

    max_context_tokens = (
        service._bot_model_config.max_context_tokens
        if service._bot_model_config is not None
        else None
    )
    memory = _main_agent_memory(max_context_tokens)
    assembly_deps: dict[str, PoolAssemblyDeps] = {
        name: PoolAssemblyDeps(memory=memory)
        for name in pool_names
    }

    # 1. Workspace-level stores.
    ctx.paths.mkdir_skeleton()
    overflow_store = LocalFileToolOverflowStore(
        workspace=ctx.paths.overflow_dir, max_chunk_size=10_000
    )
    session_index_store = build_session_store(
        app_config,
        persistence,
        session_index_dir=ctx.paths.session_index_dir,
        pool_resolver=lambda session: service._pool_for_agent(session.agent_name),
        data_dir_name=app_config.paths.data_dir_name,
    )
    from modex_agent.core.session_registry import InMemorySessionRegistry

    session_registry = InMemorySessionRegistry(store=session_index_store)
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

        workspace_transcript_store = await build_database_transcript_store(
            persistence.connection
        )
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
    )
    state.resources = resources
    # 3. Per-workspace interceptor chain, rooted at THIS workspace's overflow dir.
    shared_interceptor_chain = _build_workspace_interceptor_chain(
        service, overflow_store
    )

    # Shared (service-level) infra reused across this workspace's pools.
    shared_hooks = service._collect_run_hooks()
    shared_hook_runner = service._build_hook_runner(shared_hooks)
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
    command_processor = service.command_processor or service._build_main_command_processor()

    # 4. Per-pool data snapshots.
    for name in pool_names:
        pool_data[name] = await build_pool_data(
            ctx,
            name,
            pool_specs[name],
            service._default_provider,
            assembly_deps[name],
            service._system_prompt_for(name),
            app_config=app_config,
            persistence=persistence,
        )

    # 5. Pools — reproduce the OLD create_pool kwargs verbatim EXCEPT
    #    drop workspace_manager; add pool_data + workspace_handle; broker/
    #    inbox/bus come from THIS workspace. The resolver cell is filled with
    #    R after assembly so per-turn pool_data resolution lands back here.
    resolver_cell = WorkspaceResolverCell()
    for name in pool_names:
        pools[name] = await create_pool(
            pool_name=name,
            pool_spec=pool_specs[name],
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
            workspace_handle=WorkspaceHandle(
                target=ctx.target, data_root=ctx.paths.root
            ),
            workspace_resolver=resolver_cell,
            emitter_factory=service.emitter_factory,
            output_adapter_factory=service._output_adapter_factory,
            on_subagent_created=service._on_subagent_created,
            session_registry=session_registry,
            session_store=session_index_store,
            transcript_store=service._transcript_store,
            bot_model_config=service._bot_model_config,
            model_choice_registry=service._model_choice_registry,
            mcp_registry=service._mcp_registry,
            persistence=persistence,
            app_config=app_config,
        )

    # Phase 2: cross-pool peer wiring. Must run after all Phase 1 pools are built
    # so subagent targets precede peer targets in each store's insertion order.
    pool_store = PoolStore(base_dir=service.project_dir)
    for pool_name, instance in resources.pools.items():
        pool_tree = pool_store.read_pool(pool_name)
        for peer_pool_name in pool_tree.peers:
            peer_instance = resources.pools[peer_pool_name]
            peer_tree = pool_store.read_pool(peer_pool_name)
            description = (
                peer_tree.main.description
                or f"Peer pool {peer_pool_name}'s main agent"
            )
            target = CommunicationTarget(
                name=peer_instance.main_agent_name,
                kind=AgentCommKind.NORMAL,
                pool_name=peer_pool_name,
                bus_ref=peer_instance.agent_bus,
                description=description,
                execution_strategy=peer_tree.main.execution_strategy,
            )
            instance.target_store.add(target)

    # Wire each pool's main pipeline + communication service to THIS workspace
    # (R), then run the experience-hook wiring that used to live in
    # _wire_pool_to_workspace. Subagent pipelines pick up R via the resolver
    # cell through the factory wrap.
    resolver_cell.set(resources)
    # Task 7: each pool now owns its per-poll InboxPoller (constructed + started
    # inside create_pool), so the workspace-level shared-bus signal fan-out is
    # superseded. The Drainer + idle poller (still spawned per pool until Task
    # 8 disables them) operate on each pool's own bus.
    for name, pi in pools.items():
        _wire_pool_to_resources(pi, name, assembly_deps[name], resources)
        # Start this pool's output broker bridge so agent output published to
        # THIS workspace's broker reaches the output adapter. This MUST happen
        # at materialization for EVERY workspace — home and non-home alike —
        # otherwise a switched-to / newly-created workspace's turns run but
        # their output never leaves the broker (the agent looks silent).
        await pi.broker_bridge.start()

    default_pool = service._default_pool_name
    if default_pool not in pools:
        fallback = next(iter(pools), default_pool)
        if fallback != default_pool:
            logger.warning(
                "[pool-routing] nominated default pool %r not found; "
                "falling back to %r (pools=%s)",
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
    )

    return resources


def _build_workspace_interceptor_chain(
    service: BotService, overflow_store: LocalFileToolOverflowStore
) -> InterceptorChain:
    """Per-workspace interceptor chain rooted at THIS workspace's overflow dir.

    Re-homes ``BotService._build_interceptor_chain`` minus the shared-state
    caching: each workspace gets its own chain. Control-drain interceptors
    reuse the service-level control channel.
    """
    chain = InterceptorChain()
    overflow_cleaner = OverflowCleaner(overflow_store)
    overflow_handler = ToolResultOverflowHandler(
        store=overflow_store, cleaner=overflow_cleaner
    )
    chain.add(
        ToolResultLimitInterceptor(
            overflow_handler=overflow_handler, max_chars=50_000
        )
    )
    from modex_agent.hook.builtin.control_drain import (
        ControlDrainInterceptor,
        LlmCancelInterceptor,
    )

    chain.add(ControlDrainInterceptor(channel=service.control_channel))
    chain.add(LlmCancelInterceptor(channel=service.control_channel))
    return chain


def _wire_pool_to_resources(
    pool_instance: PoolInstance,
    name: str,
    deps: PoolAssemblyDeps,
    resources: PoolWorkspaceResources,
) -> None:
    """Wire one pool's main pipeline + experience hook to the workspace R."""

    main_inst = pool_instance.pool._agents.get(pool_instance.main_agent_name)
    pipeline = main_inst.pipeline if main_inst is not None else None
    if pipeline is None:
        return

    exp_cfg = deps.experience
    if exp_cfg is None or not exp_cfg.enabled:
        return

    pool_data = resources.pool_data.get(name)
    if pool_data is None:
        return

    from modex_agent.agents.experience.review_agent import ExperienceReviewAgent
    from modex_agent.hook import HookErrorPolicy, HookSpec
    from modex_agent.hook.builtin.experience_review import ExperienceReviewHook

    review_agent = ExperienceReviewAgent(
        provider=pool_instance.provider,
        max_iterations=exp_cfg.max_iterations,
    )
    hook = ExperienceReviewHook(
        review_agent=review_agent,
        experience_dir=pool_data.experience_dir,
        meta_store=pool_data.experience_meta,
        min_messages=exp_cfg.min_messages,
        exp_cooldown_turns=exp_cfg.exp_cooldown_turns,
    )
    spec = HookSpec(hook=hook, on_error=HookErrorPolicy.LOG)
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(spec)
    else:
        pipeline.hooks.append(hook)


async def _stop_resources(resources: PoolWorkspaceResources) -> None:
    """Tear down one workspace's resources (re-home of _on_workspace_deactivate).

    Stop order: background tasks → terminals → pools (MCP release + shutdown +
    broker bridges) → broker. The workspace DB closes LAST (after all
    DB-writing producers have stopped and final flushes complete) so no write
    races a closing connection.
    """
    if resources.background is not None:
        with contextlib.suppress(BaseException):
            await resources.background.stop()
    tasks: list[asyncio.Task[None]] = []
    for pi in resources.pools.values():
        mgr = pi.terminal_manager
        if mgr is None:
            continue
        for term_name in list(mgr.list_names()):
            tasks.append(
                asyncio.create_task(_close_terminal(mgr, term_name))
            )
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
    if resources.owned_pool_routing_store is not None:
        with contextlib.suppress(BaseException):
            resources.owned_pool_routing_store.close()
    if resources.persistence is not None and resources.owns_persistence:
        with contextlib.suppress(BaseException):
            await resources.persistence.close()


async def _close_terminal(mgr: TerminalManagerBase, name: str) -> None:
    try:
        await mgr.close(name)
    except BaseException:
        logger.debug("terminal close failed for %s", name, exc_info=True)


__all__ = [
    "WorkspaceStack",
    "build_workspace_stack",
    "build_single_workspace_stack",
]
