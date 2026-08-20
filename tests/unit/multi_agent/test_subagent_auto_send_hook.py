from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager


def _make_bus(tmpdir: Path) -> LocalAgentMessageBus:
    server = LocalFileInboxServer(workspace=tmpdir / "inbox")
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)
    return LocalAgentMessageBus(producer=producer, consumer=consumer)


def _make_context(
    session_id: str,
    agent_name: str = "worker",
    parent_session_id: str = "conv123.main",
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


def _make_hook(
    bus: LocalAgentMessageBus | None,
    runtime_dir: Path,
    *,
    execution_strategy: ExecutionStrategyKind = ExecutionStrategyKind.REACT,
) -> SubagentAutoSendHook:
    tree: SessionTreeManager | None = None
    if bus is not None:
        tree = MagicMock(spec=SessionTreeManager)

        async def _deliver(sid: str, env: object) -> None:
            await bus.send(sid, env)  # type: ignore[arg-type]

        tree.deliver = _deliver
    return SubagentAutoSendHook(
        tree=tree,
        self_name="worker",
        parent_name="main",
        runtime_dir=runtime_dir,
        execution_strategy=execution_strategy,
    )


async def _consume_content(bus: LocalAgentMessageBus) -> str:
    messages = await bus.consume("conv123.main")
    assert len(messages) == 1
    return messages[0].payload["content"]


def _result_body(notification: str) -> str:
    return notification.split("Result:\n", maxsplit=1)[1]


class TestSubagentAutoSendHookFinallyTurn:
    async def test_completed_writes_output_1_and_sends_truncated_notification(
        self,
        tmp_path: Path,
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        content = "x" * 500
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        await hook.finally_graph(
            _make_context(session_id),
            AgentResult(content=content, stop_reason=StopReason.COMPLETED),
        )

        output_path = runtime_dir / "output" / session_id / "OUTPUT_1.md"
        notification = await _consume_content(bus)
        assert output_path.read_text() == content
        assert f"Output: {output_path}" in notification
        assert len(_result_body(notification)) <= 300
        assert "[...truncated," in notification

    async def test_same_session_second_turn_writes_output_2(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)
        ctx = _make_context(session_id)

        await hook.finally_graph(ctx, AgentResult(content="first"))
        await bus.consume("conv123.main")
        await hook.finally_graph(ctx, AgentResult(content="second"))

        output_dir = runtime_dir / "output" / session_id
        assert (output_dir / "OUTPUT_1.md").read_text() == "first"
        assert (output_dir / "OUTPUT_2.md").read_text() == "second"
        notification = await _consume_content(bus)
        assert f"Output: {output_dir / 'OUTPUT_2.md'}" in notification

    async def test_preexisting_output_1_causes_output_2(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        output_dir = runtime_dir / "output" / session_id
        output_dir.mkdir(parents=True)
        (output_dir / "OUTPUT_1.md").write_text("existing")
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        await hook.finally_graph(_make_context(session_id), AgentResult(content="new"))

        assert (output_dir / "OUTPUT_1.md").read_text() == "existing"
        assert (output_dir / "OUTPUT_2.md").read_text() == "new"

    async def test_error_result_still_writes_output_1(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)
        result = AgentResult(
            content="error placeholder",
            stop_reason=StopReason.ERROR,
            error="Division by zero",
            messages=[
                ChatMessage(role=MessageRole.ASSISTANT, content="Work before failure"),
            ],
        )

        await hook.finally_graph(_make_context(session_id), result)

        output_path = runtime_dir / "output" / session_id / "OUTPUT_1.md"
        notification = await _consume_content(bus)
        assert output_path.read_text() == "Work before failure"
        assert "status: failed" in notification
        assert "Issue:" in notification
        assert "Division by zero" in notification

    async def test_max_iterations_still_writes_output_1(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        await hook.finally_graph(
            _make_context(session_id),
            AgentResult(content="Partial work", stop_reason=StopReason.MAX_ITERATIONS),
        )

        assert (
            runtime_dir / "output" / session_id / "OUTPUT_1.md"
        ).read_text() == "Partial work"
        notification = await _consume_content(bus)
        assert "status: failed" in notification
        assert "Issue:" in notification
        assert "max_iterations" in notification

    async def test_suspend_result_none_writes_nothing_and_notifies_no_one(
        self, tmp_path: Path
    ) -> None:
        """``result=None`` is the GraphInterrupt (approval suspend) dispatch —
        the turn has not ended, so no OUTPUT file is written and the parent
        receives no notification (the resumed turn notifies on completion)."""
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        await hook.finally_graph(_make_context(session_id), result=None)

        assert not (runtime_dir / "output" / session_id).exists()
        assert await bus.consume("conv123.main") == []

    async def test_file_uses_last_assistant_message_without_truncation(
        self,
        tmp_path: Path,
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        full_output = "last assistant output " + ("z" * 500)
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)
        result = AgentResult(
            content="placeholder",
            messages=[
                ChatMessage(role=MessageRole.ASSISTANT, content="earlier"),
                ChatMessage(role=MessageRole.USER, content="continue"),
                ChatMessage(role=MessageRole.ASSISTANT, content=full_output),
            ],
        )

        await hook.finally_graph(_make_context(session_id), result)

        assert (
            runtime_dir / "output" / session_id / "OUTPUT_1.md"
        ).read_text() == full_output

    async def test_file_falls_back_to_content_without_assistant_message(
        self,
        tmp_path: Path,
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)
        result = AgentResult(
            content="Fallback content",
            messages=[ChatMessage(role=MessageRole.USER, content="do it")],
        )

        await hook.finally_graph(_make_context(session_id), result)

        assert (
            runtime_dir / "output" / session_id / "OUTPUT_1.md"
        ).read_text() == "Fallback content"

    async def test_think_tags_are_removed_from_file_and_notification(
        self,
        tmp_path: Path,
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)
        result = AgentResult(content="<think\nsecret\n</think\nActual answer")

        await hook.finally_graph(_make_context(session_id), result)

        file_content = (
            runtime_dir / "output" / session_id / "OUTPUT_1.md"
        ).read_text()
        notification = await _consume_content(bus)
        assert file_content == "Actual answer"
        assert "secret" not in notification
        assert "Actual answer" in notification

    async def test_long_content_is_full_in_file_and_bounded_in_notification(
        self,
        tmp_path: Path,
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        content = "0123456789" * 100
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        await hook.finally_graph(_make_context(session_id), AgentResult(content=content))

        assert (
            runtime_dir / "output" / session_id / "OUTPUT_1.md"
        ).read_text() == content
        assert len(_result_body(await _consume_content(bus))) <= 300

    async def test_no_tree_raises_without_writing(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        hook = _make_hook(None, runtime_dir)

        with pytest.raises(RuntimeError, match="tree not wired"):
            await hook.finally_graph(
                _make_context("a1b2c3d4.worker"),
                AgentResult(content="Done"),
            )

        assert not (runtime_dir / "output").exists()


class TestSubagentAutoSendHookClassify:
    def test_native_error_returns_failure(self) -> None:
        success, issue = SubagentAutoSendHook._classify(
            "completed", "Division by zero", "abc", is_external=False
        )
        assert success is False
        assert "Division by zero" in issue
        assert "last output" in issue.lower()
        assert "invocation_id=abc" in issue

    def test_native_max_iterations_returns_failure(self) -> None:
        success, issue = SubagentAutoSendHook._classify(
            "max_iterations", None, "", is_external=False
        )
        assert success is False
        assert "max_iterations" in issue

    def test_native_loop_detected_returns_failure(self) -> None:
        success, issue = SubagentAutoSendHook._classify(
            "loop_detected", None, "", is_external=False
        )
        assert success is False
        assert "loop" in issue.lower()

    def test_native_timeout_returns_failure(self) -> None:
        success, issue = SubagentAutoSendHook._classify(
            "timeout", None, "", is_external=False
        )
        assert success is False
        assert "timeout" in issue

    def test_native_turn_cancelled_returns_failure(self) -> None:
        success, issue = SubagentAutoSendHook._classify(
            "turn_cancelled", None, "", is_external=False
        )
        assert success is False
        assert "turn_cancelled" in issue

    def test_native_completed_returns_success(self) -> None:
        assert SubagentAutoSendHook._classify(
            "completed", None, "", is_external=False
        ) == (True, "")

    def test_native_cancelled_returns_success(self) -> None:
        assert SubagentAutoSendHook._classify(
            "cancelled", None, "", is_external=False
        ) == (True, "")

    def test_external_error_returns_failure(self) -> None:
        success, issue = SubagentAutoSendHook._classify(
            "completed", "provider crashed", "ext", is_external=True
        )
        assert success is False
        assert "last output" in issue.lower()
        assert "trace" not in issue.lower()

    def test_external_completed_returns_success(self) -> None:
        assert SubagentAutoSendHook._classify(
            "completed", None, "", is_external=True
        ) == (True, "")

    def test_external_max_iterations_returns_success(self) -> None:
        assert SubagentAutoSendHook._classify(
            "max_iterations", None, "", is_external=True
        ) == (True, "")

    def test_external_loop_detected_returns_failure(self) -> None:
        success, issue = SubagentAutoSendHook._classify(
            "loop_detected", None, "ext", is_external=True
        )
        assert success is False
        assert "loop" in issue.lower()

    def test_output_status_is_not_accepted(self) -> None:
        with pytest.raises(TypeError):
            SubagentAutoSendHook._classify(
                "completed",
                None,
                "",
                is_external=False,
                output_status="written",
            )


class TestSubagentAutoSendHookWriteFail:
    async def test_raised_write_failure_still_sends_success_notification(
        self,
        tmp_path: Path,
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        with patch.object(
            hook,
            "_write_output_file",
            side_effect=OSError("disk unavailable"),
        ):
            await hook.finally_graph(
                _make_context("a1b2c3d4.worker"),
                AgentResult(content="Done", stop_reason=StopReason.COMPLETED),
            )

        notification = await _consume_content(bus)
        assert "status: success" in notification
        assert "Issue:" in notification
        assert "Deliverable file write failed" in notification

    async def test_write_failure_omits_output_line(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        with patch.object(
            hook,
            "_write_output_file",
            return_value=(None, "permission denied"),
        ):
            await hook.finally_graph(
                _make_context("a1b2c3d4.worker"),
                AgentResult(content="Done"),
            )

        assert "Output:" not in await _consume_content(bus)

    async def test_write_failure_keeps_truncated_result_body(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        with patch.object(
            hook,
            "_write_output_file",
            return_value=(None, "permission denied"),
        ):
            await hook.finally_graph(
                _make_context("a1b2c3d4.worker"),
                AgentResult(content="x" * 500),
            )

        body = _result_body(await _consume_content(bus))
        assert len(body) <= 300
        assert "[...truncated," in body


class TestSubagentAutoSendHookNotifyTruncate:
    async def test_short_content_is_unmodified(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        await hook.finally_graph(
            _make_context("a1b2c3d4.worker"),
            AgentResult(content="short result"),
        )

        body = _result_body(await _consume_content(bus))
        assert body == "short result"
        assert "truncated" not in body

    async def test_long_content_is_bounded_and_header_has_output(
        self,
        tmp_path: Path,
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)

        await hook.finally_graph(
            _make_context(session_id),
            AgentResult(content="x" * 500),
        )

        notification = await _consume_content(bus)
        assert len(_result_body(notification)) <= 300
        assert (
            f"Output: {runtime_dir / 'output' / session_id / 'OUTPUT_1.md'}"
            in notification
        )

    def test_notify_limit_is_300(self) -> None:
        assert SubagentAutoSendHook.NOTIFY_MAX_RESULT_CHARS == 300


class TestSubagentAutoSendHookExternalBranch:
    async def test_external_does_not_write_output_file(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = _make_hook(
            bus,
            runtime_dir,
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
        )

        await hook.finally_graph(
            _make_context("abc12345.pi_worker", agent_name="pi_worker"),
            AgentResult(content="done", stop_reason=StopReason.COMPLETED),
        )

        assert not (runtime_dir / "output").exists()
        notification = await _consume_content(bus)
        assert "Output:" not in notification
        assert "Trace:" not in notification
        assert "Replied:" not in notification

    async def test_external_completed_is_success_without_ack(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = _make_hook(
            bus,
            runtime_dir,
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
        )

        await hook.finally_graph(
            _make_context("abc12345.pi_worker", agent_name="pi_worker"),
            AgentResult(content="Final answer", stop_reason=StopReason.COMPLETED),
        )

        notification = await _consume_content(bus)
        assert "subagent 'worker'" in notification
        assert "status: success" in notification
        assert "Final answer" in notification
        assert "Issue:" not in notification
        assert "Replied:" not in notification

    async def test_external_error_uses_external_classification(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        bus = _make_bus(tmp_path)
        hook = _make_hook(
            bus,
            runtime_dir,
            execution_strategy=ExecutionStrategyKind.EXTERNAL,
        )

        await hook.finally_graph(
            _make_context("abc12345.pi_worker", agent_name="pi_worker"),
            AgentResult(
                content="",
                stop_reason=StopReason.ERROR,
                error="provider crashed",
            ),
        )

        notification = await _consume_content(bus)
        assert "status: failed" in notification
        assert "provider crashed" in notification
        assert "last output" in notification.lower()
        assert "trace" not in notification.lower()


class TestSubagentAutoSendHookTruncateContent:
    def test_short_content_unchanged(self) -> None:
        assert SubagentAutoSendHook._truncate_content("hello") == "hello"

    def test_long_content_includes_marker_within_limit(self) -> None:
        result = SubagentAutoSendHook._truncate_content("x" * 500)
        assert len(result) <= SubagentAutoSendHook.NOTIFY_MAX_RESULT_CHARS
        assert "[...truncated," in result

    def test_explicit_limit_is_respected(self) -> None:
        result = SubagentAutoSendHook._truncate_content("x" * 100, max_chars=50)
        assert len(result) <= 50
        assert "[...truncated," in result

    def test_think_tags_are_stripped(self) -> None:
        content = "<reasoning>step 1</reasoning><think\ndepth\n</think\nFinal"
        result = SubagentAutoSendHook._truncate_content(content)
        assert "step 1" not in result
        assert "depth" not in result
        assert result == "Final"


class TestSubagentAutoSendHookBuildXml:
    def test_success_native_renders_output_without_status_suffix(self) -> None:
        notification = SubagentAutoSendHook._build_content(
            agent_name="worker",
            invocation_id="abc123",
            success=True,
            result_text="Task done",
            issue="",
            output_path="output/session/OUTPUT_1.md",
        )
        assert "status: success" in notification
        assert "Output: output/session/OUTPUT_1.md" in notification
        assert "Task done" in notification
        assert "Trace:" not in notification
        assert "(written)" not in notification
        assert "(missing)" not in notification

    def test_failure_renders_issue(self) -> None:
        notification = SubagentAutoSendHook._build_content(
            agent_name="worker",
            invocation_id="abc",
            success=False,
            result_text="",
            issue="Subagent crashed with error: timeout",
            output_path="output/session/OUTPUT_1.md",
        )
        assert "status: failed" in notification
        assert "Issue:" in notification
        assert "timeout" in notification

    def test_external_renders_no_native_artifacts(self) -> None:
        notification = SubagentAutoSendHook._build_content(
            agent_name="pi_worker",
            invocation_id="abc",
            success=True,
            result_text="Done",
            issue="",
            replied=None,
        )
        assert "status: success" in notification
        assert "Trace:" not in notification
        assert "Output:" not in notification
        assert "Replied:" not in notification

    def test_special_characters_are_preserved(self) -> None:
        notification = SubagentAutoSendHook._build_content(
            agent_name="worker",
            invocation_id="abc",
            success=False,
            result_text="",
            issue="crashed <with> &special 'chars'",
        )
        assert "crashed <with> &special 'chars'" in notification


class TestSubagentAutoSendHookSuspendResume:
    """One logical subagent turn must produce exactly one notification.

    A subagent turn suspended by approval (GraphInterrupt) re-enters
    ``actual_turn()`` on resume, so FINALLY_GRAPH fires twice: once with
    ``result=None`` on the suspend leg (``react/agent.py`` sets ``result =
    None`` before the finally dispatch — "None signals no turn outcome") and
    once with the real result on completion. A notification fired on the
    suspend leg is delivered to the parent's inbox as a *second*
    ``AGENT_RESULT`` envelope with a fresh ``message_id`` — the inbox's
    message_id dedup cannot collapse it, and the parent consumes both (fold-in
    while busy, poller turn when idle).
    """

    async def test_suspend_then_resume_delivers_exactly_one_notification(
        self, tmp_path: Path
    ) -> None:
        runtime_dir = tmp_path / "runtime"
        session_id = "a1b2c3d4.worker"
        bus = _make_bus(tmp_path)
        hook = _make_hook(bus, runtime_dir)
        ctx = _make_context(session_id)

        # Approval suspend: FINALLY_GRAPH fires with result=None.
        await hook.finally_graph(ctx, None)
        # Approval resume: actual_turn() re-enters and the turn completes.
        await hook.finally_graph(
            ctx, AgentResult(content="done", stop_reason=StopReason.COMPLETED)
        )

        messages = await bus.consume("conv123.main")
        assert len(messages) == 1, (
            "expected exactly one notification after suspend+resume, got "
            f"{len(messages)}: "
            f"{[m.payload.get('content', '')[:80] for m in messages]}"
        )
        assert "done" in messages[0].payload["content"]
