"""SubagentAutoSendHook must classify loop_detected as incomplete."""
from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook


def test_loop_detected_is_non_normal():
    assert "loop_detected" in SubagentAutoSendHook._NON_NORMAL_STOPS


def test_classify_loop_detected_hint():
    is_normal, hint = SubagentAutoSendHook._classify_stop_native(
        stop_reason="loop_detected",
        output_status="missing",
        error=None,
        invocation_id="inv-1",
    )
    assert is_normal is False
    assert "loop" in hint.lower()
    assert "invocation_id=inv-1" in hint


def test_classify_loop_detected_no_invocation_id():
    is_normal, hint = SubagentAutoSendHook._classify_stop_native(
        stop_reason="loop_detected",
        output_status="written",
        error=None,
        invocation_id="",
    )
    assert is_normal is False
    assert "loop" in hint.lower()
