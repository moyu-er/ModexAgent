"""TodoCompletionProbeHook — DEPRECATED.

.. deprecated::
    This hook is deprecated and no longer wired in the reference bot project.
    Do not register it in new code. The mechanism of injecting a synthetic
    ``todo_read`` ToolCall and a guidance XML block into the LLM response writes
    to the conversation history (``ctx.history``) rather than the persistent
    memory system. That violates the design principle that todo reminders should
    live in memory, not in the session transcript.

    Preferred replacement: tune the system prompt layer (e.g.
    ``TodoAwareSystemPromptProvider``) and the todo tool descriptions so the
    model is reminded of unfinished tasks before it generates a response, and
    can read the list via ``todo_read`` naturally when it chooses to. This is
    how opencode-style task discipline works without a dedicated probe hook.

Historical behavior (kept for unit tests only):

An ``AfterLLMResponseHook``. When an agent that owns the todo tools tries to end
a turn with a plain assistant message (no tool_calls) while its active task list
is non-empty, the hook appends a synthetic ``todo_read`` ToolCall (plus a
guidance XML block) to the response IN PLACE. The ReAct loop then executes
``todo_read`` through the normal ToolNode and continues the same turn — no new
turn, no inbox/poller, zero engine changes.

Stall avoidance: a single ``(fp, count)`` state machine in transient turn state
(``TurnCustomKey.TODO_PROBE``) probes each distinct active-view fingerprint at
most once; an unchanged ending is let through.
"""
from __future__ import annotations

import json
import warnings
from typing import TYPE_CHECKING
from uuid import uuid4

from modex_agent.core.constants import FinishReason
from modex_agent.core.types import ToolCall
from modex_agent.hook.abc import AfterLLMResponseHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.store import TodoItem, TodoStore
from modex_agent.tools.standard.todo_tool import ACTIVE_TODO_STATUSES

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.core.types import LLMResponse

PROBE_XML = """<system_note source="todo_completion_probe">
A todo_read tool call was appended because your active task list is not empty.
After reading the list:
- If a task is not yet finished, continue it; if it should not be done, mark it cancelled.
- If the list no longer reflects what you have already done, update it with todo_write.
- If a task can neither be completed nor cancelled (e.g. blocked on something external),
  leave it unchanged and finish your reply — an unchanged list will not be probed again this turn.
</system_note>"""


def _active_items(items: list[TodoItem]) -> list[TodoItem]:
    """Active subset (pending + in_progress), mirroring todo_tool._active_view."""
    return [it for it in items if it.status in ACTIVE_TODO_STATUSES]


def _fingerprint(active: list[TodoItem]) -> str:
    """Order-independent digest of the active view: content + status, sorted."""
    payload = sorted((it.content, it.status.value) for it in active)
    return json.dumps(payload, ensure_ascii=False)


class TodoCompletionProbeHook(AfterLLMResponseHook):
    """Append a ``todo_read`` probe when an agent ends with unfinished todos.

    .. deprecated::
        This hook is deprecated. Use system-prompt optimization and clear todo
        tool descriptions instead of injecting synthetic tool calls into the
        conversation history. See the module docstring for details.
    """

    @property
    def name(self) -> str:
        return "todo_completion_probe"

    def __init__(self, store: TodoStore, tool_manager: InMemoryToolManager) -> None:
        warnings.warn(
            "TodoCompletionProbeHook is deprecated: rely on the system prompt "
            "layer (TodoAwareSystemPromptProvider) and todo tool descriptions "
            "instead of injecting synthetic todo_read calls into the conversation.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._store = store
        self._tool_manager = tool_manager

    async def after_llm_response(
        self, ctx: AgentContext, response: LLMResponse
    ) -> None:
        # Gate 0: never probe on an LLM error — the turn is about to end as
        # LLM_ERROR before any injected tool call could run.
        if response.finish_reason == FinishReason.ERROR.value:
            return
        # Gate 1: only intervene on a plain-text ending.
        if response.tool_calls:
            return
        # Gate 2: the agent actually owns todo_read.
        if not self._tool_manager.is_registered("todo_read"):
            return
        # Gate 3: there must be an active list to probe. (ctx.session is always
        # present within a turn; ctx.runtime is nullable, so guard it.)
        if ctx.runtime is None:
            return
        active = _active_items(await self._store.get(ctx.session.session_id))
        if not active:
            return

        custom = ctx.runtime.state.custom
        fp = _fingerprint(active)
        prev = custom.get(TurnCustomKey.TODO_PROBE)
        if prev is not None and prev.get("fp") == fp:
            # Same list as the last probe — let the turn end. ``count`` is
            # observability (how many times this fingerprint reappeared); the
            # inject/skip decision is binary on ``fp``.
            prev["count"] = int(prev.get("count", 1)) + 1
            return

        # New (or changed) non-empty list: probe exactly once.
        custom[TurnCustomKey.TODO_PROBE] = {"fp": fp, "count": 1}
        response.tool_calls.append(
            ToolCall(
                tool_name="todo_read",
                call_id=f"todo-probe-{uuid4().hex[:8]}",
                arguments={},
            )
        )
        response.content = (response.content or "") + "\n" + PROBE_XML
