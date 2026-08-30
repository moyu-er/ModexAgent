"""Business half — create_pool shim + the per-workspace resource bundle (R)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.session_store import SessionStore
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace import WorkspaceManager
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.resources import WorkspaceResources

if TYPE_CHECKING:
    import asyncio
    import sqlite3

    from bot.kb.provider import KbProvider

    # These live in bot.service, whose package __init__ imports BotService,
    # which imports the bundle via wiring; deferring them to TYPE_CHECKING
    # keeps the import graph acyclic (handle is the low-level bundle module).
    from bot.service.session_pool_index import SessionPoolIndex
    from bot.service.workspace_store import WorkspaceScopedTranscriptStore
    from bot.webui.transcript_store import TranscriptStore
    from bot.workspace.background import BackgroundTaskRunner
    from modex_agent.multi_agent.pool_instance import PoolInstance
    from modex_agent.multi_agent.pool_router import PoolRouter, PoolRoutingStore
    from modex_agent.orchestration import GraphOrchestrator
    from modex_agent.persistence.managers import WorkspacePersistenceManager
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_graph import GraphOutput, GraphOutputAdapter


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

    Owns the workspace-level stores, the PER-WORKSPACE broker (cross-process
    wakeup; the inbox/bus/poller are per-pool — Task 7), the per-pool data
    snapshots, the pool instances + router, and (added in a later task) the
    background tasks. It is also the per-workspace resolver: the framework
    pipeline reads ``workspace_manager.resolve_workspace().pool_data[pool]``.
    """

    target: Path
    ctx: WorkspaceContext
    overflow_store: LocalFileToolOverflowStore
    session_index_store: SessionStore
    broker: InMemoryMessageBroker
    pool_data: dict[str, PoolDataSnapshot] = field(default_factory=dict)
    pools: dict[str, PoolInstance] = field(default_factory=dict)
    pool_router: PoolRouter | None = None
    background: BackgroundTaskRunner | None = None
    persistence: WorkspacePersistenceManager | None = None
    owns_persistence: bool = False
    owned_pool_routing_store: PoolRoutingStore | None = None
    transcript_store: WorkspaceScopedTranscriptStore | None = None
    workspace_transcript_store: TranscriptStore | None = None
    kb_provider: KbProvider | None = None
    component_registry: ComponentRegistry | None = None
    graph_orchestrator: GraphOrchestrator | None = None
    graph_output_adapter: GraphOutputAdapter | None = None
    graph_event_store: dict[int, list[GraphOutput]] | None = None
    # WS graph-event subscriptions (subscribe_graph action): instance id ->
    # subscriber queues. Assembled alongside graph_event_store; the
    # WebUIGraphOutputAdapter fans out to these queues on emit.
    graph_event_subscribers: dict[int, list[asyncio.Queue[GraphOutput]]] | None = None
    graph_conn: sqlite3.Connection | None = None
    # Released with the bundle on workspace eviction — no explicit cleanup API.
    session_pool_index: SessionPoolIndex | None = None

    def resolve_workspace(self) -> PoolWorkspaceResources:
        """Resolver entry point the framework pipeline calls.

        Returns self: each workspace's pools are wired with ``pipeline.workspace_manager
        = <this R>`` at creation, so per-turn resolution always lands back here.
        """
        return self

    @property
    def workspace_root(self) -> Path:
        """The workspace working dir — what the framework binds per turn.

        Satisfies ``WorkspaceResources.workspace_root``; identical to ``target``
        and to the ``WorkspaceHandleRootProvider`` root that scopes file/shell
        tools, so attachment resolution and tool scoping share one root.
        """
        return self.target
