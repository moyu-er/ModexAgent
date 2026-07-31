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
    output_path: Path | None = None
    is_peer_send: bool = False

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
        return _format_peer_ack(result)

    if result.trace_dir is None and result.output_path is None:
        return _format_external_subagent_ack(result)

    return _format_native_subagent_ack(result)


def _format_peer_ack(result: AgentSendResult) -> str:
    return "\n".join(
        [
            f"Message sent to peer agent '{result.target_agent}'.",
            "",
            "The peer agent will process your message asynchronously. If a "
            "reply is needed, the peer agent will send it back via "
            "send_to_agent.",
        ]
    )


def _format_native_subagent_ack(result: AgentSendResult) -> str:
    lines = [
        f"Task dispatched to '{result.target_agent}' - running in background.",
        "",
        "Note: the subagent works asynchronously. You will receive an inbox",
        "notification when it finishes.",
        "",
    ]
    if result.invocation_id:
        lines.append(f"invocation_id: {result.invocation_id}")
    if result.trace_dir is not None:
        lines.append(
            "Trace (live execution log, append-only, safe to read while it "
            f"runs): {result.trace_dir}/spans.jsonl (OTel)"
        )
    if result.output_path is not None:
        lines.append(
            "Output (final deliverable, empty/absent until the subagent "
            f"completes): {result.output_path}"
        )
    lines.extend(
        [
            "",
            "You may tail the Trace file at any time to follow progress. Wait for",
            "the notification before reading the Output file. If the notification",
            "says the task is incomplete, use the invocation_id above to resume.",
        ]
    )
    return "\n".join(lines)


def _format_external_subagent_ack(result: AgentSendResult) -> str:
    lines = [
        f"Task dispatched to '{result.target_agent}' - running in background.",
        "",
        "Note: the subagent works asynchronously. You will receive an inbox",
        "notification when it finishes.",
        "",
    ]
    if result.invocation_id:
        lines.append(f"invocation_id: {result.invocation_id}")
    lines.extend(
        [
            "",
            "The subagent will reply via modexctl send. Wait for the inbox",
            "notification before continuing. If the notification says the task",
            "is incomplete, use the invocation_id above to resume.",
        ]
    )
    return "\n".join(lines)
