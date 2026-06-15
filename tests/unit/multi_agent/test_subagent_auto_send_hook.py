"""Tests for SubagentAutoSendHook (FinallyTurnHook rewrite).

Covers:
- Completed with OUTPUT.md → XML notification with output_status=written
- Error crash → is_normal=false, crash hint
- max_iterations → step limit hint
- No agent_bus → no error (graceful no-op)
- result=None → crash notification
- OUTPUT.md missing → output_status=missing, hint
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from framework.core.agent import AgentContext
from framework.core.session_id import SessionInfo
from framework.core.constants import StopReason
from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from framework.hook.builtin import SubagentAutoSendHook
from framework.memory.history import ListMessageHistory
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer


def _make_bus(tmpdir: Path) -> LocalAgentMessageBus:
    server = LocalFileInboxServer(workspace=tmpdir / "inbox")
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer)


def _make_context(
    session_id: str,
    agent_name: str = "worker",
    parent_session_id: str = "conv123.main",
    invocation_id: str | None = None,
) -> AgentContext:
    session = SessionInfo(
        session_id=session_id,
        agent_name=agent_name,
        parent_session_id=parent_session_id,
    )
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        comm_kind=AgentCommKind.SUBAGENT,
    )


def _extract_xml_field(xml: str, tag: str) -> str:
    """Extract text content from a simple XML tag in the notification."""
    pattern = rf"<{tag}>(.*?)</{tag}>"
    m = re.search(pattern, xml, re.DOTALL)
    return m.group(1).strip() if m else ""


def _mock_output_exists(runtime_dir: Path, session_id: str):
    """Return a patch that makes the output_path.exist() return True.

    session_id contains colons (e.g. conv123.worker:a1b2c3d4) which are
    illegal in Windows path components. We cannot create the real file,
    so we mock Path.exists to return True for exactly that path.
    """
    expected = runtime_dir / "output" / session_id / "OUTPUT.md"

    def _exists(self):
        if self == expected:
            return True
        return Path.__exists__(self) if hasattr(Path, "__exists__") else False

    return patch.object(Path, "exists", _exists)


class TestSubagentAutoSendHookFinallyTurn:
    """Verify finally_turn always-fire notification logic."""

    async def test_completed_with_output_sends_xml(self, tmp_path: Path):
        """OUTPUT.md exists → XML with output_status=written, is_normal=true."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id)
        result = AgentResult(content="Done.", stop_reason=StopReason.COMPLETED)

        with _mock_output_exists(runtime_dir, session_id):
            await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main", block=False)
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert "<subagent_notification>" in xml
        assert _extract_xml_field(xml, "output_status") == "written"
        assert _extract_xml_field(xml, "status") == "completed"
        assert _extract_xml_field(xml, "is_normal") == "true"
        assert _extract_xml_field(xml, "stop_reason") == "completed"

    async def test_error_crash_sends_hint(self, tmp_path: Path):
        """Error result → is_normal=false, crash hint."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id)
        result = AgentResult(
            content="",
            stop_reason=StopReason.ERROR,
            error="Division by zero",
        )

        await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main", block=False)
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert _extract_xml_field(xml, "is_normal") == "false"
        assert _extract_xml_field(xml, "status") == "incomplete"
        assert "crashed with error" in _extract_xml_field(xml, "hint")
        assert "Division by zero" in _extract_xml_field(xml, "hint")
        assert _extract_xml_field(xml, "error") == "Division by zero"

    async def test_max_iterations_sends_hint(self, tmp_path: Path):
        """max_iterations → is_normal=false, step limit hint."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id)
        result = AgentResult(
            content="Partial work...",
            stop_reason=StopReason.MAX_ITERATIONS,
        )

        # With OUTPUT.md present, the only issue is max_iterations
        with _mock_output_exists(runtime_dir, session_id):
            await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main", block=False)
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert _extract_xml_field(xml, "is_normal") == "false"
        assert "max_iterations" in _extract_xml_field(xml, "hint")
        assert "is incomplete" in _extract_xml_field(xml, "hint")
        assert _extract_xml_field(xml, "stop_reason") == "max_iterations"

    async def test_no_agent_bus_noop(self):
        """No bus → no error, hook is a graceful no-op."""
        hook = SubagentAutoSendHook(
            agent_bus=None,
            self_name="worker",
            parent_name="main",
        )
        ctx = _make_context("conv123.worker:a1b2c3d4")
        result = AgentResult(content="Done.")

        # Must not raise
        await hook.finally_turn(ctx, result)

    async def test_no_result_sends_error_notification(self, tmp_path: Path):
        """result=None → crash notification (subagent crashed)."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id)

        await hook.finally_turn(ctx, result=None)

        msgs = await bus.consume("conv123.main", block=False)
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert _extract_xml_field(xml, "is_normal") == "false"
        assert _extract_xml_field(xml, "error") == "subagent crashed"
        assert _extract_xml_field(xml, "stop_reason") == "error"

    async def test_output_status_missing_when_no_file(self, tmp_path: Path):
        """No OUTPUT.md → output_status=missing, hint about re-running."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"

        # Do NOT create OUTPUT.md — Path.exists() returns False naturally
        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id)
        result = AgentResult(content="Done.", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main", block=False)
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert _extract_xml_field(xml, "output_status") == "missing"
        assert "OUTPUT.md was not written" in _extract_xml_field(xml, "hint")
        assert _extract_xml_field(xml, "is_normal") == "false"

    async def test_invocation_id_from_session_snowflake(self, tmp_path: Path):
        """invocation_id is the session snowflake (literal external_id) and included in XML."""
        runtime_dir = tmp_path / "runtime"
        session_id = "abc12345.worker"

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id)
        result = AgentResult(content="Done.")

        await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main", block=False)
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert _extract_xml_field(xml, "invocation_id") == "abc12345"

    async def test_think_tags_stripped_from_summary(self, tmp_path: Path):
        """Think tags in content are stripped before truncation."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id)
        result = AgentResult(
            content="<think\nreasoning here\n</think\nActual answer.",
        )

        await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main", block=False)
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        summary = _extract_xml_field(xml, "summary")
        assert "reasoning here" not in summary
        assert "Actual answer." in summary

    async def test_non_default_parent_name(self, tmp_path: Path):
        """parent_name != 'main' → inbox_key routes to correct parent."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"

        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="worker",
            parent_name="qq_bot",
            runtime_dir=runtime_dir,
        )
        ctx = _make_context(session_id, parent_session_id="conv123.qq_bot")
        result = AgentResult(content="Done.")

        await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.qq_bot", block=False)
        assert len(msgs) == 1
        assert msgs[0].payload["metadata"]["agent_type"] == "worker"


