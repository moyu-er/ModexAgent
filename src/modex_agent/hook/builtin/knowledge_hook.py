"""Unified KnowledgeHook — counter reset, summary injection, retry enforcement."""

from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.core.types import MessageRole
from modex_agent.hook.abc import AfterTurnHook, BeforeTurnHook
from modex_agent.runtime.enums import TurnCustomKey

_FINDINGS_TAIL_CHARS = 800
_OPEN_QUESTIONS_TAIL_CHARS = 400


class KnowledgeHook(BeforeTurnHook, AfterTurnHook):
    """Graph knowledge base lifecycle hook.

    Combines three responsibilities in one stateless hook (Rule 1):

    before_turn (each turn attempt, including continuations):
      1. Reset GRAPH_KNOWLEDGE_READ_COUNT and WRITE_COUNT to 0.
         Each attempt independently tracks knowledge usage — other nodes
         may have written new content between attempts.
      2. Inject a truncated tail of findings.md and open_questions.md as a
         <knowledge_base> system-reminder. Using BeforeTurnHook (not
         StartNodeTurnHook) ensures continuations get refreshed summaries.

    after_turn (each turn attempt):
      3. If per-node config requires read/write but the corresponding counter
         is zero, set CONTINUATION_REQUEST and inject a reminder. Coordinates
         with DeliverRetryHook via the shared CONTINUATION_REQUEST flag —
         register KnowledgeHook AFTER DeliverRetryHook so the hard deliver
         requirement takes precedence.

    All three responsibilities early-return when ctx.graph_context is None,
    guaranteeing zero impact on normal (non-graph) sessions.

    Per-node config is read from state.custom:
    - GRAPH_KNOWLEDGE_DIR (str): knowledge directory path, set by BotAgentNode
    - GRAPH_KNOWLEDGE_REQUIRE_READ (bool, default False)
    - GRAPH_KNOWLEDGE_REQUIRE_WRITE (bool, default False)
    """

    @property
    def name(self) -> str:
        return "knowledge"

    # -- BeforeTurnHook: reset + inject ----------------------------------

    async def before_turn(self, ctx: AgentContext) -> None:
        if ctx.graph_context is None:
            return
        state = get_react_state(ctx)
        if state is None:
            return

        state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT] = 0
        state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT] = 0

        knowledge_dir = self._resolve_knowledge_dir(state)
        if knowledge_dir is None:
            return

        findings = self._read_tail(knowledge_dir / "findings.md", _FINDINGS_TAIL_CHARS)
        open_questions = self._read_tail(
            knowledge_dir / "open_questions.md", _OPEN_QUESTIONS_TAIL_CHARS
        )

        if not findings and not open_questions:
            return

        parts: list[str] = ["<knowledge_base>"]
        if findings:
            parts.append("Recent findings from other nodes:")
            parts.append(findings)
        if open_questions:
            if findings:
                parts.append("")
            parts.append("Open questions:")
            parts.append(open_questions)
        parts.append("</knowledge_base>")

        reminder = wrap_system_reminder("\n".join(parts))
        await ctx.history.append(
            {"role": str(MessageRole.SYSTEM_REMINDER), "content": reminder}
        )

    # -- AfterTurnHook: retry enforcement --------------------------------

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if ctx.graph_context is None:
            return
        if result.stop_reason in (StopReason.TURN_CANCELLED, StopReason.ERROR):
            return

        state = get_react_state(ctx)
        if state is None:
            return

        require_read = state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ, False)
        require_write = state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE, False)

        if not require_read and not require_write:
            return

        read_count = state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_READ_COUNT, 0)
        write_count = state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_WRITE_COUNT, 0)

        missing: list[str] = []
        if require_read and read_count == 0:
            missing.append("read")
        if require_write and write_count == 0:
            missing.append("write")

        if not missing:
            return

        reminder = (
            "You ended without using the knowledge base tool. "
            f"Your node configuration requires knowledge {missing[0]}"
            + (f" and {missing[1]}" if len(missing) > 1 else "")
            + ". Use the `knowledge_base` tool with the appropriate action "
            "before finishing your turn."
        )
        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(reminder),
            }
        )

        max_turns = state.custom.get(TurnCustomKey.MAX_TURNS, 3)
        if state.turn_attempt < max_turns:
            state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True

    # -- Helpers ---------------------------------------------------------

    @staticmethod
    def _resolve_knowledge_dir(state: object) -> Path | None:
        custom = getattr(state, "custom", None)
        if custom is None:
            return None
        dir_str = custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_DIR)
        if dir_str is None:
            return None
        return Path(str(dir_str))

    @staticmethod
    def _read_tail(path: Path, max_chars: int) -> str:
        if not path.exists():
            return ""
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        if len(content) <= max_chars:
            return content
        tail = content[-max_chars:]
        nl = tail.find("\n")
        if nl != -1:
            tail = tail[nl + 1 :]
        return f"[truncated - use knowledge_base action='read' for full content]\n{tail}"


__all__ = ["KnowledgeHook"]
