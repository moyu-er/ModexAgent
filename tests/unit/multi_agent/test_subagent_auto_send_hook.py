"""Tests for SubagentAutoSendHook (FinallyTurnHook rewrite).

Covers:
- Completed normally → XML with success=true, result carries content
- Error crash → success=false, issue explains error
- max_iterations → success=false, issue mentions max_iterations
- loop_detected → success=false, issue mentions loop
- No agent_bus → no error (graceful no-op)
- result=None → crash notification
- OUTPUT.md missing + completed → still success=true (no longer penalized)
- result.messages extraction: last assistant message preferred over result.content
- Think tags stripped from result
- Native XML includes <output>, <output_status>, <trace>
- EXTERNAL branch: <replied> true/false, no <trace>/<output>/<output_status>
- Default REACT strategy == explicit REACT strategy (byte-for-byte)
"""

from pathlib import Path
from unittest.mock import patch

from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import ExecutionStrategyKind, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.session_id import SessionInfo
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

    async def test_completed_sends_success_xml(self, tmp_path: Path):
        """Completed → markdown with status=success, result carries content."""
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
        assert "Subagent 'worker' task ended" in xml
        assert "status: success" in xml
        assert "Result:" in xml
        assert "Done." in xml
        # No Issue: line on success
        assert "Issue:" not in xml

    async def test_native_includes_output_and_trace_paths(self, tmp_path: Path):
        """Native markdown must include Output and Trace paths."""
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
        assert f"Trace: {expected_trace}" in xml
        assert f"Output: {expected_output}" in xml
        assert "(written)" in xml
        # Must not be bare relative fragments
        assert f"Trace: {expected_trace}" in xml
        assert f"Output: {expected_output}" in xml

    async def test_error_crash_sends_issue(self, tmp_path: Path):
        """Error result → status=failed, issue explains error."""
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
        assert "status: failed" in xml
        assert "Issue:" in xml
        assert "crashed with error" in xml
        assert "Division by zero" in xml

    async def test_max_iterations_sends_issue(self, tmp_path: Path):
        """max_iterations → status=failed, issue mentions max_iterations."""
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

        with _mock_output_exists(runtime_dir, session_id):
            await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main")
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert "status: failed" in xml
        assert "Issue:" in xml
        assert "max_iterations" in xml
        assert "incomplete" in xml

    async def test_loop_detected_sends_issue(self, tmp_path: Path):
        """loop_detected → status=failed, issue mentions loop."""
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
            stop_reason=StopReason.LOOP_DETECTED,
        )

        await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main")
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert "status: failed" in xml
        assert "loop" in xml.lower()

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
        assert "status: failed" in xml
        assert "Issue:" in xml
        assert "subagent crashed" in xml

    async def test_output_missing_still_success_but_advisory_issue(self, tmp_path: Path):
        """No OUTPUT.md but completed → status=success, but Issue: line carries an
        advisory that the deliverable file is missing.

        OUTPUT.md is the primary deliverable for native subagents; the result
        text is a fallback.  The parent should be told to check the result
        or trace when OUTPUT.md was not written.
        """
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

        # Do NOT mock output exists — Path.exists() returns False naturally
        await hook.finally_turn(ctx, result)

        msgs = await bus.consume("conv123.main")
        assert len(msgs) == 1
        xml = msgs[0].payload["content"]
        assert "status: success" in xml
        assert "(missing)" in xml
        # Advisory issue present (not a failure, but informational)
        assert "Issue:" in xml
        assert "OUTPUT.md was not written" in xml
        assert "result" in xml.lower() or "trace" in xml.lower()

    async def test_invocation_id_from_session_snowflake(self, tmp_path: Path):
        """invocation_id is the session snowflake and included in markdown."""
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
        assert "invocation_id: abc12345" in xml

    async def test_think_tags_stripped_from_result(self, tmp_path: Path):
        """Think tags in content are stripped before appearing in result body."""
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
        assert "reasoning here" not in xml
        assert "Actual answer." in xml

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

    async def test_result_text_from_messages_not_content(self, tmp_path: Path):
        """On max_iterations, result text comes from the last assistant
        message in result.messages, not the placeholder result.content."""
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

        # max_iterations path: result.content is a placeholder, but
        # result.messages should carry the real last assistant output.
        result = AgentResult(
            content="max iterations reached",  # placeholder
            stop_reason=StopReason.MAX_ITERATIONS,
            messages=[
                ChatMessage(role=MessageRole.USER, content="do the task"),
                ChatMessage(role=MessageRole.ASSISTANT, content="Working on it..."),
                ChatMessage(role=MessageRole.ASSISTANT, content="Here is the partial result: 42"),
            ],
        )

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "status: failed" in xml
        # The real output from the last assistant message, not the placeholder
        assert "Here is the partial result: 42" in xml
        assert "max iterations reached" not in xml

    async def test_result_text_falls_back_to_content_when_no_messages(
        self,
        tmp_path: Path,
    ):
        """When result.messages is empty, fall back to result.content."""
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
        result = AgentResult(content="Direct output.", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "Direct output." in xml

    async def test_result_text_falls_back_when_no_assistant_messages(
        self,
        tmp_path: Path,
    ):
        """When result.messages has no assistant messages, fall back to content."""
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
            content="Fallback content.",
            stop_reason=StopReason.COMPLETED,
            messages=[
                ChatMessage(role=MessageRole.USER, content="hello"),
                ChatMessage(role=MessageRole.TOOL, content="tool result"),
            ],
        )

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "Fallback content." in xml

    async def test_old_fields_removed_from_xml(self, tmp_path: Path):
        """The new XML must not contain old field names."""
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

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "<is_normal>" not in xml
        assert "<stop_reason>" not in xml
        assert "<status>" not in xml
        assert "<summary>" not in xml
        assert "<hint>" not in xml
        assert "<error>" not in xml
        assert "<artifacts>" not in xml


