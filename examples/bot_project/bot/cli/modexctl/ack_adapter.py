"""Adapter: map CLI ``SendResult`` to framework ``AgentSendResult``.

The CLI's ``modexctl send`` command receives a :class:`SendResult` from the
control facade. The framework's :func:`format_send_ack` consumes an
:class:`AgentSendResult`. This adapter bridges the two so the CLI emits the
same acknowledgement text as every other send path (convergence rule 1).
"""

from __future__ import annotations

from typing import assert_never

from bot.control.models import DispatchOutcome, SendResult
from modex_agent.core.agent import AgentCommKind
from modex_agent.multi_agent.communication.result import AgentSendResult


def to_agent_send_result(send_result: SendResult) -> AgentSendResult:
    """Convert a CLI :class:`SendResult` to an :class:`AgentSendResult`."""
    try:
        target_kind = AgentCommKind(send_result.target_kind)
    except ValueError:
        target_kind = AgentCommKind.NORMAL

    warning: str | None
    match send_result.dispatch_outcome:
        case DispatchOutcome.REQUESTED_INVOCATION_NOT_FOUND:
            requested_id = send_result.requested_invocation_id
            warning = (
                f"requested invocation_id '{requested_id}' not found; created new task"
                if requested_id is not None
                else "requested invocation_id not found; created new task"
            )
        case DispatchOutcome.NEW_TASK | DispatchOutcome.RESUMED | DispatchOutcome.NOT_APPLICABLE:
            warning = None
        case unreachable:
            assert_never(unreachable)

    return AgentSendResult(
        target_agent=send_result.target_agent,
        target_kind=target_kind,
        session_id=send_result.session_id,
        invocation_id=send_result.invocation_id,
        created_new_task=send_result.dispatch_outcome == DispatchOutcome.NEW_TASK,
        is_peer_send=send_result.is_peer_send,
        error=None,
        warning=warning,
        output_path=send_result.output_path,
        trace_dir=send_result.trace_dir,
    )
