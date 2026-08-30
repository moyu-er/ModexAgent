# tests/unit/multi_agent/test_message_format.py
"""Tests for the unified message_format builder."""

import pytest
from pydantic import ValidationError

from modex_agent.core import message_utils
from modex_agent.core.agent import AgentImplementation
from modex_agent.core.constants import StopReason
from modex_agent.multi_agent import message_format
from modex_agent.multi_agent.message_format import (
    ResultMeta,
    ResultStatus,
    SourceLabel,
    build_agent_comm_message,
    build_dispatch_message,
)


def test_agent_reminder_record_builder_is_owned_by_message_format() -> None:
    assert hasattr(message_format, "build_agent_reminder_record")
    assert not hasattr(message_utils, "build_agent_reminder_record")


# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


def test_source_label_values():
    assert SourceLabel.AGENT.value == "agent"
    assert SourceLabel.PEER_AGENT.value == "peer agent"
    assert SourceLabel.SUBAGENT.value == "subagent"


def test_result_status_values():
    assert ResultStatus.SUCCESS.value == "success"
    assert ResultStatus.FAILED.value == "failed"


# ---------------------------------------------------------------------------
# ResultMeta
# ---------------------------------------------------------------------------


def test_result_meta_frozen():
    meta = ResultMeta(status=ResultStatus.SUCCESS)
    with pytest.raises(ValidationError):
        meta.status = ResultStatus.FAILED  # type: ignore[misc]


def test_result_meta_extra_forbid():
    with pytest.raises(ValidationError):
        ResultMeta(status=ResultStatus.SUCCESS, unknown_field="x")  # type: ignore[call-arg]


def test_result_meta_defaults():
    meta = ResultMeta(status=ResultStatus.SUCCESS)
    assert meta.stop_reason is None
    assert meta.issue is None
    assert meta.output_path is None
    assert meta.replied is None


# ---------------------------------------------------------------------------
# build_agent_comm_message — AGENT source label (dispatch / parent reply)
# ---------------------------------------------------------------------------


def test_agent_message_with_invocation_id():
    result = build_agent_comm_message(
        source_label=SourceLabel.AGENT,
        source="office-expert",
        content="Task done.",
        invocation_id="abc123",
    )
    assert "Message from agent 'office-expert'" in result
    assert "invocation_id: abc123" in result
    assert "Content:" in result
    assert "Task done." in result


def test_agent_message_without_invocation_id():
    result = build_agent_comm_message(
        source_label=SourceLabel.AGENT,
        source="main",
        content="Hello.",
    )
    assert "Message from agent 'main'" in result
    assert "invocation_id" not in result
    assert "Content:" in result


def test_agent_message_preserves_special_chars():
    result = build_agent_comm_message(
        source_label=SourceLabel.AGENT,
        source="agent<>",
        content="<hello> & world",
        invocation_id='id"&',
    )
    assert "agent<>" in result
    assert 'id"&' in result
    assert "<hello> & world" in result


# ---------------------------------------------------------------------------
# build_agent_comm_message — PEER_AGENT source label (cross-pool peer)
# ---------------------------------------------------------------------------


def test_peer_message_has_source_and_content():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="coding",
        content="What's your status?",
        reply_contract=AgentImplementation.NATIVE,
    )
    assert "Message from peer agent 'coding'" in result
    assert "What's your status?" in result
    assert "Content:" in result


def test_peer_message_has_reply_contract_separator():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="coding",
        content="hi",
        reply_contract=AgentImplementation.NATIVE,
    )
    assert "---" in result
    assert "To reply" in result
    assert "send_to_peer tool" in result


def test_peer_message_names_source_as_reply_target():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="coding",
        content="hi",
        reply_contract=AgentImplementation.NATIVE,
    )
    assert 'target_peer = "coding"' in result


def test_peer_message_marks_reply_optional():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="coding",
        content="hi",
        reply_contract=AgentImplementation.NATIVE,
    )
    assert "only if the sender actually needs an answer" in result
    assert "ping-pong" in result


def test_peer_message_has_no_invocation_id():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="coding",
        content="hi",
        invocation_id=None,
        reply_contract=AgentImplementation.NATIVE,
    )
    assert "invocation_id" not in result


