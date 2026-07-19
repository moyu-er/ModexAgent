"""Tests for SubagentAutoSendHook (FinallyTurnHook rewrite).

Covers:
- Completed with OUTPUT.md → XML notification with output_status=written
- Error crash → is_normal=false, crash hint
- max_iterations → step limit hint
- No agent_bus → no error (graceful no-op)
- result=None → crash notification
- OUTPUT.md missing → output_status=missing, hint
- EXTERNAL branch: <replied> true/false, no <trace>/<output>/<output_status>,
  uniform fields identical to react; default REACT strategy stays byte-for-byte
  unchanged (ADR-0027, Seam 4).
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.constants import ExecutionStrategyKind, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.builtin import SubagentAutoSendHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_local import LocalFileInboxServer


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

        msgs = await bus.consume("conv123.main")
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert "<subagent_notification>" in xml
        assert _extract_xml_field(xml, "output_status") == "written"
        assert _extract_xml_field(xml, "status") == "completed"
        assert _extract_xml_field(xml, "is_normal") == "true"
        assert _extract_xml_field(xml, "stop_reason") == "completed"

    async def test_notification_uses_absolute_artifact_paths(self, tmp_path: Path):
        """trace/output in the notification must be ABSOLUTE, workspace-rooted
        paths (parity with send_to_agent's ack) — not relative fragments the
        parent cannot resolve. The subagent itself is unaware; the hook owns
        this."""
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

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        expected_trace = str(runtime_dir / "trace" / session_id / "spans.jsonl")
        expected_output = str(runtime_dir / "output" / session_id / "OUTPUT.md")
        assert _extract_xml_field(xml, "trace") == expected_trace
        assert _extract_xml_field(xml, "output") == expected_output
        # Must not be a bare relative fragment.
        assert not _extract_xml_field(xml, "trace").startswith("trace/")
        assert not _extract_xml_field(xml, "output").startswith("output/")

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

        msgs = await bus.consume("conv123.main")
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

        msgs = await bus.consume("conv123.main")
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

        msgs = await bus.consume("conv123.main")
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

        msgs = await bus.consume("conv123.main")
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

        msgs = await bus.consume("conv123.main")
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

        msgs = await bus.consume("conv123.main")
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

        msgs = await bus.consume("conv123.qq_bot")
        assert len(msgs) == 1
        assert msgs[0].payload["metadata"]["agent_type"] == "worker"


class TestSubagentAutoSendHookExternalBranch:
    """Seam 4 — EXTERNAL execution_strategy branch (ADR-0027).

    The external subagent notification carries the same uniform fields as a
    react subagent's notification, but a different ``<artifacts>`` block:
    only ``<replied>`` (bool), no ``<trace>`` / ``<output>`` / ``<output_status>``.
    The parent agent's decision logic reads only the uniform fields and does
    not branch on subagent kind.
    """

    def _make_external_hook(
        self,
        bus: LocalAgentMessageBus,
        runtime_dir: Path,
        outbox_path: Path,
    ) -> SubagentAutoSendHook:
        return SubagentAutoSendHook(
            agent_bus=bus,
            self_name="pi_worker",
            parent_name="main",
            runtime_dir=runtime_dir,
            execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
            external_outbox_path=outbox_path,
        )

    def _write_outbox(self, outbox_path: Path, entries: list[str]) -> None:
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        outbox_path.write_text(
            "\n".join(entries) + ("\n" if entries else ""),
            encoding="utf-8",
        )

    async def test_external_replied_true_when_outbox_has_entries(
        self, tmp_path: Path,
    ):
        """EXTERNAL branch with non-empty outbox → <replied>true</replied>."""
        runtime_dir = tmp_path / "runtime"
        outbox = tmp_path / "workdir" / ".modex" / "external" / "outbox.jsonl"
        self._write_outbox(
            outbox,
            ['{"target":"main","content":"hi from pi"}'],
        )

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir, outbox)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "<subagent_notification>" in xml
        assert _extract_xml_field(xml, "replied") == "true"

    async def test_external_replied_false_when_outbox_empty(
        self, tmp_path: Path,
    ):
        """EXTERNAL branch with empty outbox file → <replied>false</replied>."""
        runtime_dir = tmp_path / "runtime"
        outbox = tmp_path / "workdir" / ".modex" / "external" / "outbox.jsonl"
        self._write_outbox(outbox, [])

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir, outbox)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert _extract_xml_field(xml, "replied") == "false"

    async def test_external_replied_false_when_outbox_missing(
        self, tmp_path: Path,
    ):
        """EXTERNAL branch with no outbox file → <replied>false</replied>."""
        runtime_dir = tmp_path / "runtime"
        outbox = tmp_path / "workdir" / ".modex" / "external" / "outbox.jsonl"

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir, outbox)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert _extract_xml_field(xml, "replied") == "false"

    async def test_external_artifacts_omits_trace_output_output_status(
        self, tmp_path: Path,
    ):
        """EXTERNAL branch <artifacts> contains only <replied> — no react tags."""
        runtime_dir = tmp_path / "runtime"
        outbox = tmp_path / "workdir" / ".modex" / "external" / "outbox.jsonl"
        self._write_outbox(
            outbox,
            ['{"target":"main","content":"hi"}'],
        )

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir, outbox)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "<replied>" in xml
        assert "<trace>" not in xml
        assert "</trace>" not in xml
        assert "<output>" not in xml
        assert "</output>" not in xml
        assert "<output_status>" not in xml
        assert "</output_status>" not in xml
        artifacts_block = re.search(
            r"<artifacts>(.*?)</artifacts>", xml, re.DOTALL,
        )
        assert artifacts_block is not None
        inner = artifacts_block.group(1)
        assert "<replied>" in inner
        assert "<trace>" not in inner
        assert "<output" not in inner

    async def test_external_uniform_fields_match_react_contract(
        self, tmp_path: Path,
    ):
        """EXTERNAL notification carries the same uniform fields a react
        notification would carry for the same result — the parent's decision
        logic reads only these fields and does not branch on subagent kind."""
        runtime_dir = tmp_path / "runtime"
        outbox = tmp_path / "workdir" / ".modex" / "external" / "outbox.jsonl"
        self._write_outbox(outbox, ['{"target":"main","content":"hi"}'])

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir, outbox)
        ctx = _make_context("abc12345.pi_worker", agent_name="pi_worker")
        result = AgentResult(
            content="Final answer.",
            stop_reason=StopReason.COMPLETED,
        )

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert _extract_xml_field(xml, "agent") == "pi_worker"
        assert _extract_xml_field(xml, "invocation_id") == "abc12345"
        assert _extract_xml_field(xml, "status") == "completed"
        assert _extract_xml_field(xml, "stop_reason") == "completed"
        assert _extract_xml_field(xml, "is_normal") == "true"
        assert _extract_xml_field(xml, "error") == ""
        assert _extract_xml_field(xml, "hint") == ""
        assert _extract_xml_field(xml, "summary") == "Final answer."

    async def test_external_normal_when_no_replied_but_completed(
        self, tmp_path: Path,
    ):
        """EXTERNAL branch: missing OUTPUT.md must NOT mark the subagent
        incomplete — the deliverable signal is <replied>, not <output_status>.

        _classify_stop is called with output_status='written' for EXTERNAL
        so the 'OUTPUT.md was not written' branch never fires for external
        subagents."""
        runtime_dir = tmp_path / "runtime"
        outbox = tmp_path / "workdir" / ".modex" / "external" / "outbox.jsonl"
        self._write_outbox(outbox, [])

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir, outbox)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert _extract_xml_field(xml, "is_normal") == "true"
        assert _extract_xml_field(xml, "status") == "completed"
        assert _extract_xml_field(xml, "replied") == "false"
        assert "OUTPUT.md was not written" not in _extract_xml_field(xml, "hint")

    async def test_external_error_propagates_uniform_error_fields(
        self, tmp_path: Path,
    ):
        """EXTERNAL branch error path: uniform error/hint fields are populated
        exactly like the react branch — the only difference is <artifacts>."""
        runtime_dir = tmp_path / "runtime"
        outbox = tmp_path / "workdir" / ".modex" / "external" / "outbox.jsonl"
        self._write_outbox(outbox, [])

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir, outbox)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(
            content="",
            stop_reason=StopReason.ERROR,
            error="provider crashed",
        )

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert _extract_xml_field(xml, "is_normal") == "false"
        assert _extract_xml_field(xml, "status") == "incomplete"
        assert _extract_xml_field(xml, "error") == "provider crashed"
        assert "crashed with error" in _extract_xml_field(xml, "hint")
        assert "provider crashed" in _extract_xml_field(xml, "hint")
        assert "<replied>" in xml
        assert "<trace>" not in xml
        assert "<output>" not in xml
        assert "<output_status>" not in xml

    async def test_external_no_outbox_path_replied_false(
        self, tmp_path: Path,
    ):
        """EXTERNAL branch with external_outbox_path=None → <replied>false
        (defensive default — T8 always passes a real path, but the hook
        tolerates a missing path without raising)."""
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = SubagentAutoSendHook(
            agent_bus=bus,
            self_name="pi_worker",
            parent_name="main",
            runtime_dir=runtime_dir,
            execution_strategy=ExecutionStrategyKind.EXTERNAL_CODING,
            external_outbox_path=None,
        )
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert _extract_xml_field(xml, "replied") == "false"
        assert "<trace>" not in xml
        assert "<output_status>" not in xml

    async def test_default_execution_strategy_is_react_backward_compat(
        self, tmp_path: Path,
    ):
        """A hook constructed without execution_strategy (the default) must
        produce react-style XML — byte-for-byte identical to a hook that
        explicitly passes ExecutionStrategyKind.REACT. Existing react
        subagents don't pass the new params; their notifications must not
        change."""
        runtime_dir = tmp_path / "runtime"
        session_id = "abc12345.worker"

        bus_default = _make_bus(tmp_path / "default")
        hook_default = SubagentAutoSendHook(
            agent_bus=bus_default,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
        )
        bus_react = _make_bus(tmp_path / "react")
        hook_react = SubagentAutoSendHook(
            agent_bus=bus_react,
            self_name="worker",
            parent_name="main",
            runtime_dir=runtime_dir,
            execution_strategy=ExecutionStrategyKind.REACT,
        )

        result = AgentResult(content="Done.", stop_reason=StopReason.COMPLETED)
        with _mock_output_exists(runtime_dir, session_id):
            await hook_default.finally_turn(_make_context(session_id), result)
            await hook_react.finally_turn(_make_context(session_id), result)

        xml_default = (
            await bus_default.consume("conv123.main")
        )[0].payload["content"]
        xml_react = (
            await bus_react.consume("conv123.main")
        )[0].payload["content"]

        assert "<trace>" in xml_default
        assert "<output>" in xml_default
        assert "<output_status>" in xml_default
        assert "<replied>" not in xml_default
        assert xml_default == xml_react


class TestSubagentAutoSendHookClassifyStop:
    """Unit tests for _classify_stop_native and _classify_stop_external."""

    def test_error_returns_false_with_hint(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "completed", "written", "Division by zero", "",
        )
        assert is_normal is False
        assert "crashed" in hint
        assert "Division by zero" in hint
        assert "incomplete" in hint.lower()

    def test_error_with_invocation_id_includes_it(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "completed", "written", "timeout", "abc123",
        )
        assert is_normal is False
        assert "invocation_id=abc123" in hint

    def test_max_iterations_returns_false_with_hint(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "max_iterations", "written", None, "",
        )
        assert is_normal is False
        assert "max_iterations" in hint
        assert "incomplete" in hint.lower()

    def test_max_iterations_with_invocation_id_includes_it(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "max_iterations", "written", None, "xyz789",
        )
        assert is_normal is False
        assert "invocation_id=xyz789" in hint

    def test_missing_output_returns_false_with_hint(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "completed", "missing", None, "",
        )
        assert is_normal is False
        assert "OUTPUT.md was not written" in hint

    def test_missing_output_with_invocation_id_includes_it(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "completed", "missing", None, "resume123",
        )
        assert is_normal is False
        assert "invocation_id=resume123" in hint

    def test_completed_with_output_returns_true(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "completed", "written", None, "",
        )
        assert is_normal is True
        assert hint == ""

    def test_cancelled_with_output_returns_true(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_native(
            "cancelled", "written", None, "",
        )
        assert is_normal is True
        assert hint == ""

    def test_external_error_hint_does_not_mention_trace(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_external(
            "completed", "Division by zero", "abc123",
        )
        assert is_normal is False
        assert "trace" not in hint.lower()
        assert "last output" in hint.lower()

    def test_external_completed_returns_true_no_output_check(self):
        is_normal, hint = SubagentAutoSendHook._classify_stop_external(
            "completed", None, "",
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
            trace_path="trace/conv123.worker:abc123/spans.jsonl",
            output_path="output/conv123.worker:abc123/OUTPUT.md",
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
            trace_path="t",
            output_path="o",
            output_status="missing",
        )
        assert "<subagent_notification>" in xml
        # xml_text wraps in CDATA when special chars present
        assert "CDATA" in xml or ("&lt;" in xml and "&amp;" in xml)
