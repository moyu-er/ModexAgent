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
from typing import Any

logger = logging.getLogger(__name__)

_INJECTION_QUEUE_MAXSIZE = 50


class TurnAdmissionClosedError(RuntimeError):
    """Raised when a request tries to enter a stopped pipeline."""


class TurnSessionRegistry:
    """In-process registry of live turns, keyed by session_id."""

    def __init__(self) -> None:
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_tasks: dict[str, asyncio.Task[Any]] = {}
        self._injection_queues: dict[str, asyncio.Queue[str]] = {}
        self._turn_uuids: dict[str, str] = {}
        self._admission_lock = asyncio.Lock()
        self._admitted_tasks: dict[asyncio.Task[Any], int] = {}
        self._admission_closed = False

    async def admit(self, task: asyncio.Task[Any]) -> None:
        """Atomically admit one pipeline request unless shutdown has closed entry."""
        async with self._admission_lock:
            if self._admission_closed:
                raise TurnAdmissionClosedError("pipeline turn admission is closed")
            self._admitted_tasks[task] = self._admitted_tasks.get(task, 0) + 1

    def release(self, task: asyncio.Task[Any]) -> None:
        """Release a previously admitted request task."""
        count = self._admitted_tasks.get(task, 0)
        if count <= 1:
            self._admitted_tasks.pop(task, None)
        else:
            self._admitted_tasks[task] = count - 1

    # --- session lock ---
    def set_session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    # --- turn task tracking ---
    def get_session_task(self, session_id: str) -> asyncio.Task[Any] | None:
        return self._session_tasks.get(session_id)

    def cancel_turn(self, session_id: str) -> bool:
        """Request immediate cancellation of the running turn for *session_id*."""
        task = self._session_tasks.get(session_id)
        if task is None or task.done():
            return False
        return task.cancel()

    def register_task(self, session_id: str, task: asyncio.Task[Any]) -> None:
        if self._admission_closed and task not in self._admitted_tasks:
            raise TurnAdmissionClosedError("pipeline turn admission is closed")
        self._session_tasks[session_id] = task

    async def close_admission_and_drain(self) -> bool:
        """Close admission, cancel every owned request, and await settlement."""
        current = asyncio.current_task()
        async with self._admission_lock:
            self._admission_closed = True
            tasks = tuple(
                {
                    task
                    for task in (*self._admitted_tasks, *self._session_tasks.values())
                    if task is not current and not task.done()
                }
            )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return not any(
            not task.done()
            for task in (*self._admitted_tasks, *self._session_tasks.values())
        )

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
