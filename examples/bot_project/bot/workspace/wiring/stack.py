"""Workspace stack assembly (build_workspace_stack + dispatcher helpers)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.service.core import BotService

from bot.workspace.dispatch import WorkspaceMessageDispatcher
from bot.workspace.factory import PoolResourceFactory
from bot.workspace.handle import PoolWorkspaceResources
from modex_agent.commands.models import CommandContext
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.messaging.models import InputMessage
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.scope.compiler import CompiledAgent
from modex_agent.scope.defaults import (
    memory_config_for_position,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import ScopeRegistry, ScopeRegistryStore
from modex_agent.workspace.routing import WorkspaceResolver

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspaceStack:
    """The assembled multi-live workspace stack held by BotService.

    BotService reads ``controller`` (compat ``workspace_context``) and eagerly
    materializes home via ``registry.materialize(home_context)``.
    """

    registry: ScopeRegistry[PoolWorkspaceResources]
    resolver: WorkspaceResolver[PoolWorkspaceResources]
    controller: WorkspaceController
    dispatcher: WorkspaceMessageDispatcher
    factory: PoolResourceFactory
    store: ScopeRegistryStore


def declared_assembly_deps(
    root: CompiledAgent,
    *,
    max_context_tokens: int | None,
) -> PoolAssemblyDeps:
    """Deps for a declaration-hosted pool's compiled root (SPEC §3.2).

    The SINGLE assembly-deps road: the memory config comes from the
    position-derived defaults + the node's ``memory:`` override (the
    compiled ``MemoryOverrides`` session face) and the session threshold
    falls back to the boot-injected model window when the node declares
    none. Experience is NOT here anymore: the retired root-roster-derived
    experience config died with the experience capability's supply face —
    ``ExperienceCapability.supply`` builds the manager/dir/curator from
    the compile product's capability config (SPEC §8.3).
    """
    session_max_context_tokens = root.spec.memory_overrides.max_context_tokens
    if session_max_context_tokens is None:
        session_max_context_tokens = max_context_tokens
    return PoolAssemblyDeps(
        memory=memory_config_for_position(
            root.defaults,
            session_max_context_tokens=session_max_context_tokens,
        ),
    )


def build_workspace_stack(
    service: BotService, *, data_dir_name: str, enabled: bool = True
) -> WorkspaceStack:
    """Wire the full multi-live stack against ``service``.

    ``service`` is the BotService: read for app config, default provider/pool,
    hooks, control channel, emitter factory, and the shared input/output
    adapters. The per-workspace broker/inbox/bus/interceptor are built inside
    ``build_resources`` (one set per workspace), NOT shared here.

    ``enabled`` controls whether the WorkspaceController allows workspace
    switches (``/cd``); ``False`` gives a single-home (workspace disabled) stack.
    """
    from bot.service.builders import build_workspace_registry_store
    from bot.workspace.wiring.resources import _build_resources, _stop_resources

    async def build_resources(ctx: WorkspaceContext) -> PoolWorkspaceResources:
        return await _build_resources(service, ctx)

    async def stop_resources(resources: PoolWorkspaceResources) -> None:
        await _stop_resources(resources)

    factory = PoolResourceFactory(build_resources=build_resources, stop_resources=stop_resources)
    store = build_workspace_registry_store(
        service._app_config,
        service._registry_persistence,
        service._project_dir,
        data_dir_name,
    )
    registry: ScopeRegistry[PoolWorkspaceResources] = ScopeRegistry(
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
