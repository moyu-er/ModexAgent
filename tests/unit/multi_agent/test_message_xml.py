# tests/unit/multi_agent/test_message_xml.py
"""Tests for message_xml builders."""

from modex_agent.multi_agent.message_xml import build_agent_message, build_agent_result


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
    assert '<agent_result source="office-expert" invocation_id="abc123" status="completed">' in result
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
    assert 'id&quot;&amp;' in result
    # Element text uses CDATA when special chars present
    assert "<![CDATA[\n<hello> & world\n]]>" in result
