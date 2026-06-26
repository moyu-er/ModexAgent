"""TurnSessionRegistry — shared per-session turn state.

Owns the four in-process bookkeeping dicts (session locks, running turn tasks,
injection queues, turn UUIDs) that both the pipeline's pre-lock dispatch and
the TurnRunner's execution read and write. Centralising them here removes any
need for the TurnRunner to hold a back-reference to AgentPipeline.

Extracted from AgentPipeline.__init__ fields + query/cleanup methods.
Behaviour identical.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_INJECTION_QUEUE_MAXSIZE = 50


class TurnSessionRegistry:
    """In-process registry of live turns, keyed by session_id."""

    def __init__(self) -> None:
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_tasks: dict[str, asyncio.Task] = {}
        self._injection_queues: dict[str, asyncio.Queue[str]] = {}
        self._turn_uuids: dict[str, str] = {}

    # --- session lock ---
    def set_session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    # --- turn task tracking ---
    def get_session_task(self, session_id: str) -> asyncio.Task | None:
        return self._session_tasks.get(session_id)

    def register_task(self, session_id: str, task: asyncio.Task) -> None:
        self._session_tasks[session_id] = task

    def set_turn_uuid(self, session_id: str, turn_uuid: str) -> None:
        self._turn_uuids[session_id] = turn_uuid

    def unregister_turn(self, session_id: str) -> None:
        self._session_tasks.pop(session_id, None)
        self._turn_uuids.pop(session_id, None)

    def is_active(self, session_id: str) -> bool:
        task = self._session_tasks.get(session_id)
        return task is not None and not task.done()

    def has_active(self) -> bool:
        return any(not task.done() for task in self._session_tasks.values())

    def get_turn_uuid(self, session_id: str) -> str | None:
        if not self.is_active(session_id):
            return None
        return self._turn_uuids.get(session_id)

    # --- injection queue ---
    def get_or_create_queue(self, session_id: str) -> asyncio.Queue[str]:
        return self._injection_queues.setdefault(
            session_id, asyncio.Queue(maxsize=_INJECTION_QUEUE_MAXSIZE)
        )

    def get_queue(self, session_id: str) -> asyncio.Queue[str] | None:
        return self._injection_queues.get(session_id)

    # --- lifecycle ---
    def session_ids(self) -> list[str]:
        """Snapshot of all session ids currently holding a session lock.

        Used by pipeline shutdown to clean up every lingering session.
        """
        return list(self._session_locks.keys())

    def cleanup(self, session_id: str) -> None:
        self._session_locks.pop(session_id, None)
        self._injection_queues.pop(session_id, None)
        self._session_tasks.pop(session_id, None)
        self._turn_uuids.pop(session_id, None)
