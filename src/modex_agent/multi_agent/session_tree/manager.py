"""SessionTreeManager — runtime coordinator for the session-tree lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.models import (
    MessageTrack,
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.multi_agent.bus import LocalAgentMessageBus
    from modex_agent.multi_agent.envelope import AgentMessageEnvelope
    from modex_agent.multi_agent.inbox.types import InboxMessage
    from modex_agent.multi_agent.inbox_poller import InboxPoller
    from modex_agent.multi_agent.session_tree.store_node import TreeNodeStore
    from modex_agent.multi_agent.session_tree.store_track import MessageTrackStore
    from modex_agent.multi_agent.session_tree.store_tree import SessionTreeStore

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
    ) -> None:
        self._tree_store = tree_store
        self._node_store = node_store
        self._track_store = track_store
        self._bus = bus
        self._poller = poller
        self._pool_name = pool_name
        self._workspace_root = workspace_root
        self._session_registry = session_registry
        self._running: set[str] = set()
        self._pending_input: set[str] = set()
        self._quiesce_events: dict[str, asyncio.Event] = {}

    def _quiesce_event(self, tree_id: str) -> asyncio.Event:
        return self._quiesce_events.setdefault(tree_id, asyncio.Event())

    def _signal(self, tree_id: str) -> None:
        self._quiesce_event(tree_id).set()

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

    async def deliver(self, target_session_id: str, envelope: AgentMessageEnvelope) -> None:
        msg_type = envelope.message_type
        node = await self._ensure_node(target_session_id, envelope)
        tree_id = node.tree_id

        if msg_type in _PENDING_TYPES:
            self._pending_input.add(target_session_id)
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

        if msg_type not in _TRACKED_TYPES:
            await self._send(target_session_id, envelope)
            return

        task_req: MessageTrack | None = None
        if (
            msg_type == AgentMessageType.AGENT_RESULT
            and envelope.invocation_id
        ):
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
        self._pending_input.discard(session_id)
        node = await self._ensure_node(session_id)
        await self._node_store.update_version(
            session_id,
            node.version + 1,
            node.version,
            NodeVersionStatus.RUNNING,
        )
        self._running.add(session_id)
        await self._tree_store.update_status(node.tree_id, SessionTreeStatus.ACTIVE)

    async def on_dispatch_end(self, session_id: str) -> None:
        await self._track_store.close_tracks_for_session(
            session_id, MessageTrackStatus.CONSUMED
        )
        self._running.discard(session_id)
        node = await self._ensure_node(session_id)
        tree_id = node.tree_id
        await self._node_store.update_version(
            session_id,
            node.version,
            node.parent_version,
            NodeVersionStatus.COMPLETED,
        )
        if await self.is_quiesced(tree_id):
            await self._tree_store.update_status(tree_id, SessionTreeStatus.COMPLETED)
        self._signal(tree_id)

    async def is_quiesced(self, tree_id: str) -> bool:
        if await self._track_store.has_dispatched(tree_id):
            return False
        sessions = await self._node_store.get_tree_sessions(tree_id)
        return not any(s in self._running for s in sessions) and not any(
            s in self._pending_input for s in sessions
        )

    async def wait_quiesce(self, tree_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if await self.is_quiesced(tree_id):
                return True
            event = self._quiesce_event(tree_id)
            event.clear()
            self._poller.signal_wakeup()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                return False

    async def on_session_evicted(self, session_id: str) -> None:
        await self._track_store.close_tracks_for_session(
            session_id, MessageTrackStatus.CANCELLED
        )
        self._running.discard(session_id)
        self._pending_input.discard(session_id)
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
                        track.target_session_id, node.version, node.parent_version, NodeVersionStatus.COMPLETED)
                await self._track_store.update_status(track.track_id, MessageTrackStatus.CONSUMED, now_ms())
        sessions = await self._node_store.get_tree_sessions(tree_id)
        for sid in sessions:
            node = await self._node_store.get(sid)
            if node is not None and node.status == NodeVersionStatus.RUNNING:
                await self._node_store.update_version(sid, node.version, node.parent_version, NodeVersionStatus.COMPLETED)
        self._pending_input -= set(sessions)
        for sid in sessions:
            for env in await self._bus.peek(sid, limit=100):
                if env.message_type in _PENDING_TYPES:
                    self._pending_input.add(sid)
                    break
