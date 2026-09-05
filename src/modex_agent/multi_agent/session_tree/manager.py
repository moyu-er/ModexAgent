"""SessionTreeManager — runtime coordinator for the session-tree lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.inbox.types import SessionWork
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.models import (
    MessageTrack,
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeMetadata,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.session_binding import SessionBinding
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.multi_agent.bus import LocalAgentMessageBus
    from modex_agent.multi_agent.envelope import AgentMessageEnvelope
    from modex_agent.multi_agent.inbox.types import InboxMessage
    from modex_agent.multi_agent.inbox_poller import InboxPoller
    from modex_agent.multi_agent.session_tree.session_binding import (
        SessionBindingStore,
    )
    from modex_agent.multi_agent.session_tree.store_node import TreeNodeStore
    from modex_agent.multi_agent.session_tree.store_track import MessageTrackStore
    from modex_agent.multi_agent.session_tree.store_tree import SessionTreeStore
    from modex_agent.persistence.session_registry import SessionRegistry

logger = logging.getLogger(__name__)

_PENDING_TYPES = frozenset({AgentMessageType.EXTERNAL_INPUT, AgentMessageType.AGENT_MESSAGE})
_TRACKED_TYPES = frozenset({AgentMessageType.TASK_REQUEST, AgentMessageType.AGENT_RESULT})


class SessionTreeManager:

    def __init__(
        self,
        tree_store: SessionTreeStore,
        node_store: TreeNodeStore,
        track_store: MessageTrackStore,
        bus: LocalAgentMessageBus,
        poller: InboxPoller,
        pool_name: str,
        workspace_root: str,
        session_registry: SessionRegistry,
        binding_store: SessionBindingStore | None = None,
    ) -> None:
        self._tree_store = tree_store
        self._node_store = node_store
        self._track_store = track_store
        self._bus = bus
        self._poller = poller
        self._pool_name = pool_name
        self._workspace_root = workspace_root
        self._session_registry = session_registry
        self._bus.set_session_registry(session_registry)
        self._binding_store = binding_store
        self._running: set[str] = set()
        self._pending_input: set[str] = set()
        self._quiesce_events: dict[str, asyncio.Event] = {}
        self._paused_trees: set[str] = set()

    def _quiesce_event(self, tree_id: str) -> asyncio.Event:
        return self._quiesce_events.setdefault(tree_id, asyncio.Event())

    @property
    def binding_store(self) -> SessionBindingStore | None:
        return self._binding_store

    def _signal(self, tree_id: str) -> None:
        self._quiesce_event(tree_id).set()

    async def _maybe_bind_session(self, session_id: str, envelope: AgentMessageEnvelope) -> None:
        """Auto-create a SessionBinding from envelope metadata if not already bound.

        Called on every ``deliver``. Reads ``graph_instance_id`` from
        ``envelope.metadata`` and stores it as ``task_id`` in the binding store.
        Does NOT overwrite an existing binding — ``BotAgentNode.execute``
        creates a richer binding (with graph artifacts) before calling
        ``tree.deliver``, and that binding must survive subsequent delivers
        within the same session (e.g. subagent-reply wakeups).

        Raises ``ValueError`` if an existing binding's ``task_id`` conflicts
        with the envelope's ``graph_instance_id`` — this detects concurrent
        graph instances sharing a CACHED session, which would cross-contaminate
        graph contexts.
        """
        if self._binding_store is None:
            return
        existing = self._binding_store.get(session_id) or await self._persisted_binding(session_id)
        if existing is not None:
            incoming_gid = envelope.metadata.get("graph_instance_id")
            if incoming_gid is not None and existing.task_id is not None and incoming_gid != existing.task_id:
                raise ValueError(
                    f"Session {session_id!r} is bound to task_id={existing.task_id} "
                    f"but received a deliver with graph_instance_id={incoming_gid}. "
                    f"Concurrent graph instances sharing a CACHED session are not supported."
                )
            return
        task_id = envelope.metadata.get("graph_instance_id")
        if task_id is not None:
            await self.bind_session(
                session_id,
                SessionBinding(task_id=task_id),
            )

    async def bind_session(self, session_id: str, binding: SessionBinding) -> None:
        """Persist ownership before installing live, nonserializable artifacts.

        A persisted graph binding without its live root binding is nonrunnable,
        even if the process crashed before it could persist paused admission.
        """
        node = await self._ensure_node(session_id)
        existing = await self._persisted_binding(session_id)
        if existing is not None and existing.task_id != binding.task_id:
            record = await self._tree_store.get(node.tree_id)
            if record is not None and record.status != SessionTreeStatus.COMPLETED:
                raise ValueError(f"Session {session_id!r} still belongs to task {existing.task_id}")
        if binding.is_node_execution:
            # Binding restoration is part of entry, not permission to dispatch.
            self._paused_trees.add(node.tree_id)
        await self._session_registry.register(SessionInfo.from_str(session_id).model_copy(update={
            "metadata": {SessionTreeMetadata.BINDING: binding.model_dump(
                mode="json", exclude={"graph_artifacts"},
            )},
        }))
        if self._binding_store is not None:
            self._binding_store.bind(session_id, binding)

    async def _persisted_binding(self, session_id: str) -> SessionBinding | None:
        info = await self._session_registry.get(session_id)
        data = info.metadata.get(SessionTreeMetadata.BINDING) if info is not None else None
        return SessionBinding.model_validate(data) if data is not None else None

    async def find_paused_session(self, task_id: int, graph_node_name: str) -> SessionInfo | None:
        """Find the unfinished session whose durable admission requires node reentry."""
        for record in await self._tree_store.list_active():
            if await self.can_dispatch(record.root_node_session_id):
                continue
            binding = await self._persisted_binding(record.root_node_session_id)
            if binding is not None and (
                binding.task_id == task_id and binding.graph_node_name == graph_node_name
            ):
                return await self._session_registry.get(record.root_node_session_id)
        return None

    async def pending_work(self, session_id: str) -> SessionWork:
        return await self._bus.pending_work(session_id)

    def pending_sessions(self) -> set[str]:
        """Include reserved work no longer present in the MQ's pending index."""
        return self._pending_input.copy()

    async def can_dispatch(self, session_id: str) -> bool:
        node = await self._node_store.get(session_id)
        if node is None:
            peeked = await self._bus.peek(session_id)
            node = await self._ensure_node(session_id, peeked[0] if peeked else None)
        record = await self._tree_store.get(node.tree_id)
        if record is not None and record.status == SessionTreeStatus.CANCELLED:
            return False
        owner = await self._persisted_binding(node.tree_id)
        if owner is not None and owner.task_id is not None:
            live = self._binding_store.get(node.tree_id) if self._binding_store is not None else None
            if live is None or live.task_id != owner.task_id:
                return False
            if owner.is_node_execution and live.graph_artifacts is None:
                return False
            if self._binding_store is not None and self._binding_store.get(session_id) is None:
                self._binding_store.bind(session_id, SessionBinding(task_id=owner.task_id))
        # Last check is synchronous with the poller's task admission. Pause
        # closes this gate before its first persistence await.
        return not await self._is_tree_paused(node.tree_id)

    async def pause_session(self, session_id: str) -> None:
        """Close the owning tree's admission, cancel its tasks, and drain cleanup."""
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        self._paused_trees.add(tree_id)
        await self._session_registry.register(SessionInfo.from_str(tree_id).model_copy(update={
            "metadata": {SessionTreeMetadata.PAUSED: True},
        }))
        await self._tree_store.update_status(tree_id, SessionTreeStatus.ACTIVE)
        sessions = await self._node_store.get_tree_sessions(tree_id)
        await self._poller.cancel_sessions(sessions)
        self._signal(tree_id)
        await self.wait_quiesce(tree_id)

    async def is_session_paused(self, session_id: str) -> bool:
        node = await self._node_store.get(session_id)
        return node is not None and await self._is_tree_paused(node.tree_id)

    async def _is_tree_paused(self, tree_id: str) -> bool:
        info = await self._session_registry.get(tree_id)
        return tree_id in self._paused_trees or (
            info is not None and info.metadata.get(SessionTreeMetadata.PAUSED) is True
        )

    async def resume_session(self, session_id: str) -> bool:
        """Reopen only after live root binding restoration; return whether work remains."""
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        sessions = await self._node_store.get_tree_sessions(tree_id)
        if any(sid in self._running for sid in sessions):
            raise RuntimeError("The owning tree must drain before resume")
        owner = await self._persisted_binding(tree_id)
        if owner is not None and owner.task_id is not None:
            live = self._binding_store.get(tree_id) if self._binding_store is not None else None
            if live is None or live.task_id != owner.task_id or (
                owner.is_node_execution and live.graph_artifacts is None
            ):
                raise RuntimeError("Restore the owning node's live binding before resuming its tree")
        await self.recover_tree(tree_id)
        pending = await self._has_pending_work(tree_id, sessions)
        await self._tree_store.update_status(tree_id, SessionTreeStatus.ACTIVE)
        await self._session_registry.register(SessionInfo.from_str(tree_id).model_copy(update={
            "metadata": {SessionTreeMetadata.PAUSED: False},
        }))
        self._paused_trees.discard(tree_id)
        self._poller.signal_wakeup()
        return pending

    async def unbind_session_tree(self, session_id: str) -> None:
        """Release runtime bindings after drain, retaining durable ownership."""
        node = await self._node_store.get(session_id)
        if node is not None and self._binding_store is not None:
            for sid in await self._node_store.get_tree_sessions(node.tree_id):
                self._binding_store.unbind(sid)

    async def _ensure_node(
        self, session_id: str, envelope: AgentMessageEnvelope | None = None
    ) -> TreeNodeRecord:
        existing = await self._node_store.get(session_id)
        if existing is not None:
            return existing
        parent_sid = envelope.parent_session_id if envelope is not None else None
        agent_name = envelope.target.name if envelope and envelope.target else ""
        if parent_sid is None or not agent_name:
            info = await self._session_registry.get(session_id)
            if info is not None:
                parent_sid = parent_sid or info.parent_session_id
                agent_name = agent_name or info.agent_name
        if parent_sid is not None:
            tree_id = (await self._ensure_node(parent_sid)).tree_id
        else:
            tree_id = session_id
            ts = now_ms()
            await self._tree_store.create(SessionTreeRecord(
                tree_id=tree_id, root_node_session_id=session_id, pool_name=self._pool_name,
                workspace_root=self._workspace_root, status=SessionTreeStatus.ACTIVE,
                created_at=ts, updated_at=ts,
            ))
        ts = now_ms()
        return await self._node_store.get_or_create(TreeNodeRecord(
            tree_id=tree_id, session_id=session_id, parent_session_id=parent_sid,
            agent_name=agent_name, version=0, parent_version=None,
            status=NodeVersionStatus.COMPLETED, created_at=ts, updated_at=ts,
        ))

    async def _send(self, target_session_id: str, envelope: AgentMessageEnvelope) -> bool:
        try:
            return await self._bus.send(target_session_id, envelope)
        except Exception:
            logger.exception("bus.send failed for %s", target_session_id)
            return False

    async def tree_id_for_session(self, session_id: str) -> str | None:
        """Return the tree containing ``session_id``, if one exists."""
        node = await self._node_store.get(session_id)
        return node.tree_id if node is not None else None

    async def deliver(
        self, target_session_id: str, envelope: AgentMessageEnvelope, *,
        track_consume: bool = False,
    ) -> None:
        msg_type = envelope.message_type
        node = await self._ensure_node(target_session_id, envelope)
        tree_id = node.tree_id

        await self._maybe_bind_session(target_session_id, envelope)

        if msg_type in _PENDING_TYPES:
            self._pending_input.add(target_session_id)
        tracked_external_input = track_consume and msg_type is AgentMessageType.EXTERNAL_INPUT

        if msg_type in _PENDING_TYPES and not tracked_external_input:
            # NOTE: On _send failure (dedup or error), we discard from
            # _pending_input. This is correct for error (message not in inbox)
            # but technically wrong for dedup (message IS in inbox from a prior
            # delivery). The dedup case is unlikely (requires duplicate deliver
            # call) and self-corrects on next consume/dispatch. Changing would
            # require distinguishing dedup from error in bus.send's return type.
            if not await self._send(target_session_id, envelope):
                self._pending_input.discard(target_session_id)
            self._signal(tree_id)
            return

        if msg_type not in _TRACKED_TYPES and not tracked_external_input:
            await self._send(target_session_id, envelope)
            return

        task_req: MessageTrack | None = None
        if msg_type == AgentMessageType.AGENT_RESULT and envelope.invocation_id:
            for t in await self._track_store.list_dispatched(tree_id):
                if (
                    t.message_type == AgentMessageType.TASK_REQUEST
                    and t.invocation_id == envelope.invocation_id
                ):
                    task_req = t
                    await self._track_store.update_status(
                        t.track_id, MessageTrackStatus.CONSUMED, now_ms()
                    )
                    break

        if msg_type == AgentMessageType.TASK_REQUEST:
            source_sid = envelope.parent_session_id or ""
        else:
            source_sid = task_req.target_session_id if task_req is not None else ""
        track = MessageTrack(
            track_id=envelope.message_id,
            tree_id=tree_id,
            message_id=envelope.message_id,
            message_type=AgentMessageType(msg_type),
            invocation_id=envelope.invocation_id,
            target_session_id=target_session_id,
            source_session_id=source_sid,
            status=MessageTrackStatus.DISPATCHED,
            dispatched_at=now_ms(),
        )
        await self._track_store.create(track)

        if not await self._send(target_session_id, envelope):
            await self._track_store.update_status(
                track.track_id, MessageTrackStatus.CANCELLED
            )
            if tracked_external_input:
                self._pending_input.discard(target_session_id)
            if task_req is not None:
                await self._track_store.update_status(
                    task_req.track_id, MessageTrackStatus.DISPATCHED
                )
        self._signal(tree_id)

    async def on_consumed(self, session_id: str, message: InboxMessage) -> None:
        msg_type = message.message_type
        if msg_type in _PENDING_TYPES:
            self._pending_input.discard(session_id)
            return
        if msg_type == AgentMessageType.TASK_REQUEST:
            return
        if msg_type == AgentMessageType.AGENT_RESULT:
            node = await self._ensure_node(session_id)
            tree_id = node.tree_id
            track = await self._track_store.get_by_message_id(
                tree_id, message.message_id
            )
            if track is not None:
                await self._track_store.update_status(
                    track.track_id, MessageTrackStatus.CONSUMED, now_ms()
                )
            self._signal(tree_id)

    async def on_dispatch_start(self, session_id: str) -> None:
        self._running.add(session_id)
        self._pending_input.discard(session_id)
        node = await self._ensure_node(session_id)
        await self._node_store.update_version(
            session_id,
            node.version + 1,
            node.version,
            NodeVersionStatus.RUNNING,
        )
        if node.tree_id not in self._paused_trees:
            await self._tree_store.update_status(node.tree_id, SessionTreeStatus.ACTIVE)

    async def on_dispatch_end(self, session_id: str, *, cancelled: bool = False) -> None:
        await self._track_store.close_tracks_for_session(
            session_id, MessageTrackStatus.CANCELLED if cancelled else MessageTrackStatus.CONSUMED
        )
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        await self._node_store.update_version(
            session_id,
            node.version,
            node.parent_version,
            NodeVersionStatus.CANCELLED if cancelled else NodeVersionStatus.COMPLETED,
        )
        if (await self.pending_work(session_id)).pending or await self._bus.peek(session_id):
            self._pending_input.add(session_id)
        else:
            self._pending_input.discard(session_id)
        record = await self._tree_store.get(tree_id)
        if (
            tree_id not in self._paused_trees
            and record is not None
            and record.status == SessionTreeStatus.ACTIVE
            and await self.is_quiesced(tree_id, finishing_session_id=session_id)
        ):
            await self._tree_store.update_status(tree_id, SessionTreeStatus.COMPLETED)
        self._running.discard(session_id)
        self._signal(tree_id)

    async def is_quiesced(self, tree_id: str, *, finishing_session_id: str | None = None) -> bool:
        sessions = await self._node_store.get_tree_sessions(tree_id)
        if any(s in self._running and s != finishing_session_id for s in sessions):
            return False
        if await self._is_tree_paused(tree_id):
            return True
        return not await self._has_pending_work(tree_id, sessions)

    async def _has_pending_work(self, tree_id: str, sessions: list[str]) -> bool:
        """Pending work survives pause even when the tree is quiescent for drain."""
        if await self._track_store.has_dispatched(tree_id):
            return True
        for sid in sessions:
            if (await self.pending_work(sid)).pending:
                return True
        return any(
            s in self._pending_input for s in sessions
        )

    async def wait_quiesce(self, tree_id: str) -> None:
        while True:
            event = self._quiesce_event(tree_id)
            event.clear()
            if await self.is_quiesced(tree_id):
                return
            self._poller.signal_wakeup()
            await event.wait()

    async def get_active_subtree_nodes(
        self, tree_id: str, session_id: str
    ) -> list[str]:
        """Return active session_ids in the subtree rooted at ``session_id``.

        Active = in ``_running``, in ``_pending_input``, or has a DISPATCHED
        track targeting it — the same three signals ``is_quiesced`` uses.
        Includes ``session_id`` itself. Uses ``get_tree_node_records`` (one
        query) + in-memory BFS over ``parent_session_id`` — no N+1 ``get(s)``.
        """
        records = await self._node_store.get_tree_node_records(tree_id)
        children_map: dict[str, list[str]] = {}
        for r in records:
            if r.parent_session_id is not None:
                children_map.setdefault(r.parent_session_id, []).append(r.session_id)
        descendants: set[str] = set()
        queue = [session_id]
        while queue:
            current = queue.pop(0)
            if current in descendants:
                continue
            descendants.add(current)
            queue.extend(children_map.get(current, []))
        tracks = await self._track_store.list_dispatched(tree_id)
        sessions_with_tracks = {t.target_session_id for t in tracks}
        return [
            s
            for s in descendants
            if s in self._running
            or s in self._pending_input
            or s in sessions_with_tracks
        ]

    async def on_session_evicted(self, session_id: str) -> None:
        await self._track_store.close_tracks_for_session(
            session_id, MessageTrackStatus.CANCELLED
        )
        self._running.discard(session_id)
        self._pending_input.discard(session_id)
        if self._binding_store is not None:
            self._binding_store.unbind(session_id)
        node = await self._node_store.get(session_id)
        if node is None:
            return
        if node.parent_session_id is None:
            await self._tree_store.update_status(
                node.tree_id, SessionTreeStatus.CANCELLED
            )
            self._signal(node.tree_id)

    async def recover_tree(self, tree_id: str) -> None:
        for track in await self._track_store.list_dispatched(tree_id):
            if await self._bus.contains_pending(track.target_session_id, track.message_id):
                continue
            if track.message_type == AgentMessageType.AGENT_RESULT:
                await self._track_store.update_status(track.track_id, MessageTrackStatus.CONSUMED, now_ms())
            elif track.message_type == AgentMessageType.TASK_REQUEST:
                node = await self._node_store.get(track.target_session_id)
                if node is not None and node.status == NodeVersionStatus.RUNNING:
                    await self._node_store.update_version(
                        track.target_session_id, node.version, node.parent_version, NodeVersionStatus.CANCELLED)
                await self._track_store.update_status(track.track_id, MessageTrackStatus.CONSUMED, now_ms())
        sessions = await self._node_store.get_tree_sessions(tree_id)
        for sid in sessions:
            node = await self._node_store.get(sid)
            if node is not None and node.status == NodeVersionStatus.RUNNING:
                await self._node_store.update_version(sid, node.version, node.parent_version, NodeVersionStatus.CANCELLED)
        self._pending_input -= set(sessions)
        for sid in sessions:
            if (await self.pending_work(sid)).pending:
                self._pending_input.add(sid)
            for env in await self._bus.peek(sid, limit=100):
                if env.message_type in _PENDING_TYPES:
                    self._pending_input.add(sid)
                    break