class TestSubagentAutoSendHookClassifyStop:
    """Unit tests for _classify_stop static method."""

    def test_error_returns_false_with_hint(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "completed", "written", "Division by zero",
        )
        assert is_normal is False
        assert "crashed" in hint
        assert "Division by zero" in hint
        assert "incomplete" in hint.lower()

    def test_error_with_invocation_id_includes_it(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "completed", "written", "timeout", invocation_id="abc123",
        )
        assert is_normal is False
        assert "invocation_id=abc123" in hint

    def test_max_iterations_returns_false_with_hint(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "max_iterations", "written", None,
        )
        assert is_normal is False
        assert "max_iterations" in hint
        assert "incomplete" in hint.lower()

    def test_max_iterations_with_invocation_id_includes_it(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "max_iterations", "written", None, invocation_id="xyz789",
        )
        assert is_normal is False
        assert "invocation_id=xyz789" in hint

    def test_missing_output_returns_false_with_hint(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "completed", "missing", None,
        )
        assert is_normal is False
        assert "OUTPUT.md was not written" in hint

    def test_missing_output_with_invocation_id_includes_it(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "completed", "missing", None, invocation_id="resume123",
        )
        assert is_normal is False
        assert "invocation_id=resume123" in hint

    def test_completed_with_output_returns_true(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "completed", "written", None,
        )
        assert is_normal is True
        assert hint == ""

    def test_cancelled_with_output_returns_true(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop(
            "cancelled", "written", None,
        )
        assert is_normal is True
        assert hint == ""


class TestSubagentAutoSendHookTruncateContent:
    """Unit tests for _truncate_content class method."""

    def test_short_content_unchanged(self):
        assert SubagentAutoSendHook._truncate_content("hello", max_chars=1500) == "hello"

    def test_long_content_truncated(self):
        content = "x" * 2000
        result = SubagentAutoSendHook._truncate_content(content, max_chars=1500)
        assert result.startswith("x" * 1500)
        assert "[...truncated," in result

    def test_think_tags_stripped(self):
        content = "<think\nreasoning\n</think\nFinal answer."
        result = SubagentAutoSendHook._truncate_content(content)
        assert "reasoning" not in result
        assert "Final answer." in result

    def test_multiple_tag_types_stripped(self):
        content = "<reasoning>step 1</reasoning><think\ndepth\n</think\nFinal."
        result = SubagentAutoSendHook._truncate_content(content)
        assert "step 1" not in result
        assert "depth" not in result
        assert "Final." in result


class TestSubagentAutoSendHookBuildXml:
    """Unit tests for _build_xml static method."""

    def test_xml_structure(self):
        xml = SubagentAutoSendHook._build_xml(
            agent_name="worker",
            invocation_id="abc123",
            status="completed",
            stop_reason="completed",
            is_normal=True,
            error="",
            hint="",
            summary="Task done.",
            trace_dir_rel="trace/conv123.worker:abc123/operations.jsonl",
            output_path_rel="output/conv123.worker:abc123/OUTPUT.md",
            output_status="written",
        )
        assert "<subagent_notification>" in xml
        assert "</subagent_notification>" in xml
        assert "<agent>worker</agent>" in xml
        assert "<invocation_id>abc123</invocation_id>" in xml
        assert "<is_normal>true</is_normal>" in xml
        assert "<error></error>" in xml
        assert "<summary>Task done.</summary>" in xml
        assert "<output_status>written</output_status>" in xml

    def test_xml_escapes_special_chars(self):
        xml = SubagentAutoSendHook._build_xml(
            agent_name="worker",
            invocation_id="abc",
            status="incomplete",
            stop_reason="error",
            is_normal=False,
            error="crashed <with> &special 'chars'",
            hint="Try again",
            summary="",
            trace_dir_rel="t",
            output_path_rel="o",
            output_status="missing",
        )
        assert "<subagent_notification>" in xml
        # xml_text wraps in CDATA when special chars present
        assert "CDATA" in xml or ("&lt;" in xml and "&amp;" in xml)