def test_peer_message_external_uses_modexctl_cli():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="main",
        content="hi",
        reply_contract=AgentImplementation.EXTERNAL,
    )
    assert 'modexctl send --to "main"' in result
    assert "--stdin" in result
    assert "task tool" not in result


def test_peer_message_native_uses_send_to_peer():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="main",
        content="hi",
        reply_contract=AgentImplementation.NATIVE,
    )
    assert "send_to_peer tool" in result
    assert "target_peer" in result
    assert "modexctl send" not in result
    assert "task tool" not in result


def test_peer_message_warns_output_invisible():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="main",
        content="hi",
        reply_contract=AgentImplementation.NATIVE,
    )
    assert "INVISIBLE to the sender" in result


def test_peer_message_warns_not_to_instruct_others():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="main",
        content="hi",
        reply_contract=AgentImplementation.NATIVE,
    )
    assert "Do NOT instruct other agents" in result
    assert "reply mechanism may differ" in result


def test_peer_message_external_multi_line_stdin_hint():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="main",
        content="hi",
        reply_contract=AgentImplementation.EXTERNAL,
    )
    assert "pipe via stdin to avoid shell quoting issues" in result


def test_peer_message_no_reply_contract_omits_block():
    result = build_agent_comm_message(
        source_label=SourceLabel.PEER_AGENT,
        source="main",
        content="hi",
    )
    assert "---" not in result
    assert "To reply" not in result


# ---------------------------------------------------------------------------
# build_agent_comm_message — SUBAGENT source label + ResultMeta
# ---------------------------------------------------------------------------


def test_subagent_result_success_native():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="All tasks finished.",
        invocation_id="abc123",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
            output_path="/output/OUTPUT.md",
        ),
    )
    assert "Message from subagent 'worker'" in result
    assert "invocation_id: abc123" in result
    assert "status: success" in result
    assert "Stop reason: completed" in result
    assert "Result:" in result
    assert "All tasks finished." in result
    assert "Output: /output/OUTPUT.md" in result
    assert "(written)" not in result
    assert "Trace:" not in result
    assert "Issue:" not in result


def test_subagent_result_failed_with_issue():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="",
        invocation_id="abc123",
        result=ResultMeta(
            status=ResultStatus.FAILED,
            stop_reason=StopReason.ERROR,
            issue="Subagent crashed with error: timeout.",
            output_path="/output/OUTPUT.md",
        ),
    )
    assert "status: failed" in result
    assert "Stop reason: error" in result
    assert "Issue: Subagent crashed with error: timeout." in result
    assert "Output: /output/OUTPUT.md" in result
    assert "Result:" in result


def test_subagent_result_external_no_artifacts():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="pi_worker",
        content="Done.",
        invocation_id="abc123",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
        ),
    )
    assert "Message from subagent 'pi_worker'" in result
    assert "status: success" in result
    assert "Trace:" not in result
    assert "Output:" not in result
    assert "Issue:" not in result


def test_subagent_result_replied_rendered():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="pi_worker",
        content="Done.",
        invocation_id="abc",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
            replied=True,
        ),
    )
    assert "Replied: true" in result


def test_subagent_result_replied_false_rendered():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="pi_worker",
        content="Done.",
        invocation_id="abc",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
            replied=False,
        ),
    )
    assert "Replied: false" in result


def test_subagent_result_replied_none_omitted():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="pi_worker",
        content="Done.",
        invocation_id="abc",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
        ),
    )
    assert "Replied:" not in result


def test_output_path_renders_without_status_suffix():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="done",
        invocation_id="abc",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            output_path="/path/to/output",
        ),
    )
    assert "Output: /path/to/output" in result
    assert "(" not in result.split("Output: /path/to/output")[1].split("\n")[0]


def test_subagent_result_preserves_special_chars():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="",
        invocation_id="abc",
        result=ResultMeta(
            status=ResultStatus.FAILED,
            stop_reason=StopReason.ERROR,
            issue="crashed <with> &special 'chars'",
        ),
    )
    assert "crashed <with> &special 'chars'" in result


