"""Unit tests for :class:`ExternalEventNormalizer` — the shared normalizer.

Covers every cross-transport concern the normalizer owns so no transport
implements them itself:

- Child-session routing: first child event creates the child route
  (discovery resolve → sink factory with a child ``TurnBinding`` →
  background lineage task → env snapshot), later events reuse it.
- ``seq`` stamping: per-session turn counter on ``TurnToolResultEvent``.
- Orphan policy: tool result without a preceding tool call is dropped
  with a warning.
- Turn lifecycle synthesis: ``TurnStartedEvent`` at begin; exactly one
  terminal ``TurnFinishedEvent`` from ``finish``/``fail`` (whichever
  runs second is suppressed).
- Main-text accumulation for the harness ``AgentResult``.
- Cleanup gathers the background discovery tasks.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from modex_agent.agents.external.child_discovery import ChildSessionDiscoverySink
from modex_agent.agents.external.normalizer import (
    ExternalEventNormalizer,
    error_of_backend_result,
    stop_reason_of,
)
from modex_agent.agents.external.paths import ExternalPaths
from modex_agent.agents.external.types import (
    BackendResult,
    BackendStatus,
    ExternalEnvSpec,
)
from modex_agent.core.emitter import (
    TurnBinding,
    TurnEventSink,
    TurnEventSinkFactory,
)
from modex_agent.core.turn_events import (
    StopReason,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnStartedEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingSink(TurnEventSink):
    """Sink capturing every event it observes."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[TurnEvent] = []

    async def _dispatch(self, event: TurnEvent) -> None:
        self.events.append(event)


class _MockSink(ChildSessionDiscoverySink):
    """Deterministic discovery sink — maps provider_child_sid to mock_modex_<id>."""

    def __init__(self) -> None:
        self.discovered: list[tuple[str, str]] = []
        self.resolve_calls: list[str] = []

    async def on_child_discovered(
        self,
        provider_child_session_id: str,
        parent_modex_session_id: str,
        provider_agent_type: str | None = None,
    ) -> str:
        self.discovered.append((provider_child_session_id, parent_modex_session_id))
        return self.resolve_child_modex_session_id(provider_child_session_id)

    def resolve_child_modex_session_id(self, provider_child_session_id: str) -> str:
        self.resolve_calls.append(provider_child_session_id)
        return f"mock_modex_{provider_child_session_id}"


def _make_spec(tmp_path: Path, session_id: str = "pool1.agent1") -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=tmp_path,
        inbox_root=tmp_path / "inbox",
        workdir=tmp_path,
        session_id=session_id,
        agent_name="agent1",
        provider_session_id="prov-initial",
        agent_pool_map={"agent1": "pool1"},
        targets=[],
        modexctl_bin_dir=tmp_path / "bin",
    )


def _make_normalizer(
    tmp_path: Path,
    *,
    sink: RecordingSink,
    modex_sid: str = "pool1.agent1",
    child_sink: ChildSessionDiscoverySink | None = None,
    child_factory: TurnEventSinkFactory | None = None,
) -> ExternalEventNormalizer:
    return ExternalEventNormalizer(
        parent_sink=sink,
        modex_sid=modex_sid,
        paths=ExternalPaths(tmp_path),
        spec=_make_spec(tmp_path, session_id=modex_sid),
        base_env={"PATH": "/usr/bin"},
        child_discovery_sink=child_sink,
        child_sink_factory=child_factory,
    )


# ---------------------------------------------------------------------------
# seq stamping + orphan policy + passthrough
# ---------------------------------------------------------------------------