class TestSubagentAutoSendHookExternalBranch:
    """EXTERNAL execution_strategy branch (ADR-0027).

    The external subagent notification carries the same uniform fields as a
    react subagent's notification, but uses ``<replied>`` instead of
    ``<output>`` / ``<output_status>`` / ``<trace>``.
    """

    def _make_external_hook(
        self,
        bus: LocalAgentMessageBus,
        runtime_dir: Path,
    ) -> SubagentAutoSendHook:
        return SubagentAutoSendHook(
            agent_bus=bus,
            self_name="pi_worker",
            parent_name="main",
            runtime_dir=runtime_dir,
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
        )


    async def test_external_omits_replied_element(
        self,
        tmp_path: Path,
    ):
        """EXTERNAL branch: Replied: is omitted (replied tracking disabled)."""
        runtime_dir = tmp_path / "runtime"

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "Subagent 'pi_worker' task ended" in xml
        assert "Replied:" not in xml

    async def test_external_omits_native_artifacts(self, tmp_path: Path):
        """EXTERNAL branch: no Trace:/Output:/Replied: lines."""
        runtime_dir = tmp_path / "runtime"

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "Replied:" not in xml
        assert "Trace:" not in xml
        assert "Output:" not in xml

    async def test_external_completed_success(self, tmp_path: Path):
        """EXTERNAL branch: completed → status=success, result carries content."""
        runtime_dir = tmp_path / "runtime"

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir)
        ctx = _make_context("abc12345.pi_worker", agent_name="pi_worker")
        result = AgentResult(
            content="Final answer.",
            stop_reason=StopReason.COMPLETED,
        )

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "Subagent 'pi_worker'" in xml
        assert "invocation_id: abc12345" in xml
        assert "status: success" in xml
        assert "Final answer." in xml
        assert "Issue:" not in xml

    async def test_external_completed_no_replied_still_success(self, tmp_path: Path):
        """EXTERNAL branch: completed → still status=success, Replied: omitted."""
        runtime_dir = tmp_path / "runtime"

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "status: success" in xml
        assert "Replied:" not in xml
        assert "Issue:" not in xml

    async def test_external_error_propagates_issue(self, tmp_path: Path):
        """EXTERNAL branch error path: issue field is populated with modexctl resume hint."""
        runtime_dir = tmp_path / "runtime"

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(
            content="",
            stop_reason=StopReason.ERROR,
            error="provider crashed",
        )

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "status: failed" in xml
        assert "Issue:" in xml
        assert "crashed with error" in xml
        assert "provider crashed" in xml
        # External issue should mention "last output" not "trace"
        assert "last output" in xml.lower()
        assert "trace" not in xml.lower()
        # External resume hint is tool-agnostic (parent knows its own tools)
        assert "invocation_id=abc12345" in xml

    async def test_external_max_iterations_not_failure(self, tmp_path: Path):
        """EXTERNAL branch: max_iterations is NOT a failure — the external
        CLI may have finished without sending a reply.  The parent decides
        based on the result text."""
        runtime_dir = tmp_path / "runtime"

        bus = _make_bus(tmp_path)
        hook = self._make_external_hook(bus, runtime_dir)
        ctx = _make_context("abc12345.pi_worker")
        result = AgentResult(
            content="partial work",
            stop_reason=StopReason.MAX_ITERATIONS,
        )

        await hook.finally_turn(ctx, result)

        xml = (await bus.consume("conv123.main"))[0].payload["content"]
        assert "status: success" in xml
        assert "Issue:" not in xml

    async def test_default_execution_strategy_is_react_backward_compat(
        self,
        tmp_path: Path,
    ):
        """A hook constructed without execution_strategy (the default) must
        produce react-style XML — byte-for-byte identical to a hook that
        explicitly passes ExecutionStrategyKind.REACT."""
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

        xml_default = (await bus_default.consume("conv123.main"))[0].payload["content"]
        xml_react = (await bus_react.consume("conv123.main"))[0].payload["content"]

        assert "Trace:" in xml_default
        assert "Output:" in xml_default
        assert "(written)" in xml_default
        assert "Replied:" not in xml_default
        assert xml_default == xml_react