def test_subagent_result_no_stop_reason_omits_line():
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="done",
        invocation_id="abc",
        result=ResultMeta(status=ResultStatus.SUCCESS),
    )
    assert "Stop reason:" not in result


def test_success_completed_with_output_renders_fully_delivered_guidance():
    expected_guidance = (
        "The task is complete and its result is fully delivered — you don't\n"
        "need to call task again to collect it. The Result text above is a\n"
        "truncated summary; the Output file holds the complete deliverable.\n"
        "To assign this subagent new follow-up work, call task with\n"
        "invocation_id=abc12345."
    )

    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="summary",
        invocation_id="abc12345",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
            output_path="/tmp/OUTPUT_1.md",
        ),
    )

    assert f"Result:\nsummary\n\n{expected_guidance}" in result


def test_success_completed_without_output_renders_delivered_guidance():
    expected_guidance = (
        "The task is complete and its result is fully delivered. To assign\n"
        "this subagent new follow-up work, call task with\n"
        "invocation_id=abc12345."
    )

    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="complete result",
        invocation_id="abc12345",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
        ),
    )

    assert expected_guidance in result


def test_success_with_issue_renders_deliverable_lost_guidance():
    expected_guidance = (
        "The task is complete, but the deliverable file could not be written\n"
        "(see Issue above) — the Result text is truncated. To retrieve the\n"
        "subagent's full output, continue the session: call task with\n"
        "invocation_id=abc12345."
    )

    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="truncated result",
        invocation_id="abc12345",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.COMPLETED,
            issue="Deliverable file write failed: disk full",
        ),
    )

    assert expected_guidance in result


def test_success_unclean_stop_renders_judge_guidance():
    expected_guidance = (
        "The subagent ended with stop reason 'max_iterations' and did not\n"
        "report clean completion. Judge from the Result above: if the goal\n"
        "was met, treat it as final; otherwise continue by calling task with "
        "invocation_id=abc12345 and refined instructions."
    )

    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="partial result",
        invocation_id="abc12345",
        result=ResultMeta(
            status=ResultStatus.SUCCESS,
            stop_reason=StopReason.MAX_ITERATIONS,
        ),
    )

    assert expected_guidance in result


def test_failed_result_renders_continue_guidance():
    expected_guidance = (
        "The task is incomplete. To continue it, call task with\n"
        "target_agent='worker', invocation_id='abc12345', and\n"
        "content=your follow-up instructions — the subagent resumes with its\n"
        "prior context."
    )

    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="failure summary",
        invocation_id="abc12345",
        result=ResultMeta(
            status=ResultStatus.FAILED,
            stop_reason=StopReason.ERROR,
        ),
    )

    assert expected_guidance in result


def test_every_result_state_with_invocation_id_has_actionable_guidance():
    guidance_markers = (
        "fully delivered",
        "could not be written",
        "did not report clean completion",
        "The task is incomplete",
    )
    stop_reasons = (
        None,
        StopReason.COMPLETED,
        StopReason.ERROR,
        StopReason.MAX_ITERATIONS,
    )
    output_paths = (None, "/tmp/OUTPUT_1.md")
    issues = (None, "Deliverable file write failed: disk full")

    for status in (ResultStatus.SUCCESS, ResultStatus.FAILED):
        for stop_reason in stop_reasons:
            for output_path in output_paths:
                for issue in issues:
                    result = build_agent_comm_message(
                        source_label=SourceLabel.SUBAGENT,
                        source="worker",
                        content="result",
                        invocation_id="abc12345",
                        result=ResultMeta(
                            status=status,
                            stop_reason=stop_reason,
                            output_path=output_path,
                            issue=issue,
                        ),
                    )

                    assert "invocation_id: abc12345" in result
                    normalized_result = result.replace("\n", " ")
                    assert any(marker in normalized_result for marker in guidance_markers)
                    if status == ResultStatus.SUCCESS and issue is not None:
                        assert "could not be written" in result
                        assert "fully delivered" not in result


