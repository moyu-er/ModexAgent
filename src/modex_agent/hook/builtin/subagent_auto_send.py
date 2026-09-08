"""SubagentAutoSendHook — always-fire result notification for subagents.

Fires on FINALLY_GRAPH (guaranteed) — no communication tool check needed.
Subagents have no communication tools; this hook is the sole notification path.

The notification markdown is consumed **by the parent agent's LLM** (injected as a
tool-result message into the parent's conversation).  No code parses the fields
programmatically — every field must be self-explanatory to an LLM.

The hook delegates to ``build_agent_comm_message`` from ``message_format.py``
(convergence — single source of truth for the result markdown format).  The
``content`` body carries the subagent's last output; result metadata (status,
stop reason, issue, output path) is carried by ``ResultMeta`` and
rendered in the header block.

Native (react) subagent — includes the output path::

    Message from subagent 'explore':
    invocation_id: 638aaa67
    status: success
    Stop reason: completed
    Output: /path/to/OUTPUT_1.md

    Result:
    Exploration complete. Found 3 entry points...

    The task is complete and its result is fully delivered — you don't
    need to call task again to collect it. The Result text above is a
    truncated summary; the Output file holds the complete deliverable.
    To assign this subagent new follow-up work, call task with
    invocation_id=638aaa67.

External coding subagent — no file artifacts::

    Message from subagent 'coder':
    invocation_id: 638aaa67
    status: success
    Stop reason: completed

    Result:
    Task finished.

    The task is complete and its result is fully delivered. To assign
    this subagent new follow-up work, call task with
    invocation_id=638aaa67.

On failure an ``Issue:`` line explains the problem::

    Message from subagent 'office-expert':
    invocation_id: 638aaa67
    status: failed
    Stop reason: error
    Issue: Subagent crashed with error: timeout. Task is incomplete. Check the subagent's last output for details.
    Output: /path/to/OUTPUT_1.md

    Result:


    The task is incomplete. To continue it, call task with
    target_agent='office-expert', invocation_id='638aaa67', and
    content=your follow-up instructions — the subagent resumes with its
    prior context.

Design rationale (ADR-0027 evolution):
- ``status`` ("success"/"failed") replaces the old ``success`` boolean XML field.
  ``ResultMeta`` carries it; ``build_agent_comm_message`` renders it in the header.
- ``result_text`` carries the subagent's **real last output** extracted from
  ``result.messages`` (not ``result.content``, which is a placeholder on
  non-normal exit paths).  Notifications are truncated to 300 characters;
  native deliverable files preserve the full content.
- ``issue`` carries failure details and appears **only** on failure, keeping
  the success notification clean.
- Native subagents keep the ``Output:`` line so the parent can read the
  full deliverable.
- External subagents omit file-based artifacts (no OUTPUT.md concept).  The
  ``Replied:`` line is omitted when ``replied`` is None (the per-session
  send-tracking mechanism does not exist yet; the parent judges the outcome
  solely on status, result, and issue).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.core.emitter import StopReason
from modex_agent.core.message_utils import sanitize_reminder_content
from modex_agent.hook.abc import OutcomeFinallyHook
from modex_agent.messaging.models import ReminderKind

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager

logger = logging.getLogger(__name__)


class SubagentAutoSendHook(OutcomeFinallyHook):
    """Result notification for subagents — one per logical turn.

    Fires on the terminal FINALLY_GRAPH leg (success, error, cancel,
    max_iterations); the suspend leg (``result=None``, approval pending) is
    skipped by ``OutcomeFinallyHook``. Sends a markdown result notification
    to the parent inbox via ``build_agent_comm_message`` from
    ``message_format.py``.

    Native (react) subagents include the ``Output:`` file path so the parent
    can read the full deliverable.  External coding subagents omit
    file artifacts.
    """

    NOTIFY_MAX_RESULT_CHARS: int = 300

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
        tree: SessionTreeManager | None = None,
        self_name: str = "",
        parent_name: str = "main",
        runtime_dir: Path | None = None,
        execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT,
        max_result_chars: int = NOTIFY_MAX_RESULT_CHARS,
    ) -> None:
        self._tree = tree
        self._self_name = self_name
        self._parent_name = parent_name
        self._runtime_dir = runtime_dir or Path(".")
        self._execution_strategy = execution_strategy
        self._max_result_chars = max_result_chars

    # -- FINALLY_GRAPH (always fires) ------------------------------------------

    async def on_outcome(self, ctx: AgentContext, result: AgentResult) -> None:
        if self._tree is None:
            raise RuntimeError(
                "SubagentAutoSendHook.tree not wired — "
                "subagent result notification dropped"
            )

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
        full_text = self._extract_full_result_text(result)

        try:
            output_path, write_error = self._write_output_file(session_id, full_text)
        except Exception as exc:
            output_path, write_error = None, str(exc)

        success, issue = self._classify(
            stop_reason, error,
            is_external=False,
        )
        if write_error is not None:
            write_issue = (
                f"Deliverable file write failed: {write_error}. "
                "Full content is in this notification (truncated)."
            )
            issue = f"{issue} {write_issue}".strip()

        notify_text = self._extract_notify_text(result)

        return self._build_content(
            agent_name=self._self_name,
            invocation_id=invocation_id,
            success=success,
            result_text=notify_text,
            issue=issue,
            stop_reason=stop_reason,
            output_path=str(output_path) if output_path is not None else None,
            replied=None,
        )

    def _build_external_content(
        self,
        result: AgentResult | None,
        invocation_id: str,
    ) -> str:
        stop_reason, error, _content = self._extract_raw_fields(result)
        # External subagents have no OUTPUT.md fallback — the notification IS
        # the only delivery. Use the full result text (no truncation) so the
        # parent receives the complete deliverable.
        result_text = self._extract_full_result_text(result)
        success, issue = self._classify(
            stop_reason, error,
            is_external=True,
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
            output_path=None,
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
        output_path: str | None = None,
        replied: bool | None = None,
    ) -> str:
        """Build the markdown result content via ``build_agent_comm_message``.

        Delegates to ``build_agent_comm_message`` with ``ResultMeta``
        (convergence -- single source of truth).  Result metadata fields
        (status, stop reason, issue, output, replied) render in the
        header; ``result_text`` is the body under the ``Result:`` heading.
        """
        from modex_agent.multi_agent.message_format import (
            ResultMeta,
            ResultStatus,
            SourceLabel,
            build_agent_comm_message,
        )

        return build_agent_comm_message(
            source_label=SourceLabel.SUBAGENT,
            source=agent_name,
            content=result_text,
            invocation_id=invocation_id,
            result=ResultMeta(
                status=ResultStatus.FAILED if not success else ResultStatus.SUCCESS,
                stop_reason=StopReason(stop_reason) if stop_reason else None,
                issue=issue or None,
                output_path=output_path,
                replied=replied,
            ),
            reply_contract=None,
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

    def _extract_full_result_text(self, result: AgentResult | None) -> str:
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

        return self._THINK_TAG_RE.sub("", self._THINK_PAIRED_RE.sub("", raw))

    def _extract_notify_text(self, result: AgentResult | None) -> str:
        return self._truncate_content(
            self._extract_full_result_text(result),
            max_chars=min(self._max_result_chars, self.NOTIFY_MAX_RESULT_CHARS),
        )

    def _write_output_file(
        self,
        session_id: str,
        content: str,
    ) -> tuple[Path | None, str | None]:
        try:
            output_dir = self._runtime_dir / "output" / session_id
            output_dir.mkdir(parents=True, exist_ok=True)

            max_number = 0
            for path in output_dir.glob("OUTPUT_*.md"):
                match = re.fullmatch(r"OUTPUT_(\d+)\.md", path.name)
                if match is not None:
                    max_number = max(max_number, int(match.group(1)))

            output_path = output_dir / f"OUTPUT_{max_number + 1}.md"
            output_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            return None, str(exc)
        return output_path, None

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
    _NON_NORMAL_STOPS: frozenset[StopReason] = frozenset({
        StopReason.MAX_ITERATIONS,
        StopReason.TURN_CANCELLED,
        StopReason.TIMEOUT,
        StopReason.LOOP_DETECTED,
    })

    @classmethod
    def _classify(
        cls,
        stop_reason: str,
        error: str | None,
        *,
        is_external: bool,
    ) -> tuple[bool, str]:
        """Classify the subagent outcome as (success, issue).

        Returns (True, "") on success, (False, "<issue text>") on failure.

        The ``is_external`` flag refers to the **subagent** (callee) type,
        which affects what failure signals are reliable:

        - Native subagent: ``error`` / ``max_iterations`` / ``loop_detected``
          / ``timeout`` / ``turn_cancelled`` are all real failures.
        - External subagent: the external CLI's stop_reason may be
          unreliable — it may exit cleanly without ``modexctl send``.
          Only ``error`` and ``loop_detected`` count as hard failures;
          other non-normal stops are left for the parent to judge based
          on the result text.

        """
        # --- Hard failures (both kinds) ---
        if error:
            detail = "Check the subagent's last output for details."
            return False, (
                f"Subagent crashed with error: {error}. Task is incomplete. {detail}"
            )

        if stop_reason == StopReason.LOOP_DETECTED:
            return False, (
                "Subagent was stuck in a loop (repeating the same output or "
                "tool calls). Task is incomplete."
            )

        # --- Native-only soft failures ---
        # External subagents: max_iterations / timeout / turn_cancelled are
        # NOT reliable failure signals — the external CLI may have finished
        # its work without sending a reply.  Let the parent decide based on
        # the result text.
        if not is_external and stop_reason in cls._NON_NORMAL_STOPS:
            return False, f"Subagent stopped with {stop_reason} — task is incomplete."

        return True, ""

    # -- notification ---------------------------------------------------------

    async def _notify_parent(
        self,
        ctx: AgentContext,
        session_id: str,
        content: str,
    ) -> None:
        """Send markdown notification to parent agent's inbox."""
        if self._tree is None:
            raise RuntimeError(
                "SubagentAutoSendHook.tree not wired — "
                "subagent result notification dropped"
            )

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
        content = sanitize_reminder_content(content)

        metadata: dict[str, Any] = {"reminder_kind": ReminderKind.SUBAGENT_RESULT}
        gid = ctx.graph_instance_id
        if gid is not None:
            metadata["graph_instance_id"] = gid

        envelope = AgentMessageEnvelope(
            payload={
                "content": content,
                "message_type": AgentMessageType.AGENT_RESULT,
            },
            source=AgentAddress(name=self._self_name),
            target=AgentAddress(name=self._parent_name),
            message_type=AgentMessageType.AGENT_RESULT,
            session_id=session_id,
            agent_session_id=inbox_key,
            invocation_id=invocation_id,
            metadata=metadata,
        )

        try:
            await self._tree.deliver(inbox_key, envelope)
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
    def _truncate_content(
        cls,
        content: str,
        max_chars: int = NOTIFY_MAX_RESULT_CHARS,
    ) -> str:
        content = cls._THINK_PAIRED_RE.sub("", content)
        content = cls._THINK_TAG_RE.sub("", content)
        if len(content) <= max_chars:
            return content
        if max_chars <= 0:
            return ""

        kept_chars = max_chars
        while True:
            omitted_chars = len(content) - kept_chars
            marker = f"\n[...truncated, {omitted_chars} more chars]"
            next_kept_chars = max(0, max_chars - len(marker))
            if next_kept_chars == kept_chars:
                break
            kept_chars = next_kept_chars
        return (content[:kept_chars] + marker)[:max_chars]
