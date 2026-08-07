"""``OpenCodeSessionState`` — shared session-state registry for OpenCode SSE events.

Per-workdir singleton挂在 SSE reader 上, 维护 ``sid → SessionNode`` 状态表与
parentID 动态树关系. 纯内存 + ``Event.set()`` 唤醒, 零轮询, 零阻塞.

职责 (design 5.2):
  1. 维护 ``sid → SessionNode`` 状态表与 parentID 树
  2. SSE reader 每条原始事件喂进来 (``on_event``)
  3. 唤醒「子树覆盖该 sid」的活跃 ``TurnCompletionWaiter``
  4. REST 权威重建 (``rebuild_subtree``) — 断连重连后, fetch 失败不当空树

资源: 1 个 dict + 1 个 waiter set. 与活跃会话数线性, LRU 可清理.

关键不变量 (design 5.2/8.2):
  - ``on_event`` 同步方法 (纯 dict 操作 + ``touch``), 在 reader 协程里直接调用.
  - waiter 被唤醒后自检状态机 (registry 不判定完成, 只通知).
  - 非本回合会话事件被忽略 —— 不污染判定.
  - 断连时 ``mark_reconnect_pending`` 必须 touch 所有活跃 waiter.
  - LRU 清理排除活跃 waiter 子树 —— 卡住的 subagent 不可被清.
  - ``all_idle`` 语义: ``activity ∈ {IDLE, ERROR}`` 均算 idle; ERROR 是收敛态.
  - fetch 失败不得伪装成空树 (``null ≠ {}``).
"""

from __future__ import annotations

import logging
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .v2_client import OpencodeV2Client, OpencodeV2Error

if TYPE_CHECKING:
    from .turn_waiter import TurnCompletionWaiter

logger = logging.getLogger(__name__)

__all__ = [
    "SessionActivity",
    "SessionNode",
    "TurnState",
    "OpenCodeSessionState",
]


class SessionActivity(StrEnum):
    """Per-session activity state (design 5.1).

    Maps opencode ``status.type`` values onto the three states the turn
    completion model cares about. ``ERROR`` is a converged state — a session
    that errored will not resume, so the turn may end.
    """

    BUSY = "busy"
    IDLE = "idle"
    ERROR = "error"


class TurnState(StrEnum):
    """Per-turn waiter state machine (design 5.1).

    ACTIVE → QUIESCING → COMPLETE. The registry itself does not track turn
    state; this enum is consumed by ``TurnCompletionWaiter`` (next module).
    """

    ACTIVE = "active"
    QUIESCING = "quiescing"
    COMPLETE = "complete"


