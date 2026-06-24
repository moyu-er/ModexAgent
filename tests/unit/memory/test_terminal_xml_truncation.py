"""Tests: terminal tool XML truncation via truncate_xml_safe.

Verifies that the governance system correctly truncates <output> text in
command_result, process_result, and terminal_result XML while preserving
all other fields, tags, and attributes.
"""

from modex_agent.memory.xml_truncate import truncate_xml_safe

# ── test fixtures ──

COMMAND_RESULT = (
    "<command_result>"
    "<terminal>default</terminal>"
    "<output>" + "hello " * 500 + "</output>"
    "<status>completed</status>"
    "<elapsed_ms>234</elapsed_ms>"
    "</command_result>"
)

PROCESS_RESULT = (
    "<process_result>"
    "<action>write</action>"
    "<output>" + "data " * 500 + "</output>"
    "<terminal>default</terminal>"
    "<session_id>ps-abc123</session_id>"
    "<bytes_written>5</bytes_written>"
    "</process_result>"
)

TERMINAL_RESULT = (
    "<terminal_result>"
    "<action>current</action>"
    "<terminal>default</terminal>"
    "<default>true</default>"
    "<status>idle</status>"
    "<cursor>$</cursor>"
    "<output>" + "line " * 500 + "</output>"
    "</terminal_result>"
)

SHORT_TERMINAL_RESULT = (
    "<terminal_result>"
    "<action>list</action>"
    "<output>No active terminals.</output>"
    "</terminal_result>"
)


# ── detection helper tests ──

def test_get_truncatable_paths_returns_none_for_plain_text() -> None:
    from modex_agent.tools.terminal.types import get_terminal_xml_truncatable_paths

    assert get_terminal_xml_truncatable_paths("plain text output") is None
    assert get_terminal_xml_truncatable_paths("") is None


def test_get_truncatable_paths_detects_command_result() -> None:
    from modex_agent.tools.terminal.types import get_terminal_xml_truncatable_paths

    paths = get_terminal_xml_truncatable_paths(COMMAND_RESULT)
    assert "output" in paths


def test_get_truncatable_paths_detects_process_result() -> None:
    from modex_agent.tools.terminal.types import get_terminal_xml_truncatable_paths

    paths = get_terminal_xml_truncatable_paths(PROCESS_RESULT)
    assert paths == ["output"]


def test_get_truncatable_paths_detects_terminal_result() -> None:
    from modex_agent.tools.terminal.types import get_terminal_xml_truncatable_paths

    paths = get_terminal_xml_truncatable_paths(TERMINAL_RESULT)
    assert paths is not None
    assert "output" in paths
    assert "cursor" in paths


def test_get_truncatable_paths_detects_overflow_result() -> None:
    from modex_agent.tools.terminal.types import get_terminal_xml_truncatable_paths

    overflow_xml = (
        '<tool_result_overflow tool="read_file" total_chars="60000" '
        'total_chunks="6" current_chunk="1">\n'
        '  <chunk index="1"><![CDATA[chunk content]]></chunk>\n'
        '</tool_result_overflow>'
    )
    paths = get_terminal_xml_truncatable_paths(overflow_xml)
    assert paths == ["chunk", "instruction"]


# ── truncation tests: command_result ──

def test_command_result_truncates_output_only() -> None:
    result = truncate_xml_safe(COMMAND_RESULT, max_chars=200, truncatable_paths=["output"])

    # structure preserved
    assert "<command_result>" in result
    assert "</command_result>" in result
    # metadata fields untouched
    assert "<terminal>default</terminal>" in result
    assert "<status>completed</status>" in result
    assert "<elapsed_ms>234</elapsed_ms>" in result
    # output was truncated (original 500 copies of "hello " → 3000+ chars, now fit in 200)
    assert len(result) < len(COMMAND_RESULT)


def test_command_result_preserves_short_output() -> None:
    short = "<command_result><output>done</output><status>completed</status><elapsed_ms>10</elapsed_ms></command_result>"

    result = truncate_xml_safe(short, max_chars=100, truncatable_paths=["output"])

    # short output fitted — returned unchanged
    assert "done" in result
    assert "<status>completed</status>" in result