class TestSeqStamping:
    async def test_tool_results_carry_monotonic_seq(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.on_event(TurnToolCallEvent(tool_name="bash", call_id="c1", arguments={}))
        await normalizer.on_event(
            TurnToolResultEvent(tool_name="bash", call_id="c1", output="one")
        )
        await normalizer.on_event(TurnToolCallEvent(tool_name="bash", call_id="c2", arguments={}))
        await normalizer.on_event(
            TurnToolResultEvent(tool_name="bash", call_id="c2", output="two")
        )

        results = [e for e in sink.events if isinstance(e, TurnToolResultEvent)]
        assert [r.seq for r in results] == [0, 1]

    async def test_seq_is_per_session(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        child_sink = RecordingSink()
        discovery = _MockSink()

        def factory(binding: TurnBinding) -> TurnEventSink:
            return child_sink

        normalizer = _make_normalizer(
            tmp_path, sink=sink, child_sink=discovery, child_factory=factory
        )
        child_cb = normalizer.on_child_event("child_1")

        await normalizer.on_event(TurnToolCallEvent(tool_name="bash", call_id="m1", arguments={}))
        await normalizer.on_event(
            TurnToolResultEvent(tool_name="bash", call_id="m1", output="main")
        )
        await child_cb(TurnToolCallEvent(tool_name="grep", call_id="c1", arguments={}))
        await child_cb(TurnToolResultEvent(tool_name="grep", call_id="c1", output="child"))

        main_seq = [
            e.seq for e in sink.events if isinstance(e, TurnToolResultEvent)
        ]
        child_seq = [
            e.seq for e in child_sink.events if isinstance(e, TurnToolResultEvent)
        ]
        assert main_seq == [0]
        assert child_seq == [0]

    async def test_seq_always_present_on_external_plane(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.on_event(TurnToolCallEvent(tool_name="bash", call_id="c1", arguments={}))
        await normalizer.on_event(
            TurnToolResultEvent(tool_name="bash", call_id="c1", output="x")
        )
        result = [e for e in sink.events if isinstance(e, TurnToolResultEvent)][0]
        assert result.seq is not None


class TestOrphanPolicy:
    async def test_orphan_tool_result_dropped(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.on_event(
            TurnToolResultEvent(tool_name="bash", call_id="ghost", output="late")
        )
        assert not any(isinstance(e, TurnToolResultEvent) for e in sink.events)

    async def test_second_result_for_same_call_is_orphan(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.on_event(TurnToolCallEvent(tool_name="bash", call_id="c1", arguments={}))
        await normalizer.on_event(
            TurnToolResultEvent(tool_name="bash", call_id="c1", output="first")
        )
        await normalizer.on_event(
            TurnToolResultEvent(tool_name="bash", call_id="c1", output="duplicate")
        )
        results = [e for e in sink.events if isinstance(e, TurnToolResultEvent)]
        assert len(results) == 1
        assert results[0].output == "first"


class TestPassthroughAndText:
    async def test_text_reasoning_error_pass_through_unmodified(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        from modex_agent.core.turn_events import TurnReasoningEvent

        await normalizer.on_event(TurnTextEvent(text="hello"))
        await normalizer.on_event(TurnReasoningEvent(text="thinking"))
        await normalizer.on_event(TurnErroredEvent(message="boom"))

        assert sink.events == [
            TurnTextEvent(text="hello"),
            TurnReasoningEvent(text="thinking"),
            TurnErroredEvent(message="boom"),
        ]
        assert normalizer.text == "hello"

    async def test_child_text_not_in_main_accumulator(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        child_sink = RecordingSink()
        discovery = _MockSink()

        def factory(binding: TurnBinding) -> TurnEventSink:
            return child_sink

        normalizer = _make_normalizer(
            tmp_path, sink=sink, child_sink=discovery, child_factory=factory
        )
        await normalizer.on_event(TurnTextEvent(text="main"))
        await normalizer.on_child_event("child_1")(TurnTextEvent(text="child"))
        assert normalizer.text == "main"


# ---------------------------------------------------------------------------
# Child-session routing
# ---------------------------------------------------------------------------


class TestChildRouting:
    def _child_factory(
        self, emitters: dict[str, RecordingSink]
    ) -> TurnEventSinkFactory:
        def factory(binding: TurnBinding) -> TurnEventSink:
            sink = RecordingSink()
            emitters[binding.session_id] = sink
            return sink

        return factory

    async def test_first_child_event_creates_child_sink_with_child_binding(
        self, tmp_path: Path
    ) -> None:
        discovery = _MockSink()
        emitters: dict[str, RecordingSink] = {}
        sink = RecordingSink()
        normalizer = _make_normalizer(
            tmp_path,
            sink=sink,
            child_sink=discovery,
            child_factory=self._child_factory(emitters),
        )

        cb = normalizer.on_child_event("child_1")
        await cb(TurnTextEvent(text="child text"))

        # Child sink was created bound to the resolved child modex session id.
        assert "mock_modex_child_1" in emitters
        assert emitters["mock_modex_child_1"].events == [TurnTextEvent(text="child text")]
        # Main sink saw nothing of the child.
        assert sink.events == []
        # Discovery resolution ran synchronously before delivery.
        assert discovery.resolve_calls == ["child_1"]

    async def test_child_env_snapshot_written_on_discovery(self, tmp_path: Path) -> None:
        discovery = _MockSink()
        emitters: dict[str, RecordingSink] = {}
        sink = RecordingSink()
        normalizer = _make_normalizer(
            tmp_path,
            sink=sink,
            modex_sid="pool1.agent1",
            child_sink=discovery,
            child_factory=self._child_factory(emitters),
        )

        await normalizer.on_child_event("child_1")(TurnTextEvent(text="x"))

        snapshot = json.loads(
            ExternalPaths(tmp_path).env_snapshot_for_session("child_1").read_text("utf-8")
        )
        assert snapshot["MODEX_SESSION_ID"] == "mock_modex_child_1"
        assert snapshot["MODEX_PARENT_SESSION_ID"] == "pool1.agent1"

    async def test_no_collaborators_drops_child_events(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)

        await normalizer.on_child_event("child_1")(TurnTextEvent(text="child text"))

        assert sink.events == []

    async def test_cleanup_gathers_background_registration(self, tmp_path: Path) -> None:
        release = asyncio.Event()

        class _BlockingSink(ChildSessionDiscoverySink):
            async def on_child_discovered(
                self,
                provider_child_session_id: str,
                parent_modex_session_id: str,
                provider_agent_type: str | None = None,
            ) -> str:
                await release.wait()
                return self.resolve_child_modex_session_id(provider_child_session_id)

            def resolve_child_modex_session_id(self, provider_child_session_id: str) -> str:
                return f"mock_modex_{provider_child_session_id}"

        emitters: dict[str, RecordingSink] = {}
        sink = RecordingSink()
        normalizer = _make_normalizer(
            tmp_path,
            sink=sink,
            child_sink=_BlockingSink(),
            child_factory=self._child_factory(emitters),
        )
        await normalizer.on_child_event("child_1")(TurnTextEvent(text="x"))

        cleanup_task = asyncio.create_task(normalizer.cleanup())
        await asyncio.sleep(0.05)
        assert not cleanup_task.done(), "cleanup returned before background registration"

        release.set()
        await asyncio.wait_for(cleanup_task, timeout=2.0)


# ---------------------------------------------------------------------------
# Turn lifecycle synthesis
# ---------------------------------------------------------------------------


class TestLifecycleSynthesis:
    async def test_begin_emits_turn_started(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.begin()
        assert sink.events == [TurnStartedEvent()]

    async def test_finish_emits_exactly_one_terminal(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.begin()
        await normalizer.finish(
            BackendResult(status=BackendStatus.COMPLETED, session_id="s1")
        )
        await normalizer.finish(
            BackendResult(status=BackendStatus.FAILED, session_id="s1", error="late")
        )

        terminals = [e for e in sink.events if isinstance(e, TurnFinishedEvent)]
        assert len(terminals) == 1
        assert terminals[0].stop_reason is StopReason.COMPLETED
        assert terminals[0].error is None

    @pytest.mark.parametrize(
        ("status", "stop_reason"),
        [
            (BackendStatus.COMPLETED, StopReason.COMPLETED),
            (BackendStatus.FAILED, StopReason.ERROR),
            (BackendStatus.TIMEOUT, StopReason.TIMEOUT),
            (BackendStatus.ABORTED, StopReason.CANCELLED),
        ],
    )
    async def test_terminal_status_mapping(
        self, tmp_path: Path, status: BackendStatus, stop_reason: StopReason
    ) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.finish(BackendResult(status=status, session_id="s1"))
        terminal = [e for e in sink.events if isinstance(e, TurnFinishedEvent)][0]
        assert terminal.stop_reason is stop_reason

    async def test_failed_status_without_error_synthesizes_message(
        self, tmp_path: Path
    ) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.finish(BackendResult(status=BackendStatus.TIMEOUT, session_id="s1"))
        terminal = [e for e in sink.events if isinstance(e, TurnFinishedEvent)][0]
        assert terminal.error == "provider exited with status timeout"

    async def test_fail_emits_error_observation_then_terminal(self, tmp_path: Path) -> None:
        sink = RecordingSink()
        normalizer = _make_normalizer(tmp_path, sink=sink)
        await normalizer.begin()
        await normalizer.fail("boom")
        await normalizer.finish(BackendResult(status=BackendStatus.COMPLETED, session_id="s"))

        assert sink.events[-2] == TurnErroredEvent(message="boom")
        terminal = sink.events[-1]
        assert isinstance(terminal, TurnFinishedEvent)
        assert terminal.stop_reason is StopReason.ERROR
        assert terminal.error == "boom"
        assert len([e for e in sink.events if isinstance(e, TurnFinishedEvent)]) == 1


# ---------------------------------------------------------------------------
# Pure mapping helpers
# ---------------------------------------------------------------------------


class TestMappingHelpers:
    def test_stop_reason_of_covers_closed_set(self) -> None:
        assert stop_reason_of(BackendStatus.COMPLETED) is StopReason.COMPLETED
        assert stop_reason_of(BackendStatus.FAILED) is StopReason.ERROR
        assert stop_reason_of(BackendStatus.TIMEOUT) is StopReason.TIMEOUT
        assert stop_reason_of(BackendStatus.ABORTED) is StopReason.CANCELLED

    def test_error_of_completed_passthrough(self) -> None:
        assert error_of_backend_result(BackendResult(status=BackendStatus.COMPLETED)) is None

    def test_error_of_failure_defaults(self) -> None:
        assert (
            error_of_backend_result(BackendResult(status=BackendStatus.FAILED))
            == "provider exited with status failed"
        )
