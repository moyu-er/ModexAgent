"""SubagentAutoSendHook must classify loop_detected as incomplete."""

from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook


def test_loop_detected_is_non_normal():
    assert "loop_detected" in SubagentAutoSendHook._NON_NORMAL_STOPS


def test_classify_loop_detected_hint():
    success, issue = SubagentAutoSendHook._classify(
        stop_reason="loop_detected",
        error=None,
        invocation_id="inv-1",
        is_external=False,
    )
    assert success is False
    assert "loop" in issue.lower()
    assert "invocation_id=inv-1" in issue


def test_classify_loop_detected_no_invocation_id():
    success, issue = SubagentAutoSendHook._classify(
        stop_reason="loop_detected",
        error=None,
        invocation_id="",
        is_external=False,
    )
    assert success is False
    assert "loop" in issue.lower()