def test_command_result_preserves_all_fields_after_truncation() -> None:
    result = truncate_xml_safe(COMMAND_RESULT, max_chars=300, truncatable_paths=["output"])

    # every non-output field is intact
    assert "<command_result>" in result
    assert "<terminal>default</terminal>" in result
    assert "<status>completed</status>" in result
    assert "<elapsed_ms>234</elapsed_ms>" in result
    assert "</command_result>" in result
    # output element still present but shortened
    assert "<output>" in result
    assert "</output>" in result


# ── truncation tests: process_result ──

def test_process_result_truncates_output_only() -> None:
    result = truncate_xml_safe(PROCESS_RESULT, max_chars=200, truncatable_paths=["output"])

    assert "<process_result>" in result
    assert "</process_result>" in result
    assert "<action>write</action>" in result
    assert "<terminal>default</terminal>" in result
    assert "<session_id>ps-abc123</session_id>" in result
    assert "<bytes_written>5</bytes_written>" in result
    assert len(result) < len(PROCESS_RESULT)


# ── truncation tests: terminal_result ──

def test_terminal_result_truncates_output_only() -> None:
    result = truncate_xml_safe(
        TERMINAL_RESULT, max_chars=200, truncatable_paths=["output", "cursor"],
    )

    assert "<terminal_result>" in result
    assert "</terminal_result>" in result
    assert "<action>current</action>" in result
    assert "<terminal>default</terminal>" in result
    assert "<status>idle</status>" in result
    assert len(result) < len(TERMINAL_RESULT)


def test_terminal_result_short_is_unchanged() -> None:
    result = truncate_xml_safe(
        SHORT_TERMINAL_RESULT, max_chars=500, truncatable_paths=["output"],
    )

    assert "No active terminals." in result
    assert "<output>" in result
    assert len(result) < 500  # fits, so unchanged


# ── metadata is carried by ToolResult.to_message() when declared ──

def test_tool_result_to_message_carries_declared_overflow_metadata() -> None:
    """ToolResult.to_message() emits metadata that was declared on the result.

    Under ADR-0006 the ToolManager no longer sniffs terminal XML itself; tools
    declare metadata via ``result_metadata`` and the ToolManager stores it on
    the ToolResult. The overflow layer constructs ToolResults for
    ``<tool_result_overflow>`` output and passes the metadata explicitly.
    """
    from modex_agent.core.message import ContentFormat
    from modex_agent.core.tool_manager import ToolResult

    xml = (
        '<tool_result_overflow tool="read_file" total_chars="60000" '
        'total_chunks="6" current_chunk="1">\n'
        '  <chunk index="1"><![CDATA[chunk content]]></chunk>\n'
        '</tool_result_overflow>'
    )
    result = ToolResult(
        tool_name="read_file",
        result=xml,
        call_id="tc_1",
        content_format=ContentFormat.XML,
        truncatable_paths=["chunk", "instruction"],
    )
    msg = result.to_message()

    assert msg.get("content_format") == "xml"
    assert msg.get("truncatable_paths") == ["chunk", "instruction"]


def test_tool_result_to_message_no_metadata_when_undeclared() -> None:
    """A bare ToolResult with no declared metadata attaches none (ADR-0006)."""
    from modex_agent.core.tool_manager import ToolResult

    result = ToolResult(
        tool_name="read_file",
        result="<command_result><output>x</output></command_result>",
        call_id="tc_1",
    )
    msg = result.to_message()
    assert "content_format" not in msg
    assert "truncatable_paths" not in msg


# ── edge case: empty truncatable_paths ──

def test_empty_truncatable_paths_preserves_all() -> None:
    result = truncate_xml_safe(COMMAND_RESULT, max_chars=200, truncatable_paths=[])

    assert "<command_result>" in result
    assert "</command_result>" in result
    # nothing truncated by path, falls back to structure-preserving truncation
    assert len(result) < len(COMMAND_RESULT)
