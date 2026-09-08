"""Tests for ApprovalDenialContext."""

from modex_agent.runtime.models import ApprovalDenialContext


class TestApprovalDenialContext:
    def test_fields(self):
        ctx = ApprovalDenialContext(
            tool_name="bash",
            tool_call_id="tc1",
            arguments={"cmd": "rm"},
            tier="dangerous",
            denied_at=100.0,
            reason="denied by user",
            session_id="s1",
        )
        assert ctx.tool_name == "bash"
        assert ctx.tier == "dangerous"
        assert ctx.reason == "denied by user"
