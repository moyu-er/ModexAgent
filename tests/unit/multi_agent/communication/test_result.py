from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.agent import AgentCommKind
from modex_agent.multi_agent.communication.result import AgentSendResult, format_send_ack


def test_parent_reply_ack_is_distinct_from_subagent_dispatch() -> None:
    """D7: parent-reply ack is unchanged."""
    result = AgentSendResult(
        target_agent="main",
        target_kind=AgentCommKind.NORMAL,
        session_id="conv-1.main",
        invocation_id=None,
        created_new_task=False,
        is_peer_send=False,
    )

    ack = format_send_ack(result)

    assert ack == (
        "Reply delivered to 'main'.\n\n"
        "The parent agent will process your reply asynchronously. "
        "Wait for the notification."
    )


def test_peer_ack_is_unchanged() -> None:
    """D5: peer ack is unchanged."""
    result = AgentSendResult(
        target_agent="peer",
        target_kind=AgentCommKind.NORMAL,
        session_id="conv-1.peer",
        invocation_id=None,
        created_new_task=False,
        is_peer_send=True,
    )

    ack = format_send_ack(result)

    assert ack == (
        "Message sent to peer agent 'peer'.\n\n"
        "The peer agent will process your message asynchronously. "
        "Wait for the notification."
    )


@pytest.mark.parametrize(
    "result",
    [
        AgentSendResult(
            target_agent="peer",
            target_kind=AgentCommKind.NORMAL,
            session_id="conv-1.peer",
            invocation_id=None,
            created_new_task=False,
            is_peer_send=True,
            warning="delivery warning",
        ),
        AgentSendResult(
            target_agent="main",
            target_kind=AgentCommKind.NORMAL,
            session_id="conv-1.main",
            invocation_id=None,
            created_new_task=False,
            warning="delivery warning",
        ),
        AgentSendResult(
            target_agent="external-worker",
            target_kind=AgentCommKind.SUBAGENT,
            session_id="task-1.external-worker",
            invocation_id="task-1",
            created_new_task=True,
            is_external=True,
            warning="delivery warning",
        ),
        AgentSendResult(
            target_agent="worker",
            target_kind=AgentCommKind.SUBAGENT,
            session_id="task-1.worker",
            invocation_id="task-1",
            created_new_task=True,
            trace_dir=Path("/tmp/trace"),
            warning="delivery warning",
        ),
    ],
    ids=["peer", "parent-reply", "external-subagent", "native-subagent"],
)
def test_warning_is_appended_to_every_success_ack(result: AgentSendResult) -> None:
    ack = format_send_ack(result)

    assert ack.endswith("Note: delivery warning")


def test_error_ack_does_not_render_warning() -> None:
    result = AgentSendResult(
        target_agent="worker",
        target_kind=AgentCommKind.SUBAGENT,
        session_id="",
        invocation_id=None,
        created_new_task=False,
        error="send failed",
        warning="delivery warning",
    )

    assert format_send_ack(result) == "Error: send failed"


def test_subagent_ack_omits_implementation_details() -> None:
    """D1-D4: unified subagent ack must not leak native/external implementation details."""
    result = AgentSendResult(
        target_agent="worker",
        target_kind=AgentCommKind.SUBAGENT,
        session_id="task-1.worker",
        invocation_id="task-1",
        created_new_task=True,
        trace_dir=Path("/tmp/trace"),
    )

    ack = format_send_ack(result)

    # D1: no "modexctl send"
    assert "modexctl send" not in ack
    # D2: no "inbox notification"
    assert "inbox notification" not in ack
    # D3: contains "Wait for the notification"
    assert "Wait for the notification" in ack
    # D4: contains invocation_id
    assert result.invocation_id is not None
    assert result.invocation_id in ack
    # No trace path leaked into ack
    assert str(result.trace_dir) not in ack
    # No legacy wording
    assert "tail the Trace" not in ack
    assert "Output:" not in ack


def test_native_and_external_subagent_acks_are_identical() -> None:
    """D6: native and external subagent acks must be byte-for-byte identical."""
    native = AgentSendResult(
        target_agent="worker",
        target_kind=AgentCommKind.SUBAGENT,
        session_id="task-1.worker",
        invocation_id="task-1",
        created_new_task=True,
        trace_dir=Path("/tmp/trace"),
    )
    external = AgentSendResult(
        target_agent="worker",
        target_kind=AgentCommKind.SUBAGENT,
        session_id="task-1.worker",
        invocation_id="task-1",
        created_new_task=True,
        is_external=True,
    )

    assert format_send_ack(native) == format_send_ack(external)
