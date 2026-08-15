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
         set CONTINUATION_REQUEST and inject a reminder. require_read is
         exempted when the KB had no readable content at turn start
         (GRAPH_KNOWLEDGE_HAS_READABLE is False) — the agent cannot read
         what no node has written.

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

        # Always inject a <knowledge_base> system-reminder so the agent knows
        # the current state of the shared knowledge base — even when no node
        # has written anything yet. Each pattern section reflects its actual
        # state (not created / empty / has content) and guides the agent to
        # the right `knowledge_base` tool action.
        parts: list[str] = ["<knowledge_base>"]
        parts.extend(
            self._render_pattern_section(
                knowledge_dir / "findings.md",
                _FINDINGS_TAIL_CHARS,
                label="Findings",
                pattern="findings",
                create_guidance=(
                    "record discoveries, analysis results, or key facts "
                    "from your work"
                ),
            )
        )
        parts.extend(
            self._render_pattern_section(
                knowledge_dir / "open_questions.md",
                _OPEN_QUESTIONS_TAIL_CHARS,
                label="Open questions",
                pattern="open_questions",
                create_guidance="raise questions for downstream nodes to address",
            )
        )
        parts.append("</knowledge_base>")

        reminder = wrap_system_reminder("\n".join(parts))
        await ctx.history.append(
            {"role": str(MessageRole.SYSTEM_REMINDER), "content": reminder}
        )

        findings_path = knowledge_dir / "findings.md"
        open_questions_path = knowledge_dir / "open_questions.md"
        state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE] = (
            _has_non_empty_content(findings_path)
            or _has_non_empty_content(open_questions_path)
        )

    @staticmethod
    def _render_pattern_section(
        path: Path,
        max_chars: int,
        *,
        label: str,
        pattern: str,
        create_guidance: str,
    ) -> list[str]:
        """Render one pattern's state section for the knowledge_base reminder.

        Three states are distinguished so the agent receives precise guidance:

        - File does not exist (or is unreadable): "not yet created" → guide
          the agent to use ``write`` to create it.
        - File exists but content is empty/whitespace: "exists but is empty"
          → guide the agent to use ``write`` to populate it.
        - File has content: show the tail (truncated if longer than
          ``max_chars``) → guide the agent to use ``read`` for full content.

        Returns a list of lines (no trailing newline) ending with a blank
        separator line so multiple sections compose cleanly.
        """
        if not path.exists():
            return [
                f"{label}: not yet created for this graph instance.",
                f"  → Use `knowledge_base` action='write' pattern='{pattern}' to {create_guidance}.",
                "",
            ]
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return [
                f"{label}: not yet created for this graph instance.",
                f"  → Use `knowledge_base` action='write' pattern='{pattern}' to {create_guidance}.",
                "",
            ]
        if not content.strip():
            return [
                f"{label}: file exists but is empty.",
                f"  → Use `knowledge_base` action='write' pattern='{pattern}' to {create_guidance}.",
                "",
            ]
        if len(content) <= max_chars:
            return [
                f"{label} (current content):",
                content,
                f"  → Use `knowledge_base` action='read' pattern='{pattern}' for full content.",
                "",
            ]
        tail = content[-max_chars:]
        nl = tail.find("\n")
        if nl != -1:
            tail = tail[nl + 1 :]
        return [
            f"{label} (current content, tail shown):",
            f"[truncated — use `knowledge_base` action='read' pattern='{pattern}' for full content]",
            tail,
            "",
        ]

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
            has_readable = state.custom.get(TurnCustomKey.GRAPH_KNOWLEDGE_HAS_READABLE, False)
            if has_readable:
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


def _has_non_empty_content(path: Path) -> bool:
    """True when ``path`` exists and has non-whitespace content."""
    if not path.exists():
        return False
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


__all__ = ["KnowledgeHook"]
