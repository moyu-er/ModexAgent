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
from typing import Any

from bot.service.pool_builder import create_pool
from bot.service.pool_router import PoolRouter, PoolSessionStore
from bot.service.session_store import WorkspacePoolSessionStore
from bot.workspace.background import BackgroundTaskRunner
from bot.workspace.dispatch import WorkspaceMessageDispatcher
from bot.workspace.factory import PoolResourceFactory
from bot.workspace.handle import (
    PoolWorkspaceResources,
    WorkspaceHandle,
    WorkspaceResolverCell,
)
from bot.workspace.pool_data import build_pool_data
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.store import GlobalWorkspaceStore
from modex_agent.approval.ui import IMUserInterface
from modex_agent.commands.models import CommandContext
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.types import InputMessage
from modex_agent.interceptor.builtin import ToolResultLimitInterceptor
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.comm_tracker import CommunicationTracker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore

logger = logging.getLogger(__name__)


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


def build_single_workspace_stack(service: Any, *, data_dir_name: str) -> WorkspaceStack:
    """Wire a single-home (workspace disabled) stack against ``service``.

    Uses a WorkspaceController that rejects /cd /exit.
    """
    return build_workspace_stack(service, data_dir_name=data_dir_name, enabled=False)


def build_workspace_stack(
    service: Any, *, data_dir_name: str, enabled: bool = True
) -> WorkspaceStack:
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
    store = GlobalWorkspaceStore(
        home=service._project_dir, data_dir_name=data_dir_name
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
    )


def _message_workspace_of(message: InputMessage) -> Path:
    """Workspace path carried on the message (filled by ResolveWorkspaceStage)."""
    return message.workspace


def _command_session_id_of(context: CommandContext) -> str:
    """Conversation id from a CommandContext (slash-command path)."""
    return session_id_prefix_of(context.session_id)


def _build_dispatcher(service: Any, resolver: WorkspaceResolver) -> Any:
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


async def _build_resources(
    service: Any, ctx: WorkspaceContext
) -> PoolWorkspaceResources:
    """Build one workspace's full resource bundle (the business ``R``).

    Re-homes FAITHFULLY the per-workspace construction the old
    ``_on_workspace_activate`` + ``_initialize_pool`` did. Uses
    PER-WORKSPACE broker/inbox/bus/interceptor rooted at ``ctx.paths``.
    """
    app_config = service._app_config
    pool_configs = app_config.pools

    # Main-agent memory is a baked default (bot.config.memory_defaults.
    # main_agent_memory) — never persisted in pool.yml, never user-editable.
    # Resolve it once here so BOTH build_pool_data (which constructs the memory
    # system via create_memory) and create_pool (which seeds long-term defaults)
    # see a non-None memory block. This is the single source of truth.
    from bot.config.memory_defaults import main_agent_memory

    pool_configs = {
        name: (
            cfg
            if cfg.memory is not None
            else cfg.model_copy(update={"memory": main_agent_memory()})
        )
        for name, cfg in pool_configs.items()
    }

    # 1. Workspace-level stores.
    ctx.paths.mkdir_skeleton()
    overflow_store = LocalFileToolOverflowStore(
        workspace=ctx.paths.overflow_dir, max_chunk_size=10_000
    )
    session_index_store = WorkspacePoolSessionStore(
        base_dir=ctx.paths.session_index_dir,
        pool_resolver=lambda session: service._pool_for_agent(session.agent_name),
        data_dir_name=app_config.paths.data_dir_name,
    )
    from modex_agent.core.session_registry import InMemorySessionRegistry

    session_registry = InMemorySessionRegistry(store=session_index_store)

    # 2. Per-workspace broker (cross-process wakeup). The inbox/bus are now
    #    per-pool (Task 7) — built inside create_pool, one set per pool.
    broker = InMemoryMessageBroker()
    await broker.start()

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
    pool_data: dict[str, Any] = {}
    for name, cfg in pool_configs.items():
        pool_data[name] = await build_pool_data(
            ctx,
            name,
            cfg,
            service._default_provider,
            lambda pc: pc.memory,
            service._system_prompt_for(name),
        )

    # 5. Pools — reproduce the OLD create_pool kwargs verbatim EXCEPT
    #    drop workspace_manager; add pool_data + workspace_handle; broker/
    #    inbox/bus come from THIS workspace. The resolver cell is filled with
    #    R after assembly so per-turn pool_data resolution lands back here.
    pools: dict[str, Any] = {}
    resolver_cell = WorkspaceResolverCell()
    for name, cfg in pool_configs.items():
        pools[name] = await create_pool(
            pool_name=name,
            pool_cfg=cfg,
            project_dir=service._project_dir,
            data_dir=ctx.paths.root,
            broker=broker,
            output_adapter=service.output_adapter,
            safety=service.safety_policy,
            retention=retention,
            comm_tracker=CommunicationTracker(),
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
    )

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
        _wire_pool_to_resources(pi, name, pool_configs[name], resources)

    # 6. Background tasks (dream/curator) — per workspace.
    background = BackgroundTaskRunner(
        pool_data=pool_data,
        pools_config=pool_configs,
        default_pool_name=service._default_pool_name,
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
        session_store = PoolSessionStore(data_dir=ctx.paths.root)
    resources.pool_router = PoolRouter(
        input_adapter=service.input_adapter,
        broker=broker,
        pools=pools,
        session_store=session_store,
        default_pool=service._default_pool_name,
    )

    return resources


def _build_workspace_interceptor_chain(
    service: Any, overflow_store: LocalFileToolOverflowStore
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
    pool_instance: Any,
    name: str,
    pool_cfg: Any,
    resources: PoolWorkspaceResources,
) -> None:
    """Wire one pool's main pipeline + experience hook to the workspace R.

    Re-homes the body of the old ``_wire_pool_to_workspace`` +
    ``_wire_experience_hook``: set ``pipeline.workspace_manager`` is already
    done by the factory wrap (via the resolver cell); here we register the
    experience review hook from this workspace's pool_data when enabled.
    """
    main_inst = pool_instance.pool._agents.get(pool_instance.main_agent_name)
    pipeline = main_inst.pipeline if main_inst is not None else None
    if pipeline is None:
        return

    main_cfg = next(
        (a for a in pool_cfg.agents if a.role == "main"), None
    )
    if main_cfg is None:
        return
    # Experience review is always enabled for main agents (baked). Params come
    # from ExperienceConfig defaults when the agent's block is absent.
    from modex_agent.ioc.configs.agent import ExperienceConfig

    exp_cfg = getattr(main_cfg, "experience", None) or ExperienceConfig()

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
    """Tear down one workspace's resources (re-home of _on_workspace_deactivate)."""
    if resources.background is not None:
        with contextlib.suppress(BaseException):
            await resources.background.stop()
    # Close terminals across this workspace's pools.
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
    # Shut down pools + broker bridges + broker.
    for pi in resources.pools.values():
        if pi.mcp_manager is not None:
            with contextlib.suppress(BaseException):
                await pi.mcp_manager.disconnect_all()
        with contextlib.suppress(BaseException):
            await pi.pool.shutdown_all()
        with contextlib.suppress(BaseException):
            await pi.broker_bridge.stop()
    with contextlib.suppress(BaseException):
        await resources.broker.stop()


async def _close_terminal(mgr: Any, name: str) -> None:
    try:
        await mgr.close(name)
    except BaseException:
        logger.debug("terminal close failed for %s", name, exc_info=True)


__all__ = [
    "WorkspaceStack",
    "build_workspace_stack",
    "build_single_workspace_stack",
]
