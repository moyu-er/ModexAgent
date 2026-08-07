"""Value objects and ack formatting for inter-agent communication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modex_agent.multi_agent.comm_kind import AgentCommKind


@dataclass(frozen=True)
class AgentSendResult:
    """Result returned by AgentCommunicationService after a send attempt."""

    target_agent: str
    target_kind: AgentCommKind
    session_id: str
    invocation_id: str | None
    created_new_task: bool
    error: str | None = None
    warning: str | None = None
    trace_dir: Path | None = None
    is_peer_send: bool = False
    is_external: bool = False

    @staticmethod
    def with_error(
        target_agent: str,
        target_kind: AgentCommKind,
        error: str,
        *,
        session_id: str = "",
        invocation_id: str | None = None,
    ) -> AgentSendResult:
        """Build a failed-send result."""
        return AgentSendResult(
            target_agent=target_agent,
            target_kind=target_kind,
            session_id=session_id,
            invocation_id=invocation_id,
            created_new_task=False,
            error=error,
        )


def format_send_ack(result: AgentSendResult) -> str:
    """Format the acknowledgement text returned by send_async."""
    if result.error:
        return f"Error: {result.error}"

    if result.is_peer_send:
        ack = _format_peer_ack(result)
    elif result.target_kind == AgentCommKind.NORMAL and not result.is_peer_send:
        ack = _format_parent_reply_ack(result)
    else:
        ack = _format_subagent_ack(result)

    if result.warning:
        return f"{ack}\n\nNote: {result.warning}"
    return ack


def _format_peer_ack(result: AgentSendResult) -> str:
    return "\n".join(
        [
            f"Message sent to peer agent '{result.target_agent}'.",
            "",
            "next_step: The peer agent will process your message asynchronously"
            " and may or may not reply — peer replies are not automatic."
            " Do NOT call task again for this agent — continue with your"
            " own work or end your turn.",
        ]
    )


def _format_parent_reply_ack(result: AgentSendResult) -> str:
    return "\n".join(
        [
            f"Reply delivered to '{result.target_agent}'.",
            "",
            "automatic_notification: true",
            "",
            "next_step: The parent agent will process your reply asynchronously."
            " Do NOT call send_to_agent again — end your turn or continue with"
            " non-overlapping work.",
        ]
    )


def _format_subagent_ack(result: AgentSendResult) -> str:
    lines = [
        f"Task dispatched to '{result.target_agent}' - running in background.",
        "",
        "automatic_notification: true",
        "",
        "next_step: The result will be delivered to you automatically as a"
        " notification when the subagent finishes. Do NOT call task again"
        " for this agent — end your turn or continue with non-overlapping work.",
        "",
    ]
    if result.invocation_id:
        lines.append(f"invocation_id: {result.invocation_id}")
        lines.append(
            "If the result says the task is incomplete, pass this invocation_id"
            " to continue the session in a later turn."
        )
    return "\n".join(lines)
