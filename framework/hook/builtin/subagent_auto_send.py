"""SubagentAutoSendHook — always-fire result notification for subagents.

Fires on FINALLY_TURN (guaranteed) — no communication tool check needed.
Subagents have no communication tools; this hook is the sole notification path.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from framework.hook.abc import FinallyTurnHook

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.multi_agent.bus import AgentMessageBus

logger = logging.getLogger(__name__)


class SubagentAutoSendHook(FinallyTurnHook):
    """Always-fire result notification for subagents.

    Fires on FINALLY_TURN (success, error, cancel, max_iterations — always).
    Derives trace_dir and output_path deterministically from session_id.
    Sends XML notification to parent inbox.
    """

    @property
    def name(self) -> str:
        return "subagent_auto_send_hook"

    _THINK_PAIRED_RE = re.compile(
        r"<\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)"
        r"(.*?)</\s*(?:think|reasoning|reflection)\b[^>]*(?:>|\n)",
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
    ) -> None:
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name
        self._runtime_dir = runtime_dir or Path(".")

    # -- FINALLY_TURN (always fires) ------------------------------------------

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if self._agent_bus is None:
            return

        session_id = str(ctx.session)

        # 1. Derive artifact paths from session_id (deterministic)
        trace_dir = self._runtime_dir / "trace" / session_id
        output_path = self._runtime_dir / "output" / session_id / "OUTPUT.md"

        # 2. Check OUTPUT.md status
        output_status = "written" if output_path.exists() else "missing"

        # 3. Determine stop condition
        stop_reason: str = "error"
        error: str | None = "subagent crashed"
        content = ""
        if result is not None:
            stop_reason = result.stop_reason or "error"
            error = result.error
            content = result.content or ""

        # 4. Get invocation_id from session snowflake
        invocation_id = ctx.session.snowflake

        is_normal, hint = self._classify_stop(
            stop_reason, output_status, error, invocation_id,
        )

        # 5. Truncate last assistant output
        summary = self._truncate_content(content, max_chars=1500)

        # 6. Build XML notification
        xml = self._build_xml(
            agent_name=self._self_name,
            invocation_id=invocation_id,
            status="completed" if is_normal else "incomplete",
            stop_reason=stop_reason,
            is_normal=is_normal,
            error=error or "",
            hint=hint,
            summary=summary,
            trace_dir_rel=f"trace/{session_id}/operations.jsonl",
            output_path_rel=f"output/{session_id}/OUTPUT.md",
            output_status=output_status,
        )

        # 7. Send to parent inbox
        await self._notify_parent(ctx, session_id, xml)

    # -- stop classification --------------------------------------------------

    # Stop reasons that indicate the turn did NOT complete normally.
    _NON_NORMAL_STOPS: frozenset[str] = frozenset({
        "max_iterations",
        "turn_cancelled",
        "timeout",
    })

    @staticmethod
    def _classify_stop(
        stop_reason: str,
        output_status: str,
        error: str | None,
        invocation_id: str = "",
    ) -> tuple[bool, str]:
        """Classify whether the stop was normal and produce an actionable hint.

        The hint tells the parent agent HOW to continue — always including the
        invocation_id so the parent can resume this exact session.
        """
        resume = (
            f" To continue, send a message with invocation_id={invocation_id}."
            if invocation_id
            else ""
        )
        if error:
            return False, (
                f"Subagent crashed with error: {error}. Task is incomplete. "
                "Check the trace for details. The subagent session can be resumed — "
                f"send a message with invocation_id={invocation_id} to continue."
                if invocation_id
                else f"Subagent crashed with error: {error}. Task is incomplete."
            )
        if stop_reason in SubagentAutoSendHook._NON_NORMAL_STOPS:
            return False, (
                f"Subagent stopped with {stop_reason} — task is incomplete."
                f"{resume}"
            )
        if output_status == "missing":
            return False, (
                "Subagent finished but OUTPUT.md was not written — "
                "the deliverable is missing (results may be in conversation only)."
                f"{resume}"
            )
        return True, ""

    # -- XML builder ----------------------------------------------------------

    @staticmethod
    def _build_xml(
        *,
        agent_name: str,
        invocation_id: str,
        status: str,
        stop_reason: str,
        is_normal: bool,
        error: str,
        hint: str,
        summary: str,
        trace_dir_rel: str,
        output_path_rel: str,
        output_status: str,
    ) -> str:
        from framework.utils.xml import xml_text

        return (
            "<subagent_notification>\n"
            f"  <agent>{xml_text(agent_name)}</agent>\n"
            f"  <invocation_id>{xml_text(invocation_id)}</invocation_id>\n"
            f"  <status>{xml_text(status)}</status>\n"
            f"  <stop_reason>{xml_text(stop_reason)}</stop_reason>\n"
            f"  <is_normal>{str(is_normal).lower()}</is_normal>\n"
            f"  <error>{xml_text(error)}</error>\n"
            f"  <hint>{xml_text(hint)}</hint>\n"
            f"  <summary>{xml_text(summary)}</summary>\n"
            f"  <artifacts>\n"
            f"    <trace>{xml_text(trace_dir_rel)}</trace>\n"
            f"    <output>{xml_text(output_path_rel)}</output>\n"
            f"    <output_status>{xml_text(output_status)}</output_status>\n"
            f"  </artifacts>\n"
            f"</subagent_notification>"
        )

    # -- notification ---------------------------------------------------------

    async def _notify_parent(
        self,
        ctx: AgentContext,
        session_id: str,
        xml: str,
    ) -> None:
        """Send XML notification to parent agent's inbox."""
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope

        conversation_id = str(ctx.session)
        invocation_id = ctx.session.snowflake
        parent_session_id = ctx.session.parent_session_id
        if parent_session_id is None:
            logger.warning(
                "SubagentAutoSendHook: no parent_session_id for session %s",
                session_id,
            )
            return
        inbox_key = parent_session_id

        # Strip think tags from the XML summary (defense in depth)
        from framework.hook.builtin.inbox_flush import InboxFlushHook

        xml = InboxFlushHook._sanitize_content(xml)

        envelope = AgentMessageEnvelope(
            payload={
                "content": xml,
                "message_type": "agent_result",
                "metadata": {"agent_type": self._self_name, "format": "xml"},
            },
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=self._parent_name),
            message_type="agent_result",
            conversation_id=conversation_id,
            agent_session_id=inbox_key,
            invocation_id=invocation_id,
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
    def _truncate_content(cls, content: str, max_chars: int = 1500) -> str:
        content = cls._THINK_PAIRED_RE.sub("", content)
        content = cls._THINK_TAG_RE.sub("", content)
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n[...truncated, {len(content) - max_chars} more chars]"