class SessionNode(BaseModel):
    """One entry in the session-state registry (design 5.1).

    ``frozen=False`` because the registry mutates ``activity`` and
    ``last_event_ms`` on every status event. ``extra="forbid"`` keeps the
    shape strict — no accidental fields leak in from wire data.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    sid: str
    parent_sid: str | None = None
    activity: SessionActivity = SessionActivity.IDLE
    last_event_ms: int = Field(default_factory=lambda: time.monotonic_ns())
    discovered: bool = False


class OpenCodeSessionState:
    """Shared session-state registry, per-workdir singleton (design 5.2).

    Hangs off the SSE reader. The reader calls ``on_event`` for every raw
    event (before parser). The registry maintains the sid→node table and
    parentID tree, and touches waiters whose subtree covers the event's sid.

    The registry NEVER decides turn completion — it only notifies waiters.
    Each waiter self-checks its state machine after being touched.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, SessionNode] = {}
        self._waiters: set[TurnCompletionWaiter] = set()
        self._reconnect_pending: bool = False
        self._rebuild_pending: bool = False
        self._root_missing: set[str] = set()

    # ------------------------------------------------------------------
    # Reader 接入 (同步, 纯内存)
    # ------------------------------------------------------------------

    def on_event(
        self,
        sid: str | None,
        event_type: str,
        parent_sid: str | None = None,
        activity: SessionActivity | None = None,
    ) -> None:
        """SSE reader calls this in ``_process_event`` before parser.

        - ``session.created`` with parent in a waiter subtree → add child node.
        - ``session.status`` / ``session.idle`` / ``session.error`` → update
          node activity (idle deprecated but still arrives; error → ERROR).
        - Any tree event → touch covering waiters.
        - Non-tree session → ignore.
        """
        if sid is None:
            return
        now = time.monotonic_ns()

        # session.created with parent in a waiter subtree → add child node
        if event_type == "session.created" and parent_sid is not None:
            if self._is_in_any_subtree(parent_sid):
                self._nodes[sid] = SessionNode(
                    sid=sid,
                    parent_sid=parent_sid,
                    activity=SessionActivity.IDLE,
                    last_event_ms=now,
                )
                self._touch_waiters_covering(sid)
            # parent not in tree → ignore (non-tree session)
            return

        # session.status / session.idle / session.error → update node activity
        if event_type in ("session.status", "session.idle", "session.error"):
            if sid not in self._nodes:
                # Root node is created on first status event (not session.created)
                # — prevents "prompt → immediate idle" false completion race.
                if not self._is_waiter_root(sid):
                    return  # non-tree session → ignore
                self._nodes[sid] = SessionNode(
                    sid=sid,
                    parent_sid=None,
                    activity=activity if activity is not None else SessionActivity.IDLE,
                    last_event_ms=now,
                )
            else:
                node = self._nodes[sid]
                if activity is not None:
                    node.activity = activity
                node.last_event_ms = now
            self._touch_waiters_covering(sid)
            return

        # Any other tree event (message.part.*, session.next.*) → touch if in tree
        if sid in self._nodes:
            self._nodes[sid].last_event_ms = now
            self._touch_waiters_covering(sid)
        # Non-tree session → ignore

    # ------------------------------------------------------------------
    # Waiter lifecycle (per-turn)
    # ------------------------------------------------------------------

    def register_waiter(self, waiter: TurnCompletionWaiter) -> None:
        """Register a per-turn waiter. Called before ``prompt_async``."""
        self._waiters.add(waiter)

    def unregister_waiter(self, waiter: TurnCompletionWaiter) -> None:
        """Unregister a waiter. Called in ``execute_streaming`` finally block."""
        self._waiters.discard(waiter)

    # ------------------------------------------------------------------
    # 树查询
    # ------------------------------------------------------------------

    def subtree_ids(self, root_sid: str) -> frozenset[str]:
        """BFS from root following parent_sid → children.

        Returns empty frozenset if root is not in ``_nodes``.
        """
        if root_sid not in self._nodes:
            return frozenset()
        result: set[str] = {root_sid}
        changed = True
        while changed:
            changed = False
            for sid, node in self._nodes.items():
                if sid not in result and node.parent_sid in result:
                    result.add(sid)
                    changed = True
        return frozenset(result)

    def all_idle(self, root_sid: str) -> bool:
        """True iff root exists and all subtree nodes are IDLE or ERROR.

        Only BUSY prevents idle. Empty tree (root not seen) → False
        (design 5.3 _recheck: "树为空 → 保持 ACTIVE, 继续等").
        """
        if root_sid not in self._nodes:
            return False
        for sid in self.subtree_ids(root_sid):
            if self._nodes[sid].activity is SessionActivity.BUSY:
                return False
        return True

    def last_event_ms(self, root_sid: str) -> int | None:
        """Max ``last_event_ms`` across subtree, or None if tree empty."""
        subtree = self.subtree_ids(root_sid)
        if not subtree:
            return None
        return max(self._nodes[sid].last_event_ms for sid in subtree)

    # ------------------------------------------------------------------
    # 断连标记
    # ------------------------------------------------------------------

    def mark_reconnect_pending(self) -> None:
        """Reader disconnect → touch all active waiters.

        Must touch, otherwise waiters sleep until ``max_turn_s`` timeout
        (no events arrive during disconnect → ``_wakeup`` never set).
        Touching cancels any quiesce timer → back to ACTIVE.
        """
        self._reconnect_pending = True
        for waiter in self._waiters:
            waiter.touch()

    def is_reconnect_pending(self) -> bool:
        return self._reconnect_pending

    def clear_reconnect_pending(self) -> None:
        """Called by ``rebuild_subtree`` on success."""
        self._reconnect_pending = False

    def is_rebuild_pending(self) -> bool:
        """True if the last ``rebuild_subtree`` had a fetch error."""
        return self._rebuild_pending

    # ------------------------------------------------------------------
    # LRU 清理
    # ------------------------------------------------------------------

    def lru_cleanup(self, threshold_ns: int) -> None:
        """Remove nodes older than ``threshold_ns`` from ``_nodes``.

        Excludes any sid in an active waiter's subtree — a stuck subagent
        (long quiet but not finished) must not be cleaned, otherwise
        ``all_idle`` would miss it and produce a false COMPLETE.
        """
        exclusion: set[str] = set()
        for waiter in self._waiters:
            exclusion |= self.subtree_ids(waiter.root_sid)
        to_remove = [
            sid
            for sid, node in self._nodes.items()
            if sid not in exclusion and node.last_event_ms < threshold_ns
        ]
        for sid in to_remove:
            del self._nodes[sid]

    # ------------------------------------------------------------------
    # Root session 不存在 (opencode 进程重启后)
    # ------------------------------------------------------------------

    def mark_root_missing(self, root_sid: str) -> None:
        """Mark root as gone — all covering waiters should COMPLETE (ERROR).

        Called by ``rebuild_subtree`` when root ``GET /children`` returns 404.
        Touches covering waiters so they re-check and see ``is_root_missing``.
        """
        self._root_missing.add(root_sid)
        for waiter in self._waiters:
            if waiter.root_sid == root_sid:
                waiter.touch()

    def is_root_missing(self, root_sid: str) -> bool:
        """True if ``mark_root_missing`` was called for this root."""
        return root_sid in self._root_missing

    # ------------------------------------------------------------------
    # REST 权威重建 (断连重连后)
    # ------------------------------------------------------------------

    async def rebuild_subtree(
        self, root_sid: str, client: OpencodeV2Client, directory: str
    ) -> None:
        """Recursive REST rebuild: ``GET /children`` + ``GET /session/status``.

        - Root ``GET /children`` 404 → ``mark_root_missing`` (opencode restarted).
        - Any fetch error → ``rebuild_pending = True``, NOT empty tree.
        - Success → clear ``reconnect_pending`` and ``rebuild_pending``.

        ``null ≠ {}`` — a failed fetch never becomes a fake empty/idle tree.
        """
        # Root existence check — 404 means root session is gone
        try:
            root_children = await client.get_children(root_sid, directory=directory)
        except OpencodeV2Error as exc:
            if exc.status == 404:
                self.mark_root_missing(root_sid)
                return
            self._rebuild_pending = True
            logger.warning("rebuild_subtree: root get_children failed: %s", exc)
            return
        except Exception:  # noqa: BLE001
            self._rebuild_pending = True
            logger.exception("rebuild_subtree: unexpected error for %s", root_sid)
            return

        # Root exists — recursively rebuild status + descendants
        try:
            failed = await self._rebuild_descendants(
                root_sid, None, root_children, client, directory
            )
        except Exception:  # noqa: BLE001
            failed = True
            logger.exception("rebuild_subtree: descendant rebuild failed for %s", root_sid)

        if failed:
            self._rebuild_pending = True
        else:
            self._rebuild_pending = False
            self._reconnect_pending = False

    async def _rebuild_descendants(
        self,
        sid: str,
        parent_sid: str | None,
        children: list[dict[str, Any]],
        client: OpencodeV2Client,
        directory: str,
    ) -> bool:
        """Recursively rebuild one node + its descendants.

        Returns ``True`` if any fetch failed during this subtree rebuild.
        Partial updates are kept (useful for next event-driven retry).
        """
        failed = False
        try:
            status = await client.get_session_status_v1(sid, directory=directory)
        except Exception:  # noqa: BLE001
            return True

        activity = self._activity_from_status(status)
        now = time.monotonic_ns()
        if sid in self._nodes:
            node = self._nodes[sid]
            node.activity = activity
            node.last_event_ms = now
            node.discovered = True
            if parent_sid is not None:
                node.parent_sid = parent_sid
        else:
            self._nodes[sid] = SessionNode(
                sid=sid,
                parent_sid=parent_sid,
                activity=activity,
                last_event_ms=now,
                discovered=True,
            )

        for child_info in children:
            child_id = child_info.get("id") if isinstance(child_info, dict) else None
            if not isinstance(child_id, str) or not child_id:
                continue
            try:
                grand_children = await client.get_children(child_id, directory=directory)
            except Exception:  # noqa: BLE001
                failed = True
                continue
            child_failed = await self._rebuild_descendants(
                child_id, sid, grand_children, client, directory
            )
            if child_failed:
                failed = True
        return failed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_in_any_subtree(self, sid: str) -> bool:
        """True if ``sid`` is in any registered waiter's subtree."""
        return any(sid in self.subtree_ids(waiter.root_sid) for waiter in self._waiters)

    def _is_waiter_root(self, sid: str) -> bool:
        """True if ``sid`` is a registered waiter's ``root_sid``."""
        return any(waiter.root_sid == sid for waiter in self._waiters)

    def _touch_waiters_covering(self, sid: str) -> None:
        """Touch all waiters whose subtree contains ``sid``."""
        for waiter in self._waiters:
            if sid in self.subtree_ids(waiter.root_sid):
                waiter.touch()

    @staticmethod
    def _activity_from_status(status: str) -> SessionActivity:
        """Map V1 ``status.type`` string to ``SessionActivity``."""
        if status in ("busy", "retry"):
            return SessionActivity.BUSY
        if status == "error":
            return SessionActivity.ERROR
        return SessionActivity.IDLE
