from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.agent import AgentCommKind
from modex_agent.multi_agent.communication.result import AgentSendResult, format_send_ack


def test_parent_reply_ack_is_distinct_from_subagent_dispatch() -> None:
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
        "Reply delivered to 'main'.\n\nThe parent agent will process your reply asynchronously."
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


def test_native_subagent_ack_omits_output_and_tail_wording() -> None:
    result = AgentSendResult(
        target_agent="worker",
        target_kind=AgentCommKind.SUBAGENT,
        session_id="task-1.worker",
        invocation_id="task-1",
        created_new_task=True,
        trace_dir=Path("/tmp/trace"),
    )

    ack = format_send_ack(result)

    assert "Output:" not in ack
    assert "tail the Trace" not in ack
    assert "Wait for the inbox notification" in ack
    assert result.trace_dir is not None
    assert str(result.trace_dir) in ack
    assert result.invocation_id is not None
    assert result.invocation_id in ack
