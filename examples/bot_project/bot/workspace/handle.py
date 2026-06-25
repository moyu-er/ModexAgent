"""Business half — create_pool shim + the per-workspace resource bundle (R)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.workspace.context import WorkspaceContext
from modex_agent.core.session_store import LocalFileSessionStore
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.resources import WorkspaceResources

if TYPE_CHECKING:
    # These live in bot.service, whose package __init__ imports BotService,
    # which imports the bundle via wiring; deferring them to TYPE_CHECKING
    # keeps the import graph acyclic (handle is the low-level bundle module).
    from bot.service.pool_instance import PoolInstance
    from bot.service.pool_router import PoolRouter
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.service.workspace_store import WorkspaceScopedTranscriptStore
    from bot.workspace.background import BackgroundTaskRunner


class WorkspaceHandleRootProvider(WorkspaceRootProvider):
    """WorkspaceRootProvider reading a WorkspaceHandle.current (fixed per workspace).

    One workspace's tools share one handle (and thus one root); a workspace
    switch is a different workspace with its own handle + provider, so the
    provider reads live state without any per-switch wiring.
    """

    def __init__(self, handle: WorkspaceHandle) -> None:
        self._handle: WorkspaceHandle = handle

    def current(self) -> Path:
        return self._handle.current


from modex_agent.workspace import WorkspaceManager


class WorkspaceResolverCell(WorkspaceManager):
    """Late-binding holder for the per-workspace resource bundle (``R``).

    ``create_pool`` runs BEFORE the workspace's ``PoolWorkspaceResources`` (R)
    is assembled (R holds the pools). The agent factory wraps every agent
    creation (resident + dynamic subagent) and must set
    ``pipeline.workspace_manager = <resolver>`` so per-turn pool_data
    resolves; the communication service likewise resolves paths via
    ``resolver.resolve_workspace().pool_data``. Both read this cell lazily,
    and ``build_resources`` fills it with R once the workspace is assembled.

    Satisfies the framework pipeline's ``workspace_manager.resolve_workspace()
    .pool_data[pool]`` contract: ``resolve_workspace`` returns R, which has
    ``.pool_data``.
    """

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value: PoolWorkspaceResources | None = None

    def set(self, value: PoolWorkspaceResources) -> None:
        self._value = value

    def resolve_workspace(self) -> PoolWorkspaceResources:
        value = self._value
        if value is None:
            raise RuntimeError("Workspace resources not yet materialized")
        return value


class WorkspaceHandle:
    """Drop-in ``workspace_context`` for create_pool: a FIXED per-workspace target.

    Exposes ``.current`` (the workspace working dir, = target) and ``.data_dir``
    (the data root = target/.modex). create_pool reads these to build the
    WorkspaceRootProvider (working dir) and the experience-path fallback (data root).
    """

    __slots__ = ("_target", "_data_root")

    def __init__(self, *, target: Path, data_root: Path) -> None:
        self._target: Path = Path(target).resolve()
        self._data_root: Path = Path(data_root)

    @property
    def current(self) -> Path:
        return self._target

    @property
    def data_dir(self) -> Path:
        return self._data_root


@dataclass
class PoolWorkspaceResources(WorkspaceResources):
    """One workspace's full resource bundle (the business ``R``).

    Owns the workspace-level stores, the PER-WORKSPACE broker/inbox/bus (not
    shared — each workspace consumes only its own inbox), the per-pool data
    snapshots, the pool instances + router, and (added in a later task) the
    background tasks. It is also the per-workspace resolver: the framework
    pipeline reads ``workspace_manager.resolve_workspace().pool_data[pool]``.
    """

    target: Path
    ctx: WorkspaceContext
    inbox_server: LocalFileInboxServer
    overflow_store: LocalFileToolOverflowStore
    session_index_store: WorkspacePoolSessionStore
    broker: InMemoryMessageBroker
    inbox_producer: InboxProducer
    inbox_consumer: InboxConsumer
    agent_bus: LocalAgentMessageBus
    pool_data: dict[str, PoolDataSnapshot] = field(default_factory=dict)
    pools: dict[str, PoolInstance] = field(default_factory=dict)
    pool_router: PoolRouter | None = None
    background: BackgroundTaskRunner | None = None

    def resolve_workspace(self) -> PoolWorkspaceResources:
        """Resolver entry point the framework pipeline calls.

        Returns self: each workspace's pools are wired with ``pipeline.workspace_manager
        = <this R>`` at creation, so per-turn resolution always lands back here.
        """
        return self
