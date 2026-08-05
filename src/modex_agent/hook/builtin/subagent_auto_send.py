"""SubagentAutoSendHook — always-fire result notification for subagents.

Fires on FINALLY_TURN (guaranteed) — no communication tool check needed.
Subagents have no communication tools; this hook is the sole notification path.

The notification markdown is consumed **by the parent agent's LLM** (injected as a
tool-result message into the parent's conversation).  No code parses the fields
programmatically — every field must be self-explanatory to an LLM.

The hook delegates to ``build_agent_result`` from ``message_xml.py`` (convergence
— single source of truth for the result markdown format).  The ``content`` body
carries the subagent's last output plus optional advisory lines (issue, output
path, trace path, replied flag).

Native (react) subagent — includes output/trace paths::

    Subagent 'explore' task ended (status: success).
    invocation_id: 638aaa67
    Stop reason: normal

    Result:
    Exploration complete. Found 3 entry points...
    Output: /path/to/OUTPUT.md (written)
    Trace: /path/to/spans.jsonl

External coding subagent — no file artifacts::

    Subagent 'coder' task ended (status: success).
    invocation_id: 638aaa67
    Stop reason: normal

    Result:
    Task finished.

On failure an ``Issue:`` line explains the problem and how to resume::

    Subagent 'office-expert' task ended (status: failed).
    invocation_id: 638aaa67
    Stop reason: error

    Result:

    Issue: Subagent crashed with error: timeout. To continue, send a message with invocation_id=638aaa67.
    Output: /path/to/OUTPUT.md (missing)
    Trace: /path/to/spans.jsonl

Design rationale (ADR-0027 evolution):
- ``status`` ("success"/"failed") replaces the old ``success`` boolean XML field.
  ``build_agent_result`` renders it in the header line.
- ``result_text`` carries the subagent's **real last output** extracted from
  ``result.messages`` (not ``result.content``, which is a placeholder on
  non-normal exit paths).  Truncated to ``max_result_chars`` (default 6000).
- ``issue`` merges the old ``error`` + ``hint`` and appears **only** on
  failure, keeping the success notification clean.
- Native subagents keep ``Output:`` / ``Trace:`` lines so the parent can read
  the full deliverable and trace file.
- External subagents omit file-based artifacts (no OUTPUT.md concept).  The
  ``Replied:`` line is omitted when ``replied`` is None (the per-session
  send-tracking mechanism does not exist yet; the parent judges the outcome
  solely on status, result, and issue).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.types import ReminderKind
from modex_agent.hook.abc import FinallyTurnHook

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.multi_agent.bus import AgentMessageBus

logger = logging.getLogger(__name__)


class SubagentAutoSendHook(FinallyTurnHook):
    """Always-fire result notification for subagents.

    Fires on FINALLY_TURN (success, error, cancel, max_iterations — always).
    Sends a markdown result notification to the parent inbox via
    ``build_agent_result`` from ``message_xml.py``.

    Native (react) subagents include ``Output:`` and ``Trace:`` file paths so
    the parent can read the full deliverable.  External coding subagents omit
    file artifacts.
    """

    #: Default truncation limit for the result text (~1500 tokens).
    DEFAULT_MAX_RESULT_CHARS: int = 6000

    @property
    def name(self) -> str:
        return "subagent_auto_send_hook"

    _THINK_PAIRED_RE = re.compile(
        r"<\s*(?:think|reasoning|reflection)\b[^>]*[>\n]"
        r"(.*?)</\s*(?:think|reasoning|reflection)\b[^>]*[>\n]",
        re.IGNORECASE | re.DOTALL,
    )
    _THINK_TAG_RE = re.compile(
        r"<\s*/?\s*(?:think|reasoning|reflection)\b[^>]*>?",
        re.IGNORECASE,
    )

    def __init__(
        self,
        agent_bus: AgentMessageBus | None = None,
        self_name: str = "",
        parent_name: str = "main",
        runtime_dir: Path | None = None,
        trace_enabled: bool = True,
        execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    ) -> None:
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name
        self._runtime_dir = runtime_dir or Path(".")
        self._trace_enabled = trace_enabled
        self._execution_strategy = execution_strategy
        self._max_result_chars = max_result_chars

    # -- FINALLY_TURN (always fires) ------------------------------------------

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if self._agent_bus is None:
            return

        invocation_id = ctx.session.session_id_prefix
        session_id = str(ctx.session)

        if self._execution_strategy is ExecutionStrategyKind.EXTERNAL:
            content = self._build_external_content(result, invocation_id)
        else:
            content = self._build_native_content(result, invocation_id, session_id)

        await self._notify_parent(ctx, session_id, content)

    # -- content construction -------------------------------------------------

    def _build_native_content(
        self,
        result: AgentResult | None,
        invocation_id: str,
        session_id: str,
    ) -> str:
        stop_reason, error, _content = self._extract_raw_fields(result)
        result_text = self._extract_result_text(result)

        trace_path: Path | None = None
        if self._trace_enabled:
            trace_path = self._runtime_dir / "trace" / session_id / "spans.jsonl"
        output_path = self._runtime_dir / "output" / session_id / "OUTPUT.md"
        output_status = "written" if output_path.exists() else "missing"

        success, issue = self._classify(
            stop_reason, error, invocation_id,
            is_external=False,
            output_status=output_status,
        )

        return self._build_content(
            agent_name=self._self_name,
            invocation_id=invocation_id,
            success=success,
            result_text=result_text,
            issue=issue,
            stop_reason=stop_reason,
            trace_path=str(trace_path) if trace_path is not None else None,
            output_path=str(output_path),
            output_status=output_status,
            replied=None,
        )

    def _build_external_content(
        self,
        result: AgentResult | None,
        invocation_id: str,
    ) -> str:
        stop_reason, error, _content = self._extract_raw_fields(result)
        result_text = self._extract_result_text(result)
        success, issue = self._classify(
            stop_reason, error, invocation_id,
            is_external=True,
            output_status=None,
        )
        # replied is None — the Replied: line is omitted from the content.
        # A correct per-session send-tracking mechanism (e.g. modexctl
        # writing a .sent marker after successful fetch_send) does not
        # exist yet. The parent agent judges the outcome solely on
        # status, result, and issue.
        replied: bool | None = None

        return self._build_content(
            agent_name=self._self_name,
            invocation_id=invocation_id,
            success=success,
            result_text=result_text,
            issue=issue,
            stop_reason=stop_reason,
            trace_path=None,
            output_path=None,
            output_status=None,
            replied=replied,
        )

    @staticmethod
    def _build_content(
        *,
        agent_name: str,
        invocation_id: str,
        success: bool,
        result_text: str,
        issue: str,
        stop_reason: str = "",
        trace_path: str | None = None,
        output_path: str | None = None,
        output_status: str | None = None,
        replied: bool | None = None,
    ) -> str:
        """Build the markdown result content via ``build_agent_result``.

        Delegates the header format to ``build_agent_result`` (convergence —
        single source of truth).  The ``content`` body is the result text
        followed by optional advisory lines (Issue, Output, Trace, Replied),
        each omitted when its value is None/empty.
        """
        from modex_agent.multi_agent.message_xml import build_agent_result

        body_lines: list[str] = [result_text]
        if issue:
            body_lines.append(f"Issue: {issue}")
        if output_path is not None:
            if output_status is not None:
                body_lines.append(f"Output: {output_path} ({output_status})")
            else:
                body_lines.append(f"Output: {output_path}")
        if trace_path is not None:
            body_lines.append(f"Trace: {trace_path}")
        if replied is not None:
            body_lines.append(f"Replied: {str(replied).lower()}")

        return build_agent_result(
            source=agent_name,
            invocation_id=invocation_id,
            status="success" if success else "failed",
            stop_reason=stop_reason,
            content="\n".join(body_lines),
        )

    # -- field extraction -----------------------------------------------------

    @staticmethod
    def _extract_raw_fields(
        result: AgentResult | None,
    ) -> tuple[str, str | None, str]:
        """Return (stop_reason, error, content) from result, with safe defaults."""
        if result is None:
            return "error", "subagent crashed", ""
        return (
            result.stop_reason or "error",
            result.error,
            result.content or "",
        )

    def _extract_result_text(self, result: AgentResult | None) -> str:
        """Extract the subagent's real last output.

        Prefers the last assistant message from ``result.messages`` (which is
        the actual output even on max_iterations / error paths), falling back
        to ``result.content`` (only meaningful on normal completion).
        """
        raw = ""
        if result is not None and result.messages:
            for msg in reversed(result.messages):
                role = self._get_role(msg)
                if str(role) == "assistant":
                    content = self._get_content(msg)
                    if content:
                        raw = str(content)
                        break
        if not raw and result is not None:
            raw = result.content or ""

        return self._truncate_content(raw, max_chars=self._max_result_chars)

    @staticmethod
    def _get_role(msg: ChatMessage | dict[str, object]) -> object:
        if isinstance(msg, dict):
            return msg.get("role", "")
        return msg.role

    @staticmethod
    def _get_content(msg: ChatMessage | dict[str, object]) -> object:
        if isinstance(msg, dict):
            return msg.get("content", "")
        return msg.content

    # -- success classification -----------------------------------------------

    #: Stop reasons that indicate the turn did NOT complete normally.
    _NON_NORMAL_STOPS: frozenset[str] = frozenset({
        "max_iterations",
        "turn_cancelled",
        "timeout",
        "loop_detected",
    })

    @classmethod
    def _classify(
        cls,
        stop_reason: str,
        error: str | None,
        invocation_id: str,
        *,
        is_external: bool,
        output_status: str | None = None,
    ) -> tuple[bool, str]:
        """Classify the subagent outcome as (success, issue).

        Returns (True, "") on success, (False, "<issue text>") on failure.
        On success with a caveat (e.g. native OUTPUT.md missing), returns
        (True, "<advisory issue>") so the parent is informed.

        The ``is_external`` flag refers to the **subagent** (callee) type,
        which affects what failure signals are reliable:

        - Native subagent: ``error`` / ``max_iterations`` / ``loop_detected``
          / ``timeout`` / ``turn_cancelled`` are all real failures.
          ``output_status="missing"`` is an advisory: OUTPUT.md is the
          primary deliverable, so the parent should be told to read the
          result text instead (or check the trace).
        - External subagent: the external CLI's stop_reason may be
          unreliable — it may exit cleanly without ``modexctl send``.
          Only ``error`` and ``loop_detected`` count as hard failures;
          other non-normal stops are left for the parent to judge based
          on the result text.  ``output_status`` is always ``None`` for
          external (no OUTPUT.md concept).

        The resume hint does **not** depend on the subagent type — it is
        advice to the **parent** (the agent receiving this notification).
        The hook runs on the subagent side and does not know the parent's
        type, so it uses the tool-agnostic wording "send a message with
        invocation_id=xxx" (matching the original design).  The parent
        already knows which communication tool it has.
        """
        resume = cls._resume_hint(invocation_id)

        # --- Hard failures (both kinds) ---
        if error:
            detail = (
                "Check the subagent's last output for details."
                if is_external
                else "Check the trace for details."
            )
            return False, (
                f"Subagent crashed with error: {error}. Task is incomplete. "
                f"{detail}{resume}"
            )

        if stop_reason == "loop_detected":
            return False, (
                f"Subagent was stuck in a loop (repeating the same output or "
                f"tool calls). Task is incomplete.{resume}"
            )

        # --- Native-only soft failures ---
        # External subagents: max_iterations / timeout / turn_cancelled are
        # NOT reliable failure signals — the external CLI may have finished
        # its work without sending a reply.  Let the parent decide based on
        # the result text.
        if not is_external and stop_reason in cls._NON_NORMAL_STOPS:
            return False, (
                f"Subagent stopped with {stop_reason} — task is incomplete."
                f"{resume}"
            )

        # --- Native advisory: OUTPUT.md missing ---
        # Not a failure (the subagent completed normally), but the primary
        # deliverable file was not written.  The parent should rely on the
        # result text or check the trace.
        if (
            not is_external
            and output_status == "missing"
            and stop_reason not in cls._NON_NORMAL_STOPS
            and not error
        ):
            return True, (
                "Subagent finished but OUTPUT.md was not written — "
                "the deliverable file is missing. "
                "Check the result text for the subagent's last output"
                f" or read the trace for details.{resume}"
            )

        return True, ""

    @staticmethod
    def _resume_hint(invocation_id: str) -> str:
        """Build a resume instruction for the parent agent.

        Tool-agnostic: the hook runs on the subagent side and does not know
        whether the parent is native (uses ``send_to_agent``) or external
        (uses ``modexctl send``).  The parent already knows its own tools,
        so we only state the invocation_id to resume with.
        """
        if not invocation_id:
            return ""
        return f" To continue, send a message with invocation_id={invocation_id}."

    # -- notification ---------------------------------------------------------

    async def _notify_parent(
        self,
        ctx: AgentContext,
        session_id: str,
        content: str,
    ) -> None:
        """Send markdown notification to parent agent's inbox."""
        if self._agent_bus is None:
            return

        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.envelope import AgentMessageEnvelope
        from modex_agent.multi_agent.message_type import AgentMessageType

        session_id = str(ctx.session)
        invocation_id = ctx.session.session_id_prefix
        parent_session_id = ctx.session.parent_session_id
        if parent_session_id is None:
            logger.warning(
                "SubagentAutoSendHook: no parent_session_id for session %s",
                session_id,
            )
            return
        inbox_key = parent_session_id

        # Strip think tags from the content (defense in depth)
        from modex_agent.hook.builtin.inbox_flush import InboxFlushHook

        content = InboxFlushHook._sanitize_content(content)

        envelope = AgentMessageEnvelope(
            payload={
                "content": content,
                "message_type": AgentMessageType.AGENT_RESULT,
                "metadata": {"agent_type": self._self_name, "format": "markdown"},
            },
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=self._parent_name),
            message_type=AgentMessageType.AGENT_RESULT,
            session_id=session_id,
            agent_session_id=inbox_key,
            invocation_id=invocation_id,
            metadata={"reminder_kind": ReminderKind.SUBAGENT_RESULT},
        )

        try:
            await self._agent_bus.send(inbox_key, envelope)
            logger.info(
                "SubagentAutoSendHook: notified parent %s (agent=%s, session=%s)",
                self._parent_name,
                self._self_name,
                session_id,
            )
        except Exception:
            logger.exception(
                "SubagentAutoSendHook: failed to notify parent %s",
                self._parent_name,
            )

    # -- content helpers ------------------------------------------------------

    @classmethod
    def _truncate_content(cls, content: str, max_chars: int = DEFAULT_MAX_RESULT_CHARS) -> str:
        content = cls._THINK_PAIRED_RE.sub("", content)
        content = cls._THINK_TAG_RE.sub("", content)
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n[...truncated, {len(content) - max_chars} more chars]"
