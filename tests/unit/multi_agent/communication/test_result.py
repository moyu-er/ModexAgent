from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.agent import AgentCommKind
from modex_agent.multi_agent.communication.result import AgentSendResult, format_send_ack


def test_parent_reply_ack_has_anti_redundancy() -> None:
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
        "Reply delivered to 'main'. The parent agent will process\n"
        "your reply asynchronously — do not call send_to_agent again for this\n"
        "reply; end your turn or continue with non-overlapping work."
    )
    assert "non-overlapping" in ack
    assert "do not call send_to_agent again" in ack


def test_peer_ack_has_anti_redundancy() -> None:
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
        "Message sent to peer agent 'peer'. This tool is a\n"
        "communication channel — the peer receives your message, but whether\n"
        "it replies is up to it. Continue with your own work or end your\n"
        "turn; do not call task again for this agent."
    )
    assert "communication channel" in ack
    assert "whether it replies is up to it" in ack.replace("\n", " ")
    assert "do not call task again" in ack


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

    assert ack == (
        "Task dispatched to 'worker' — running in background.\n\n"
        "invocation_id: task-1\n\n"
        "The result will be delivered to you automatically as a notification\n"
        "when the subagent finishes. The preferred action is to end your turn\n"
        "and wait for it; if you continue, choose work that does not overlap\n"
        "with the subagent's task.\n\n"
        "Avoid calling task with this invocation_id again until the\n"
        "notification arrives — the subagent is still working on the current\n"
        "task."
    )
    # D1: no "modexctl send"
    assert "modexctl send" not in ack
    # D2: no "inbox notification"
    assert "inbox notification" not in ack
    assert "notification" in ack  # the result arrives as a notification
    assert "Avoid calling task with this invocation_id" in ack
    # D4: contains invocation_id
    assert result.invocation_id is not None
    assert result.invocation_id in ack
    # No trace path leaked into ack
    assert str(result.trace_dir) not in ack
    # No legacy wording
    assert "tail the Trace" not in ack
    assert "Output:" not in ack


def test_subagent_ack_without_invocation_id_omits_id_spacing() -> None:
    result = AgentSendResult(
        target_agent="worker",
        target_kind=AgentCommKind.SUBAGENT,
        session_id="task-1.worker",
        invocation_id=None,
        created_new_task=True,
    )

    ack = format_send_ack(result)

    assert ack == (
        "Task dispatched to 'worker' — running in background.\n\n"
        "The result will be delivered to you automatically as a notification\n"
        "when the subagent finishes. The preferred action is to end your turn\n"
        "and wait for it; if you continue, choose work that does not overlap\n"
        "with the subagent's task.\n\n"
        "Avoid calling task with this invocation_id again until the\n"
        "notification arrives — the subagent is still working on the current\n"
        "task."
    )


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


def test_ack_shapes_omit_legacy_fields_and_separate_subagent_avoidance() -> None:
    results = (
        AgentSendResult(
            target_agent="peer",
            target_kind=AgentCommKind.NORMAL,
            session_id="conv-1.peer",
            invocation_id=None,
            created_new_task=False,
            is_peer_send=True,
        ),
        AgentSendResult(
            target_agent="main",
            target_kind=AgentCommKind.NORMAL,
            session_id="conv-1.main",
            invocation_id=None,
            created_new_task=False,
            is_peer_send=False,
        ),
        AgentSendResult(
            target_agent="worker",
            target_kind=AgentCommKind.SUBAGENT,
            session_id="task-1.worker",
            invocation_id="task-1",
            created_new_task=True,
        ),
    )

    peer_ack, parent_reply_ack, subagent_ack = tuple(map(format_send_ack, results))

    for ack in (peer_ack, parent_reply_ack, subagent_ack):
        assert "automatic_notification" not in ack
        assert "next_step" not in ack
    assert "\n\nAvoid calling task with this invocation_id" in subagent_ack
