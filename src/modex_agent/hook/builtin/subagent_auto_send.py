"""SubagentAutoSendHook — always-fire result notification for subagents.

Fires on FINALLY_TURN (guaranteed) — no communication tool check needed.
Subagents have no communication tools; this hook is the sole notification path.

ADR-0027 — the notification's uniform fields (``agent``, ``invocation_id``,
``status``, ``stop_reason``, ``is_normal``, ``error``, ``hint``, ``summary``)
are constructed identically for react and external subagents. Only the
``<artifacts>`` block differs: react carries ``<trace>`` + ``<output>`` +
``<output_status>``; external carries only ``<replied>`` (bool — whether the
subagent emitted at least one ``modexctl send`` during the turn). The parent
agent's decision logic reads only the uniform fields and does not branch on
subagent kind.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.hook.abc import FinallyTurnHook

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.multi_agent.bus import AgentMessageBus

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
        trace_enabled: bool = True,
        execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT,
        external_outbox_path: Path | None = None,
    ) -> None:
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name
        self._runtime_dir = runtime_dir or Path(".")
        self._trace_enabled = trace_enabled
        self._execution_strategy = execution_strategy
        self._external_outbox_path = external_outbox_path

    # -- FINALLY_TURN (always fires) ------------------------------------------

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if self._agent_bus is None:
            return

        stop_reason: str = "error"
        error: str | None = "subagent crashed"
        content = ""
        if result is not None:
            stop_reason = result.stop_reason or "error"
            error = result.error
            content = result.content or ""

        invocation_id = ctx.session.session_id_prefix
        session_id = str(ctx.session)

        if self._execution_strategy is ExecutionStrategyKind.EXTERNAL_CODING:
            xml = self._build_external_xml(
                stop_reason, error, content, invocation_id,
            )
        else:
            xml = self._build_native_xml(
                stop_reason, error, content, invocation_id, session_id,
            )

        await self._notify_parent(ctx, session_id, xml)

    def _build_native_xml(
        self,
        stop_reason: str,
        error: str | None,
        content: str,
        invocation_id: str,
        session_id: str,
    ) -> str:
        trace_path: Path | None = None
        if self._trace_enabled:
            trace_path = self._runtime_dir / "trace" / session_id / "spans.jsonl"
        output_path = self._runtime_dir / "output" / session_id / "OUTPUT.md"
        output_status = "written" if output_path.exists() else "missing"

        is_normal, hint = self._classify_stop_native(
            stop_reason, output_status, error, invocation_id,
        )
        summary = self._truncate_content(content, max_chars=1500)
        return self._build_xml(
            agent_name=self._self_name,
            invocation_id=invocation_id,
            status="completed" if is_normal else "incomplete",
            stop_reason=stop_reason,
            is_normal=is_normal,
            error=error or "",
            hint=hint,
            summary=summary,
            trace_path=str(trace_path) if trace_path is not None else None,
            output_path=str(output_path),
            output_status=output_status,
        )

    def _build_external_xml(
        self,
        stop_reason: str,
        error: str | None,
        content: str,
        invocation_id: str,
    ) -> str:
        replied = self._check_replied()
        is_normal, hint = self._classify_stop_external(
            stop_reason, error, invocation_id,
        )
        summary = self._truncate_content(content, max_chars=1500)
        return self._build_xml(
            agent_name=self._self_name,
            invocation_id=invocation_id,
            status="completed" if is_normal else "incomplete",
            stop_reason=stop_reason,
            is_normal=is_normal,
            error=error or "",
            hint=hint,
            summary=summary,
            replied=replied,
        )

    # -- stop classification --------------------------------------------------

    # Stop reasons that indicate the turn did NOT complete normally.
    _NON_NORMAL_STOPS: frozenset[str] = frozenset({
        "max_iterations",
        "turn_cancelled",
        "timeout",
        "loop_detected",
    })

    @staticmethod
    def _classify_stop_native(
        stop_reason: str,
        output_status: str,
        error: str | None,
        invocation_id: str,
    ) -> tuple[bool, str]:
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
        if stop_reason == "loop_detected":
            return False, (
                f"Subagent stopped with {stop_reason} — it was stuck in a loop "
                f"(repeating the same output or the same tool calls). "
                f"Task is incomplete.{resume}"
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

    @staticmethod
    def _classify_stop_external(
        stop_reason: str,
        error: str | None,
        invocation_id: str,
    ) -> tuple[bool, str]:
        resume = (
            f" To continue, send a message with invocation_id={invocation_id}."
            if invocation_id
            else ""
        )
        if error:
            return False, (
                f"Subagent crashed with error: {error}. Task is incomplete. "
                "Check the subagent's last output for details. "
                "The subagent session can be resumed — "
                f"send a message with invocation_id={invocation_id} to continue."
                if invocation_id
                else f"Subagent crashed with error: {error}. Task is incomplete."
            )
        if stop_reason == "loop_detected":
            return False, (
                f"Subagent stopped with {stop_reason} — it was stuck in a loop "
                f"(repeating the same output or the same tool calls). "
                f"Task is incomplete.{resume}"
            )
        if stop_reason in SubagentAutoSendHook._NON_NORMAL_STOPS:
            return False, (
                f"Subagent stopped with {stop_reason} — task is incomplete."
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
        trace_path: str | None = None,
        output_path: str = "",
        output_status: str = "",
        replied: bool | None = None,
    ) -> str:
        from modex_agent.utils.xml import xml_text

        if replied is not None:
            return SubagentAutoSendHook._build_external_xml_block(
                agent_name=agent_name,
                invocation_id=invocation_id,
                status=status,
                stop_reason=stop_reason,
                is_normal=is_normal,
                error=error,
                hint=hint,
                summary=summary,
                replied=replied,
            )
        return SubagentAutoSendHook._build_native_xml_block(
            agent_name=agent_name,
            invocation_id=invocation_id,
            status=status,
            stop_reason=stop_reason,
            is_normal=is_normal,
            error=error,
            hint=hint,
            summary=summary,
            trace_path=trace_path,
            output_path=output_path,
            output_status=output_status,
        )

    @staticmethod
    def _build_native_xml_block(
        *,
        agent_name: str,
        invocation_id: str,
        status: str,
        stop_reason: str,
        is_normal: bool,
        error: str,
        hint: str,
        summary: str,
        trace_path: str | None,
        output_path: str,
        output_status: str,
    ) -> str:
        from modex_agent.utils.xml import xml_text

        trace_block = (
            f"    <trace>{xml_text(trace_path)}</trace>\n"
            if trace_path is not None
            else ""
        )
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
            "  <artifacts>\n"
            f"{trace_block}"
            f"    <output>{xml_text(output_path)}</output>\n"
            f"    <output_status>{xml_text(output_status)}</output_status>\n"
            "  </artifacts>\n"
            f"</subagent_notification>"
        )

    @staticmethod
    def _build_external_xml_block(
        *,
        agent_name: str,
        invocation_id: str,
        status: str,
        stop_reason: str,
        is_normal: bool,
        error: str,
        hint: str,
        summary: str,
        replied: bool,
    ) -> str:
        from modex_agent.utils.xml import xml_text

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
            "  <artifacts>\n"
            f"    <replied>{str(replied).lower()}</replied>\n"
            "  </artifacts>\n"
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

        # Strip think tags from the XML summary (defense in depth)
        from modex_agent.hook.builtin.inbox_flush import InboxFlushHook

        xml = InboxFlushHook._sanitize_content(xml)

        envelope = AgentMessageEnvelope(
            payload={
                "content": xml,
                "message_type": AgentMessageType.AGENT_RESULT,
                "metadata": {"agent_type": self._self_name, "format": "xml"},
            },
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=self._parent_name),
            message_type=AgentMessageType.AGENT_RESULT,
            session_id=session_id,
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

    def _check_replied(self) -> bool:
        """Return True if the external subagent emitted any modexctl send.

        Simplified for T7: checks whether outbox.jsonl has any content at all.
        Turn-window filtering (entries timestamped within the current turn's
        start/end window) requires BEFORE_TURN dispatch, which T3 did not add
        to ExternalTurnRunner; it can be layered in as a future refinement
        without changing this method's signature.
        """
        if self._external_outbox_path is None:
            return False
        try:
            content = self._external_outbox_path.read_text(encoding="utf-8").strip()
            return bool(content)
        except OSError:
            return False

    @classmethod
    def _truncate_content(cls, content: str, max_chars: int = 1500) -> str:
        content = cls._THINK_PAIRED_RE.sub("", content)
        content = cls._THINK_TAG_RE.sub("", content)
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + f"\n[...truncated, {len(content) - max_chars} more chars]"
