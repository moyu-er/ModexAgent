"""Tests for approval state."""
from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState


class TestApprovalRequest:
    def test_create(self):
        req = ApprovalRequest(
            tool_name="delete_file", tool_call_id="call_001",
            arguments={"path": "/tmp/x"}, tier="dangerous", iteration=2,
        )
        assert req.tool_call_id == "call_001"
        assert req.tier == "dangerous"


class TestApprovalState:
    def test_new_state_all_pending(self):
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "sensitive", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        assert state.every_tool_decided is False
        assert state.unresolved_count == 2

    def test_apply_allowed(self):
        reqs = [ApprovalRequest("t1", "c1", {}, "dangerous", 1)]
        state = ApprovalState(session_id="s1", requests=reqs)
        state.apply("c1", ApprovalDecision.ALLOWED)
        assert state.every_tool_decided is True

    def test_apply_denied_cascades(self):
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "sensitive", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        state.apply("c1", ApprovalDecision.DENIED)
        assert state.final_decisions() == [ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED]

    def test_partial_approval(self):
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        state.apply("c1", ApprovalDecision.ALLOWED)
        assert state.every_tool_decided is False
        assert state.unresolved_count == 1

    def test_status_transitions(self):
        reqs = [
            ApprovalRequest("t1", "c1", {}, "dangerous", 1),
            ApprovalRequest("t2", "c2", {}, "dangerous", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        state.apply("c1", ApprovalDecision.ALLOWED)
        assert state.status == "partial"
        state.apply("c2", ApprovalDecision.ALLOWED)
        assert state.status == "approved"