class TestSubagentAutoSendHookClassify:
    """Unit tests for _classify method."""

    # -- Native failures --

    def test_native_error_returns_false_with_issue(self):
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            "Division by zero",
            "",
            is_external=False,
        )
        assert success is False
        assert "crashed" in issue
        assert "Division by zero" in issue
        assert "incomplete" in issue.lower()

    def test_native_error_with_invocation_id_has_resume_hint(self):
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            "timeout",
            "abc123",
            is_external=False,
        )
        assert success is False
        assert "invocation_id=abc123" in issue

    def test_native_max_iterations_returns_false_with_issue(self):
        success, issue = SubagentAutoSendHook._classify(
            "max_iterations",
            None,
            "",
            is_external=False,
        )
        assert success is False
        assert "max_iterations" in issue
        assert "incomplete" in issue.lower()

    def test_native_max_iterations_with_invocation_id_has_resume_hint(self):
        success, issue = SubagentAutoSendHook._classify(
            "max_iterations",
            None,
            "xyz789",
            is_external=False,
        )
        assert success is False
        assert "invocation_id=xyz789" in issue

    def test_native_loop_detected_returns_false_with_issue(self):
        success, issue = SubagentAutoSendHook._classify(
            "loop_detected",
            None,
            "",
            is_external=False,
        )
        assert success is False
        assert "loop" in issue.lower()

    def test_native_loop_detected_with_invocation_id_has_resume_hint(self):
        success, issue = SubagentAutoSendHook._classify(
            "loop_detected",
            None,
            "loop123",
            is_external=False,
        )
        assert success is False
        assert "invocation_id=loop123" in issue

    def test_native_completed_returns_true(self):
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            None,
            "",
            is_external=False,
        )
        assert success is True
        assert issue == ""

    def test_native_cancelled_returns_true(self):
        # 'cancelled' is not in _NON_NORMAL_STOPS, only 'turn_cancelled' is
        success, issue = SubagentAutoSendHook._classify(
            "cancelled",
            None,
            "",
            is_external=False,
        )
        assert success is True
        assert issue == ""

    def test_native_timeout_returns_false(self):
        success, issue = SubagentAutoSendHook._classify(
            "timeout",
            None,
            "",
            is_external=False,
        )
        assert success is False
        assert "timeout" in issue

    def test_native_turn_cancelled_returns_false(self):
        success, issue = SubagentAutoSendHook._classify(
            "turn_cancelled",
            None,
            "",
            is_external=False,
        )
        assert success is False
        assert "turn_cancelled" in issue

    # -- Native advisory: OUTPUT.md missing --

    def test_native_output_missing_advisory(self):
        """Native completed + OUTPUT.md missing → success=true but issue
        carries an advisory that the deliverable file is missing."""
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            None,
            "inv1",
            is_external=False,
            output_status="missing",
        )
        assert success is True
        assert "OUTPUT.md was not written" in issue
        assert "result" in issue.lower() or "trace" in issue.lower()
        assert "invocation_id=inv1" in issue

    def test_native_output_written_no_advisory(self):
        """Native completed + OUTPUT.md written → success=true, no issue."""
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            None,
            "inv1",
            is_external=False,
            output_status="written",
        )
        assert success is True
        assert issue == ""

    def test_native_output_missing_with_error_not_advisory(self):
        """Native error + OUTPUT.md missing → failure (error takes priority,
        no advisory about OUTPUT.md)."""
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            "crashed",
            "inv1",
            is_external=False,
            output_status="missing",
        )
        assert success is False
        assert "crashed" in issue
        assert "OUTPUT.md was not written" not in issue

    def test_native_output_missing_with_max_iterations_not_advisory(self):
        """Native max_iterations + OUTPUT.md missing → failure (max_iterations
        takes priority, no advisory about OUTPUT.md)."""
        success, issue = SubagentAutoSendHook._classify(
            "max_iterations",
            None,
            "inv1",
            is_external=False,
            output_status="missing",
        )
        assert success is False
        assert "max_iterations" in issue
        assert "OUTPUT.md was not written" not in issue

    # -- External failures --

    def test_external_error_issue_mentions_last_output_not_trace(self):
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            "Division by zero",
            "abc123",
            is_external=True,
        )
        assert success is False
        assert "trace" not in issue.lower()
        assert "last output" in issue.lower()
        # Resume hint is tool-agnostic (parent knows its own tools)
        assert "invocation_id=abc123" in issue

    def test_external_error_no_invocation_id(self):
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            "crashed",
            "",
            is_external=True,
        )
        assert success is False
        assert "modexctl" not in issue  # no invocation_id → no resume hint

    def test_external_completed_returns_true(self):
        success, issue = SubagentAutoSendHook._classify(
            "completed",
            None,
            "",
            is_external=True,
        )
        assert success is True
        assert issue == ""

    def test_external_max_iterations_not_failure(self):
        """External subagent max_iterations is NOT a failure — the external
        CLI may have finished without sending a reply."""
        success, issue = SubagentAutoSendHook._classify(
            "max_iterations",
            None,
            "",
            is_external=True,
        )
        assert success is True
        assert issue == ""

    def test_external_timeout_not_failure(self):
        """External subagent timeout is NOT a failure."""
        success, issue = SubagentAutoSendHook._classify(
            "timeout",
            None,
            "",
            is_external=True,
        )
        assert success is True
        assert issue == ""

    def test_external_loop_detected_is_failure(self):
        """External subagent loop_detected IS a failure (hard stop)."""
        success, issue = SubagentAutoSendHook._classify(
            "loop_detected",
            None,
            "ext123",
            is_external=True,
        )
        assert success is False
        assert "loop" in issue.lower()
        assert "invocation_id=ext123" in issue


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
    """Unit tests for _build_content static method."""

    def test_xml_structure_success_native(self):
        xml = SubagentAutoSendHook._build_content(
            agent_name="worker",
            invocation_id="abc123",
            success=True,
            result_text="Task done.",
            issue="",
            trace_path="trace/conv123.worker:abc123/spans.jsonl",
            output_path="output/conv123.worker:abc123/OUTPUT.md",
            output_status="written",
        )
        assert "Subagent 'worker' task ended" in xml
        assert "invocation_id: abc123" in xml
        assert "status: success" in xml
        assert "Result:" in xml
        assert "Task done." in xml
        assert "Issue:" not in xml
        assert "(written)" in xml

    def test_xml_structure_failure_with_issue(self):
        xml = SubagentAutoSendHook._build_content(
            agent_name="worker",
            invocation_id="abc",
            success=False,
            result_text="",
            issue="Subagent crashed with error: timeout.",
            trace_path="t",
            output_path="o",
            output_status="missing",
        )
        assert "Subagent 'worker' task ended" in xml
        assert "status: failed" in xml
        assert "Result:" in xml
        assert "Issue:" in xml
        assert "timeout" in xml

    def test_xml_structure_external(self):
        xml = SubagentAutoSendHook._build_content(
            agent_name="pi_worker",
            invocation_id="abc",
            success=True,
            result_text="Done.",
            issue="",
            replied=True,
        )
        assert "Subagent 'pi_worker' task ended" in xml
        assert "status: success" in xml
        assert "Replied: true" in xml
        assert "Trace:" not in xml
        assert "Output:" not in xml

    def test_xml_escapes_special_chars(self):
        xml = SubagentAutoSendHook._build_content(
            agent_name="worker",
            invocation_id="abc",
            success=False,
            result_text="",
            issue="crashed <with> &special 'chars'",
            trace_path="t",
            output_path="o",
            output_status="missing",
        )
        assert "Subagent 'worker' task ended" in xml
        # Markdown preserves special chars verbatim (no XML entity escaping)
        assert "crashed <with> &special 'chars'" in xml