@pytest.mark.parametrize(
    ("meta", "expected_guidance"),
    [
        (
            ResultMeta(
                status=ResultStatus.SUCCESS,
                stop_reason=StopReason.COMPLETED,
                output_path="/tmp/OUTPUT_1.md",
            ),
            (
                "The task is complete and its result is fully delivered — you don't\n"
                "need to call task again to collect it. The Result text above is a\n"
                "truncated summary; the Output file holds the complete deliverable."
            ),
        ),
        (
            ResultMeta(
                status=ResultStatus.SUCCESS,
                stop_reason=StopReason.COMPLETED,
            ),
            "The task is complete and its result is fully delivered.",
        ),
        (
            ResultMeta(
                status=ResultStatus.SUCCESS,
                stop_reason=StopReason.COMPLETED,
                issue="Deliverable file write failed: disk full",
            ),
            (
                "The task is complete, but the deliverable file could not be written\n"
                "(see Issue above) — the Result text is truncated."
            ),
        ),
        (
            ResultMeta(
                status=ResultStatus.SUCCESS,
                stop_reason=StopReason.MAX_ITERATIONS,
            ),
            (
                "The subagent ended with stop reason 'max_iterations' and did not\n"
                "report clean completion. Judge from the Result above: if the goal\n"
                "was met, treat it as final."
            ),
        ),
        (
            ResultMeta(
                status=ResultStatus.FAILED,
                stop_reason=StopReason.ERROR,
            ),
            "The task is incomplete.",
        ),
    ],
)
def test_empty_invocation_id_omits_follow_up_guidance(meta, expected_guidance):
    result = build_agent_comm_message(
        source_label=SourceLabel.SUBAGENT,
        source="worker",
        content="result",
        invocation_id=None,
        result=meta,
    )

    assert result.endswith(expected_guidance)
    assert "invocation_id=" not in result
    assert "To assign this subagent" not in result
    assert "To retrieve the subagent's full output" not in result
    assert "otherwise continue" not in result
    assert "To continue it" not in result


def test_parent_reply_with_invocation_id_appends_answer_contract():
    expected_answer_block = (
        "---\n\n"
        "To answer this subagent, continue its session: call task with\n"
        "target_agent='worker', invocation_id='abc12345', and\n"
        "content=your answer."
    )

    result = message_format.build_parent_reply_message(
        source="worker",
        invocation_id="abc12345",
        content="What should I do next?",
    )

    assert result.endswith(expected_answer_block)


def test_parent_reply_without_invocation_id_omits_answer_contract():
    result = message_format.build_parent_reply_message(
        source="worker",
        invocation_id=None,
        content="No session to continue.",
    )

    assert "To answer this subagent" not in result
    assert "---" not in result


def test_plain_content_message_remains_byte_identical():
    result = build_agent_comm_message(
        source_label=SourceLabel.AGENT,
        source="main",
        content="hi",
    )

    assert result == "Message from agent 'main':\n\nContent:\nhi"


# ---------------------------------------------------------------------------
# build_dispatch_message — convergence wrapper (subagent dispatch only;
# parent replies use build_parent_reply_message)
# ---------------------------------------------------------------------------


def test_dispatch_external_target_no_reply_contract():
    result = build_dispatch_message(
        source="main",
        invocation_id="abc12345",
        content="do work",
    )
    assert "WARNING" not in result
    assert "modexctl send" not in result
    assert "---" not in result
    assert "To reply" not in result


def test_dispatch_external_target_includes_invocation_id_when_passed():
    result = build_dispatch_message(
        source="main",
        invocation_id="abc12345",
        content="do work",
    )
    assert "invocation_id: abc12345" in result


def test_dispatch_native_target_uses_minimal_format():
    result = build_dispatch_message(
        source="main",
        invocation_id="abc12345",
        content="do work",
    )
    assert "Message from agent 'main'" in result
    assert "invocation_id: abc12345" in result
    assert "Content:" in result
    assert "WARNING" not in result
    assert "---" not in result
    assert "modexctl send" not in result


def test_dispatch_without_invocation_id_omits_session_identity_and_answer_contract():
    result = build_dispatch_message(
        source="main",
        invocation_id=None,
        content="do work",
    )
    assert "Message from agent 'main'" in result
    assert "invocation_id" not in result
    assert "To answer this subagent" not in result
    assert "Content:" in result
    assert "do work" in result
