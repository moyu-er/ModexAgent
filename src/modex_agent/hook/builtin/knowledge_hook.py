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
      2. Inject findings.md and open_questions.md tails as a
         <knowledge_base> system-reminder.

    after_turn (each turn attempt):
      3. If per-node config requires read/write but the counter is zero,
         set CONTINUATION_REQUEST and inject a reminder.

    Graph mode is the upper layer — only graph node main agents
    (``is_node_execution``) receive knowledge lifecycle. Subagents are
    atomic agents and never get knowledge config, even in graph mode.

    Gate: ``_has_knowledge_config(ctx)`` checks ``graph_context`` is set
    AND ``GRAPH_KNOWLEDGE_DIR`` state key exists (set only by
    ``GraphKnowledgeConfigurator`` whose gate is ``is_node_execution and
    NORMAL``). This excludes subagents who have ``graph_context`` but no
    knowledge dir key.

    Configuration matrix (see ``docs/design/session-tree/layered-config-matrix.md``):

    | Mode                  | KnowledgeHook |
    |-----------------------|---------------|
    | native main session   | no-op         |
    | native main graph     | active        |
    | native sub session    | no-op         |
    | native sub graph      | no-op (excluded) |
    | external (any)        | not registered |

    Per-node config is read from state.custom:
    - GRAPH_KNOWLEDGE_DIR (str): set by GraphKnowledgeConfigurator
    - GRAPH_KNOWLEDGE_REQUIRE_READ (bool, default False)
    - GRAPH_KNOWLEDGE_REQUIRE_WRITE (bool, default False)
    """

    @property
    def name(self) -> str:
        return "knowledge"

    # -- BeforeTurnHook: reset + inject ----------------------------------

    async def before_turn(self, ctx: AgentContext) -> None:
        if not _has_knowledge_config(ctx):
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
        if not _has_knowledge_config(ctx):
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


def _has_knowledge_config(ctx: AgentContext) -> bool:
    """True when this turn has graph knowledge config (graph node main agent only).

    Graph mode is the upper layer — only graph node main agents receive
    knowledge lifecycle. Subagents are atomic agents and never get
    ``GRAPH_KNOWLEDGE_DIR``, even in graph mode. Checking for the dir key
    (set by ``GraphKnowledgeConfigurator`` whose gate is
    ``is_node_execution and agent_kind == NORMAL``) is the most precise gate:
    it directly tests whether the configurator ran, regardless of how
    ``graph_context`` was set.
    """
    if ctx.graph_context is None:
        return False
    state = get_react_state(ctx)
    if state is None:
        return False
    return state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_DIR) is not None


__all__ = ["KnowledgeHook"]
