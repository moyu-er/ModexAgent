# tests/unit/multi_agent/test_message_xml.py
"""Tests for message_xml builders."""

from modex_agent.core.agent import AgentImplementation
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.multi_agent.message_xml import (
    build_agent_message,
    build_agent_result,
    build_dispatch_xml,
    build_peer_agent_message,
)


def test_build_agent_message_with_invocation_id():
    result = build_agent_message(
        source="office-expert",
        invocation_id="abc123",
        content="Task done.",
    )
    assert '<agent_message source="office-expert" invocation_id="abc123">' in result
    assert "<content>Task done.</content>" in result


def test_build_agent_message_without_invocation_id():
    result = build_agent_message(
        source="main",
        invocation_id=None,
        content="Hello.",
    )
    assert 'source="main"' in result
    assert "invocation_id" not in result


def test_build_agent_result_completed():
    result = build_agent_result(
        source="office-expert",
        invocation_id="abc123",
        status="completed",
        stop_reason="missed_communication",
        content="All tasks finished.",
    )
    assert (
        '<agent_result source="office-expert" invocation_id="abc123" status="completed">' in result
    )
    assert "<stop_reason>missed_communication</stop_reason>" in result
    assert "<content>All tasks finished.</content>" in result


def test_build_agent_result_max_iterations():
    result = build_agent_result(
        source="planner",
        invocation_id="xyz789",
        status="max_iterations",
        stop_reason="max_iterations",
        content="Still working...",
    )
    assert 'status="max_iterations"' in result
    assert "<stop_reason>max_iterations</stop_reason>" in result


def test_xml_escapes_special_chars():
    result = build_agent_message(
        source="agent<>",
        invocation_id='id"&',
        content="<hello> & world",
    )
    # Attribute values use entity escaping
    assert "agent&lt;&gt;" in result
    assert "id&quot;&amp;" in result
    # Element text uses CDATA when special chars present
    assert "<![CDATA[\n<hello> & world\n]]>" in result


def test_build_peer_agent_message_has_source_and_content():
    result = build_peer_agent_message(
        source="coding",
        content="What's your status?",
    )
    assert '<agent_message source="coding">' in result
    assert "<content>What's your status?</content>" in result


def test_build_peer_agent_message_has_reply_contract():
    """Peer XML MUST include a reply_contract so the receiver knows normal
    output is invisible and the only reply path is send_to_agent."""
    result = build_peer_agent_message(source="coding", content="hi")
    assert "<reply_contract>" in result
    assert "INVISIBLE" in result
    assert "send_to_agent" in result


def test_build_peer_agent_message_names_source_as_reply_target():
    """The reply_contract MUST tell the receiver to send back to the source
    by exact name — otherwise the receiver cannot reply."""
    result = build_peer_agent_message(source="coding", content="hi")
    assert 'target_agent = "coding"' in result


def test_build_peer_agent_message_marks_reply_optional():
    """Reply must be marked conditional — forcing it would ping-pong forever."""
    result = build_peer_agent_message(source="coding", content="hi")
    assert "only if the sender actually needs an answer" in result
    assert "ping-pong" in result


def test_build_peer_agent_message_has_no_invocation_id_attr():
    """Peer XML never carries invocation_id — the sender's prefix is reused,
    and exposing an invocation_id would mislead the receiver into thinking
    it needs to continue a task session."""
    result = build_peer_agent_message(source="coding", content="hi")
    assert "invocation_id" not in result


def test_build_peer_agent_message_escapes_source_in_reply_target():
    """Source name is echoed into target_agent= line — must be escaped."""
    result = build_peer_agent_message(source='naughty"&me', content="hi")
    assert 'target_agent = "naughty&quot;&amp;me"' in result


def test_build_peer_agent_message_external_uses_modexctl_cli():
    """External receivers (opencode/pi) reply via modexctl send CLI, NOT
    send_to_agent tool."""
    result = build_peer_agent_message(
        source="main", content="hi", receiver_implementation=AgentImplementation.EXTERNAL
    )
    assert 'modexctl send --to "main"' in result
    assert "--stdin" in result
    assert "send_to_agent tool" not in result
    assert 'implementation="' not in result


def test_build_peer_agent_message_native_uses_send_to_agent_tool():
    """Modex-native receivers reply via the send_to_agent tool."""
    result = build_peer_agent_message(
        source="main", content="hi", receiver_implementation=AgentImplementation.NATIVE
    )
    assert "send_to_agent tool" in result
    assert "modexctl send" not in result
    assert 'implementation="' not in result


def test_build_peer_agent_message_no_implementation_attr():
    """No implementation attribute on the XML — sender's implementation is
    invisible to agents."""
    result = build_peer_agent_message(
        source="main", content="hi", receiver_implementation=AgentImplementation.EXTERNAL
    )
    assert "implementation=" not in result


def test_build_peer_agent_message_warns_not_to_instruct_others():
    """Receiver should not instruct other agents on how to reply — their
    mechanism may differ."""
    result = build_peer_agent_message(source="main", content="hi")
    assert "Do NOT instruct other agents" in result
    assert "may differ" in result


# ---------------------------------------------------------------------------
# build_dispatch_xml — convergence point for "target is external → peer format"
# ---------------------------------------------------------------------------


def test_build_dispatch_xml_external_target_uses_peer_format():
    """External targets receive the peer format with <reply_contract> +
    modexctl send instructions — the external CLI has no
    SubagentAutoSendHook, so it MUST see the reply contract to reply."""
    result = build_dispatch_xml(
        source="main",
        invocation_id="abc12345",
        content="do work",
        target_execution_strategy=ExecutionStrategyKind.EXTERNAL,
    )
    assert "<reply_contract>" in result
    assert 'modexctl send --to "main"' in result
    assert "--stdin" in result
    assert "INVISIBLE" in result


def test_build_dispatch_xml_external_target_drops_invocation_id_attr():
    """Peer format never carries invocation_id — the session is already
    correlated via MODEX_SESSION_ID env var."""
    result = build_dispatch_xml(
        source="main",
        invocation_id="abc12345",
        content="do work",
        target_execution_strategy=ExecutionStrategyKind.EXTERNAL,
    )
    assert "invocation_id" not in result


def test_build_dispatch_xml_native_target_uses_agent_format():
    """Native targets receive the minimal build_agent_message format —
    SubagentAutoSendHook delivers the reply automatically, so the
    reply_contract is unnecessary token overhead."""
    result = build_dispatch_xml(
        source="main",
        invocation_id="abc12345",
        content="do work",
        target_execution_strategy=ExecutionStrategyKind.REACT,
    )
    assert '<agent_message source="main" invocation_id="abc12345">' in result
    assert "<reply_contract>" not in result
    assert "modexctl send" not in result


def test_build_dispatch_xml_native_target_preserves_invocation_id():
    result = build_dispatch_xml(
        source="main",
        invocation_id="abc12345",
        content="do work",
        target_execution_strategy=ExecutionStrategyKind.REACT,
    )
    assert 'invocation_id="abc12345"' in result
