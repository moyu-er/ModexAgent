"""Unit tests for :class:`ExternalAgent` and the streaming contract.

Covers every T5 acceptance criterion:

- ``StreamingProviderBackend`` ABC shape (abstract, not directly
  instantiable).
- :class:`ScriptedStreamingAdapter` fans parsed emissions through the
  callback.
- Full-turn integration: a 3-event programme (text + tool_use +
  tool_result) drives the harness end-to-end; the emitter sees all
  three, the transcript persists the accumulated text, and the
  backend's recorded env carries all 9 ``MODEX_*`` vars (8 ``MODEX_*``
  keys plus the recreated ``PATH``).
- ``current_agent_context`` set/reset symmetry (token reset in
  ``finally``).
- Stale-session recovery: backend raises → invalidate → single fresh
  retry.
- Outbound send intent exercised in-process by constructing the same
  ``OutboxLine`` shape the agent emits when it calls ``modexctl send``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modex_agent.agents.external.agent import (
    ExternalAgent,
    ScriptedStreamingAdapter,
    StaleSessionError,
    StreamingProviderBackend,
)
from modex_agent.agents.external.backend_provider import (
    BackendProvider,
    PoolScopedBackendProvider,
    TurnContext,
)
from modex_agent.agents.external.contracts import ProviderEventParser
from modex_agent.agents.external.events import ExternalEvent
from modex_agent.agents.external.paths import ExternalPaths
from modex_agent.agents.external.scripted_backend import (
    ScriptedProgramme,
    ScriptedProviderBackend,
    ScriptedStep,
)
from modex_agent.agents.external.session_store import LocalFileExternalSessionMapStore
from modex_agent.agents.external.types import (
    BackendResult,
    BackendStatus,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
)
from modex_agent.core.agent import (
    AgentContext,
    AgentImplementation,
    ProviderKind,
    current_agent_context,
)
from modex_agent.core.emitter import AgentResult, ContentEmitter, StopReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.message_format import SourceLabel, build_agent_comm_message
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.tools.manager import InMemoryToolManager

_MODEX_ENV_KEYS = (
    "MODEX_WORKSPACE_ROOT",
    "MODEX_INBOX_ROOT",
    "MODEX_WORKDIR",
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_PROVIDER_SESSION_ID",
    "MODEX_AGENT_POOL_MAP",
    "MODEX_TARGETS",
    "MODEX_COMM_KIND",
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _PiCompatibleParser(ProviderEventParser):
    """Parses Pi-style JSONL lines — replacement for deleted PiEventParser."""

    def parse_line(self, line: str) -> Iterator[Emission]:
        line = line.strip()
        if not line:
            return iter(())
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return iter(())
        event_type = data.get("type")
        if event_type == "message_update":
            update = data.get("update", {})
            if "text_delta" in update:
                return iter([Emission(event=ExternalEvent.TEXT_DELTA, text=update["text_delta"])])
        elif event_type == "tool_execution_start":
            return iter(
                [
                    Emission(
                        event=ExternalEvent.TOOL_USE,
                        tool_name=data.get("tool_name", ""),
                        tool_input=json.dumps(data.get("args", {})),
                        call_id=data.get("tool_call_id"),
                    )
                ]
            )
        elif event_type == "tool_execution_end":
            return iter(
                [
                    Emission(
                        event=ExternalEvent.TOOL_RESULT,
                        call_id=data.get("tool_call_id"),
                        output=data.get("result"),
                    )
                ]
            )
        return iter(())


class RecordingEmitter(ContentEmitter[ExternalEvent]):  # type: ignore[type-arg]
    """Duck-typed emitter capturing deltas, events, completes, errors."""

    def __init__(self) -> None:
        super().__init__()
        self.deltas: list[str] = []
        self.events: list[tuple[ExternalEvent, object]] = []
        self.turn_events: list[TurnEvent] = []
        self.completed: AgentResult | None = None
        self.errors: list[str] = []

    def wants_streaming(self) -> bool:
        return False

    async def emit(self, event: ExternalEvent, data: object | None = None) -> None:
        self.events.append((event, data))

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_turn_event(self, event: TurnEvent) -> None:
        self.turn_events.append(event)

    async def emit_content(self, full_content: str) -> None:
        self.deltas.append(full_content)

    async def emit_stream_end(self, resuming: bool = False) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        self.completed = result

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)

    async def flush(self) -> None:
        pass


def _make_spec(workdir: Path, session_id: str = "pool1.agent1") -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=workdir,
        inbox_root=workdir / "inbox",
        workdir=workdir,
        session_id=session_id,
        agent_name="agent1",
        provider_session_id="prov-initial",
        agent_pool_map={"agent1": "pool1", "helper": "pool1"},
        targets=[("helper", "a helper agent")],
        modexctl_bin_dir=workdir / "bin",
    )


def _pool_provider(backend: StreamingProviderBackend) -> PoolScopedBackendProvider:
    """Wrap a backend in the default pool-scoped provider (T2 migration helper)."""
    return PoolScopedBackendProvider(backend)


def _make_ctx(session_id: str = "pool1.agent1") -> AgentContext:
    history = ListMessageHistory([ChatMessage(role="user", content="Please list the files.")])
    return AgentContext(
        system_prompt="",
        history=history,
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
        current_input="Please list the files.",
    )


def _pi_text_step(text: str) -> ScriptedStep:
    return ScriptedStep(text=json.dumps({"type": "message_update", "update": {"text_delta": text}}))


def _pi_tool_use_step() -> ScriptedStep:
    return ScriptedStep(
        text=json.dumps(
            {
                "type": "tool_execution_start",
                "tool_name": "bash",
                "tool_call_id": "call-1",
                "args": {"cmd": "ls"},
            }
        )
    )


def _pi_tool_result_step() -> ScriptedStep:
    return ScriptedStep(
        text=json.dumps(
            {
                "type": "tool_execution_end",
                "tool_call_id": "call-1",
                "result": "file.txt",
            }
        )
    )


# ---------------------------------------------------------------------------
# StreamingProviderBackend ABC
# ---------------------------------------------------------------------------


class TestStreamingProviderBackendABC:
    def test_is_subclass_of_provider_backend(self) -> None:
        from modex_agent.agents.external.contracts import ProviderBackend

        assert issubclass(StreamingProviderBackend, ProviderBackend)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            StreamingProviderBackend()  # type: ignore[abstract]

    def test_subclass_without_execute_streaming_is_abstract(self) -> None:
        class Half(StreamingProviderBackend):
            pass

        with pytest.raises(TypeError):
            Half()  # type: ignore[abstract]

    def test_inherited_execute_delegates_to_execute_streaming(self, tmp_path: Path) -> None:
        driven: list[ExecOptions] = []

        class Concrete(StreamingProviderBackend):
            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                driven.append(opts)
                return BackendResult(status="completed", session_id="s1")

        result = asyncio.run(Concrete().execute(ExecOptions(prompt="x", workdir=tmp_path)))
        assert result.status == "completed"
        assert len(driven) == 1


# ---------------------------------------------------------------------------
# ScriptedStreamingAdapter
# ---------------------------------------------------------------------------


class TestScriptedStreamingAdapter:
    @pytest.mark.asyncio
    async def test_emits_one_emission_per_parsed_line(self, tmp_path: Path) -> None:
        steps = (_pi_text_step("Hello"),)
        scripted = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
        received: list[Emission] = []

        async def on_emission(e: Emission) -> None:
            received.append(e)

        opts = ExecOptions(prompt="hi", workdir=tmp_path)
        result = await adapter.execute_streaming(opts, {"PATH": "/x"}, on_emission)
        assert len(received) == 1
        assert received[0].event is ExternalEvent.TEXT_DELTA
        assert received[0].text == "Hello"
        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_records_opts_and_env(self, tmp_path: Path) -> None:
        scripted = ScriptedProviderBackend(ScriptedProgramme())
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())

        async def on_emission(_e: Emission) -> None:
            pass

        opts = ExecOptions(prompt="hi", workdir=tmp_path)
        env = {"MODEX_SESSION_ID": "s", "PATH": "/p"}
        await adapter.execute_streaming(opts, env, on_emission)
        assert adapter.recorded_opts == [opts]
        assert adapter.recorded_envs == [env]

    @pytest.mark.asyncio
    async def test_side_effect_fires_at_marked_step(self, tmp_path: Path) -> None:
        fired: list[ExecOptions] = []

        async def side_effect(opts: ExecOptions) -> None:
            fired.append(opts)

        steps = (
            _pi_text_step("first"),
            ScriptedStep(text="{}", side_effect=True),
            _pi_text_step("third"),
        )
        scripted = ScriptedProviderBackend(ScriptedProgramme(steps=steps))
        adapter = ScriptedStreamingAdapter(
            scripted, _PiCompatibleParser(), send_side_effect=side_effect
        )

        async def on_emission(_e: Emission) -> None:
            pass

        opts = ExecOptions(prompt="hi", workdir=tmp_path)
        await adapter.execute_streaming(opts, {}, on_emission)
        assert len(fired) == 1
        assert fired[0] is opts


# ---------------------------------------------------------------------------
# Full-turn integration (the headline T5 acceptance test)
# ---------------------------------------------------------------------------


class TestExternalAgentFullTurn:
    @pytest.mark.asyncio
    async def test_three_event_turn_emits_and_persists(self, tmp_path: Path) -> None:
        steps = (
            _pi_text_step("Hello world"),
            _pi_tool_use_step(),
            _pi_tool_result_step(),
        )
        scripted = ScriptedProviderBackend(
            ScriptedProgramme(steps=steps, status="completed", session_id="prov-sess-1")
        )
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
        spec = _make_spec(tmp_path)
        paths = ExternalPaths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        agent = ExternalAgent(
            backend_provider=_pool_provider(adapter),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )
        ctx = _make_ctx()
        emitter = RecordingEmitter()

        result = await agent.run(ctx, emitter)

        # AgentResult mapping.
        assert isinstance(result, AgentResult)
        assert result.stop_reason == StopReason.COMPLETED
        assert result.content == "Hello world"

        # Provider emissions are translated to the canonical core seam.
        assert emitter.turn_events[0] == TurnTextEvent(text="Hello world")
        assert emitter.turn_events[1] == TurnToolCallEvent(
            tool_name="bash", call_id="call-1", arguments={"cmd": "ls"}
        )
        assert emitter.turn_events[2] == TurnToolResultEvent(
            tool_name="bash", call_id="call-1", output="file.txt"
        )

        # External agent memory is NOT persisted locally — the provider
        # owns its own session state. Only the original user message remains.
        messages = await ctx.history.to_list()
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 0

        # Backend recorded the env with all 9 MODEX_* vars (8 MODEX_* + PATH).
        assert len(adapter.recorded_envs) == 1
        recorded_env = adapter.recorded_envs[0]
        for key in _MODEX_ENV_KEYS:
            assert key in recorded_env, f"missing {key}"
        assert "PATH" in recorded_env
        assert str(spec.modexctl_bin_dir) in recorded_env["PATH"]

        # Session committed for resume.
        provider_sid, is_resume = store.resolve(str(ctx.session))
        assert provider_sid == "prov-sess-1"
        assert is_resume is True

        # Emitter was notified of completion.
        assert emitter.completed is not None

        # AGENTS.md marker block was written into the workdir.
        agents_md = paths.agents_md.read_text(encoding="utf-8")
        assert "BEGIN MODEX-RUNTIME" in agents_md
        assert "modexctl send" in agents_md

    @pytest.mark.asyncio
    async def test_provider_emission_renews_dispatch_deadline(self, tmp_path: Path) -> None:
        """Every provider emission is an activity signal: on_emission renews
        the dispatch deadline by its default amount (chunk_renew_seconds) —
        same watchdog protocol as ReAct stream chunks."""
        from modex_agent.runtime.dispatch import (
            DispatchDeadline,
            current_dispatch_deadline,
        )

        steps = (
            _pi_text_step("Hello world"),
            _pi_tool_use_step(),
            _pi_tool_result_step(),
        )
        scripted = ScriptedProviderBackend(
            ScriptedProgramme(steps=steps, status="completed", session_id="prov-sess-2")
        )
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        agent = ExternalAgent(
            backend_provider=_pool_provider(adapter),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
            base_env={"PATH": "/usr/bin"},
        )
        ctx = _make_ctx()
        emitter = RecordingEmitter()

        deadline = DispatchDeadline(initial_timeout=0.0, max_ahead_seconds=600.0)
        token = current_dispatch_deadline.set(deadline)
        try:
            assert deadline.is_expired
            await agent.run(ctx, emitter)
            assert not deadline.is_expired
            assert 2.5 <= deadline.remaining <= 3.1  # default 3s renewal
        finally:
            current_dispatch_deadline.reset(token)


class TestAgentsMdIdempotency:
    @pytest.mark.asyncio
    async def test_second_turn_does_not_rewrite(self, tmp_path: Path) -> None:
        scripted = ScriptedProviderBackend(
            ScriptedProgramme(steps=[_pi_text_step("ok")], session_id="prov-1")
        )
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        agent = ExternalAgent(
            backend_provider=_pool_provider(adapter),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )

        await agent.run(_make_ctx(), RecordingEmitter())
        paths = ExternalPaths(tmp_path)
        mtime1 = paths.agents_md.stat().st_mtime_ns

        await agent.run(_make_ctx(), RecordingEmitter())
        mtime2 = paths.agents_md.stat().st_mtime_ns

        assert mtime2 == mtime1


class TestExternalAgentStop:
    @pytest.mark.asyncio
    async def test_concurrent_stop_callers_share_successful_close(self, tmp_path: Path) -> None:
        # Given
        class _BlockingBackend(StreamingProviderBackend):
            def __init__(self) -> None:
                self.close_calls = 0
                self.close_started = asyncio.Event()
                self.release_close = asyncio.Event()

            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                return BackendResult(status=BackendStatus.COMPLETED)

            async def close(self) -> None:
                self.close_calls += 1
                self.close_started.set()
                await self.release_close.wait()

        backend = _BlockingBackend()
        agent = ExternalAgent(
            backend_provider=_pool_provider(backend),
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
        )
        second_caller_started = asyncio.Event()

        async def stop_from_second_caller() -> None:
            second_caller_started.set()
            await agent.stop()

        first_stop = asyncio.create_task(agent.stop())
        await backend.close_started.wait()
        second_stop = asyncio.create_task(stop_from_second_caller())
        await second_caller_started.wait()

        # When
        backend.release_close.set()
        results = await asyncio.gather(first_stop, second_stop)

        # Then
        assert results == [None, None]
        assert backend.close_calls == 1
        assert agent._stopped is True

    @pytest.mark.asyncio
    async def test_concurrent_stop_callers_share_failure_then_retry(self, tmp_path: Path) -> None:
        # Given
        failure = RuntimeError("close failed")

        class _BlockingFailOnceBackend(StreamingProviderBackend):
            def __init__(self) -> None:
                self.close_calls = 0
                self.close_started = asyncio.Event()
                self.release_failure = asyncio.Event()

            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                return BackendResult(status=BackendStatus.COMPLETED)

            async def close(self) -> None:
                self.close_calls += 1
                if not self.release_failure.is_set():
                    self.close_started.set()
                    await self.release_failure.wait()
                    raise failure

        backend = _BlockingFailOnceBackend()
        agent = ExternalAgent(
            backend_provider=_pool_provider(backend),
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
        )
        second_caller_started = asyncio.Event()

        async def stop_from_second_caller() -> None:
            second_caller_started.set()
            await agent.stop()

        first_stop = asyncio.create_task(agent.stop())
        await backend.close_started.wait()
        second_stop = asyncio.create_task(stop_from_second_caller())
        await second_caller_started.wait()

        # When
        backend.release_failure.set()
        with pytest.raises(RuntimeError) as first_error:
            await first_stop
        with pytest.raises(RuntimeError) as second_error:
            await second_stop
        calls_after_failure = backend.close_calls
        await agent.stop()

        # Then
        assert first_error.value is failure
        assert second_error.value is failure
        assert calls_after_failure == 1
        assert backend.close_calls == 2
        assert agent._stopped is True

    @pytest.mark.asyncio
    async def test_failed_backend_close_can_be_retried(self, tmp_path: Path) -> None:
        # Given
        class _FailOnceBackend(StreamingProviderBackend):
            def __init__(self) -> None:
                self.close_calls = 0

            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                return BackendResult(status=BackendStatus.COMPLETED)

            async def close(self) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("close failed")

        backend = _FailOnceBackend()
        agent = ExternalAgent(
            backend_provider=_pool_provider(backend),
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
        )

        # When
        with pytest.raises(RuntimeError, match="close failed"):
            await agent.stop()
        stopped_after_failure = agent._stopped
        await agent.stop()

        # Then
        assert (stopped_after_failure, backend.close_calls, agent._stopped) == (False, 2, True)

    @pytest.mark.asyncio
    async def test_cancelled_backend_close_can_be_retried(self, tmp_path: Path) -> None:
        # Given
        class _CancelledOnceBackend(StreamingProviderBackend):
            def __init__(self) -> None:
                self.close_calls = 0
                self.close_started = asyncio.Event()

            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                return BackendResult(status=BackendStatus.COMPLETED)

            async def close(self) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    self.close_started.set()
                    await asyncio.Event().wait()

        backend = _CancelledOnceBackend()
        agent = ExternalAgent(
            backend_provider=_pool_provider(backend),
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
        )
        first_stop = asyncio.create_task(agent.stop())
        await backend.close_started.wait()

        # When
        first_stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_stop
        stopped_after_cancellation = agent._stopped
        await agent.stop()

        # Then
        assert (stopped_after_cancellation, backend.close_calls, agent._stopped) == (
            False,
            2,
            True,
        )


# ---------------------------------------------------------------------------
# current_agent_context set/reset symmetry
# ---------------------------------------------------------------------------


class TestCurrentAgentContextLifecycle:
    @pytest.mark.asyncio
    async def test_context_set_during_turn_reset_after(self, tmp_path: Path) -> None:
        captured: list[AgentContext | None] = []

        async def side_effect(_opts: ExecOptions) -> None:
            captured.append(current_agent_context.get(None))

        steps = (
            _pi_text_step("hi"),
            ScriptedStep(text="{}", side_effect=True),
        )
        scripted = ScriptedProviderBackend(ScriptedProgramme(steps=steps, session_id="prov-1"))
        adapter_with_fx = ScriptedStreamingAdapter(
            scripted, _PiCompatibleParser(), send_side_effect=side_effect
        )
        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        agent = ExternalAgent(
            backend_provider=_pool_provider(adapter_with_fx),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )
        ctx = _make_ctx()
        emitter = RecordingEmitter()

        await agent.run(ctx, emitter)

        # The side-effect ran inside the turn, where the context was set.
        assert len(captured) == 1
        assert captured[0] is ctx
        # After the turn, the context token was reset.
        assert current_agent_context.get(None) is None

    @pytest.mark.asyncio
    async def test_context_reset_even_on_error(self, tmp_path: Path) -> None:
        # A backend that always raises should still let the finally
        # block reset the context token.
        class _AlwaysFailing(StreamingProviderBackend):
            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                raise RuntimeError("boom")

        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        agent = ExternalAgent(
            backend_provider=_pool_provider(_AlwaysFailing()),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )
        ctx = _make_ctx()
        emitter = RecordingEmitter()

        result = await agent.run(ctx, emitter)

        assert result.stop_reason == StopReason.ERROR
        assert result.error is not None
        assert current_agent_context.get(None) is None
        assert emitter.completed is not None


# ---------------------------------------------------------------------------
# Stale-session recovery
# ---------------------------------------------------------------------------


class TestStaleSessionRecovery:
    @pytest.mark.asyncio
    async def test_stale_session_invalidates_and_retries_fresh(self, tmp_path: Path) -> None:
        class _FlakyBackend(StreamingProviderBackend):
            def __init__(self) -> None:
                self.calls = 0
                self.resume_ids: list[str | None] = []

            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                self.calls += 1
                self.resume_ids.append(opts.resume_session_id)
                if self.calls == 1:
                    raise StaleSessionError("provider session expired")
                return BackendResult(status="completed", session_id="prov-recovered")

        spec = _make_spec(tmp_path)
        paths = ExternalPaths(tmp_path)
        store = LocalFileExternalSessionMapStore(paths)
        modex_sid = "pool1.agent1"
        # Pre-seed a stale mapping.
        await store.commit(modex_sid, "prov-old", ProviderKind.PI)

        backend = _FlakyBackend()
        agent = ExternalAgent(
            backend_provider=_pool_provider(backend),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )
        ctx = _make_ctx()
        emitter = RecordingEmitter()

        result = await agent.run(ctx, emitter)

        # Exactly two attempts: first with the stale id, second fresh.
        assert backend.calls == 2
        assert backend.resume_ids == ["prov-old", None]
        # Recovered session committed.
        provider_sid, is_resume = store.resolve(modex_sid)
        assert provider_sid == "prov-recovered"
        assert is_resume is True
        assert result.stop_reason == StopReason.COMPLETED

    @pytest.mark.asyncio
    async def test_second_failure_propagates(self, tmp_path: Path) -> None:
        class _AlwaysStale(StreamingProviderBackend):
            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                raise StaleSessionError("still stale")

        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        agent = ExternalAgent(
            backend_provider=_pool_provider(_AlwaysStale()),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )
        ctx = _make_ctx()
        emitter = RecordingEmitter()

        result = await agent.run(ctx, emitter)
        # The retry also raised → the harness surfaces it as an error result.
        assert result.stop_reason == StopReason.ERROR
        assert result.error is not None


# ---------------------------------------------------------------------------
# Outbound send intent via T2 routing (same code path the CLI uses)
# ---------------------------------------------------------------------------


class TestOutboundSendViaRouting:
    @pytest.mark.asyncio
    async def test_side_effect_calls_t2_build_inbox_line(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, session_id="pool1.agent1")
        built: list[str] = []

        async def side_effect(_opts: ExecOptions) -> None:
            xml_content = build_agent_comm_message(
                source_label=SourceLabel.PEER_AGENT,
                source=spec.agent_name,
                content="hello from agent1",
                reply_contract=AgentImplementation.EXTERNAL,
            )
            import json as _json
            line_dict = {
                "message_id": "test-id",
                "source": spec.agent_name,
                "content": xml_content,
                "message_type": AgentMessageType.AGENT_MESSAGE.value,
                "timestamp": datetime.now(UTC).isoformat(),
                "metadata": {
                    "agent_session_id": "pool1.helper",
                    "session_id": spec.session_id,
                    "invocation_id": "pool1",
                    "parent_session_id": None,
                },
            }
            built.append(_json.dumps(line_dict))

        steps = (
            _pi_text_step("sending a message"),
            ScriptedStep(text="{}", side_effect=True),
        )
        scripted = ScriptedProviderBackend(ScriptedProgramme(steps=steps, session_id="prov-1"))
        adapter = ScriptedStreamingAdapter(
            scripted, _PiCompatibleParser(), send_side_effect=side_effect
        )
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))
        agent = ExternalAgent(
            backend_provider=_pool_provider(adapter),
            session_store=store,
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=spec,
            base_env={"PATH": "/usr/bin"},
        )
        ctx = _make_ctx()
        emitter = RecordingEmitter()

        await agent.run(ctx, emitter)

        # The T2 routing function ran in-process and produced a line
        # shaped exactly as the inbox server expects.
        assert len(built) == 1
        line = json.loads(built[0])
        assert line["source"] == "agent1"
        assert "Message from peer agent" in line["content"]
        assert "hello from agent1" in line["content"]
        assert line["metadata"]["agent_session_id"] == "pool1.helper"
        assert line["metadata"]["session_id"] == "pool1.agent1"


# ---------------------------------------------------------------------------
# Minimal builder
# ---------------------------------------------------------------------------


class TestExternalAgentBuilder:
    def test_build_requires_all_collaborators(self, tmp_path: Path) -> None:
        from modex_agent.agents.external.builder import (
            ExternalAgentBuilder,
        )

        builder = ExternalAgentBuilder()
        with pytest.raises(ValueError, match="missing required"):
            builder.build()

    @pytest.mark.asyncio
    async def test_build_assembles_agent(self, tmp_path: Path) -> None:
        from modex_agent.agents.external.builder import (
            ExternalAgentBuilder,
        )

        scripted = ScriptedProviderBackend(ScriptedProgramme(session_id="prov-1"))
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
        spec = _make_spec(tmp_path)
        store = LocalFileExternalSessionMapStore(ExternalPaths(tmp_path))

        agent = (
            ExternalAgentBuilder()
            .with_backend_provider(_pool_provider(adapter))
            .with_session_store(store)
            .with_parser(_PiCompatibleParser())
            .with_provider_kind(ProviderKind.PI)
            .with_spec(spec)
            .with_base_env({"PATH": "/usr/bin"})
            .build()
        )
        assert isinstance(agent, ExternalAgent)
        assert agent.name == "ExternalAgent"


# ---------------------------------------------------------------------------
# ExecOptions frozen-model sanity (regression guard for model_copy in retry)
# ---------------------------------------------------------------------------


class TestExecOptionsRetryCopy:
    def test_model_copy_update_resume_session_id(self, tmp_path: Path) -> None:
        opts = ExecOptions(prompt="x", workdir=tmp_path, resume_session_id="old")
        retried = opts.model_copy(update={"resume_session_id": None})
        assert retried.resume_session_id is None
        assert retried.prompt == "x"
        # Original is untouched (frozen semantics via copy).
        assert opts.resume_session_id == "old"


# ---------------------------------------------------------------------------
# Seam 3 — BackendProvider acquire/release lifecycle (ADR-0027, T2)
# ---------------------------------------------------------------------------


class _RecordingProvider(BackendProvider):
    """BackendProvider test double that records every call.

    ``acquire`` returns the wrapped backend so the agent can drive a real
    turn. ``release`` and ``close_all`` record their arguments so a test
    can assert ordering, ``turn_failed`` propagation, and shutdown
    routing without observing the backend itself.
    """

    def __init__(self, backend: StreamingProviderBackend) -> None:
        self._backend = backend
        self.acquire_calls: list[tuple[str, TurnContext]] = []
        self.release_calls: list[tuple[StreamingProviderBackend, bool]] = []
        self.close_all_calls: int = 0

    async def acquire(
        self, modex_session_id: str, turn_context: TurnContext
    ) -> StreamingProviderBackend:
        self.acquire_calls.append((modex_session_id, turn_context))
        return self._backend

    async def release(self, backend: StreamingProviderBackend, *, turn_failed: bool) -> None:
        self.release_calls.append((backend, turn_failed))

    async def close_all(self) -> None:
        self.close_all_calls += 1


class _FailingBackend(StreamingProviderBackend):
    """Backend whose ``execute_streaming`` always raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        raise self._exc


class _CountingCloseBackend(StreamingProviderBackend):
    """Backend that counts ``close()`` calls — for PoolScopedBackendProvider tests."""

    def __init__(self) -> None:
        self.close_calls = 0

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        return BackendResult(status=BackendStatus.COMPLETED)

    async def close(self) -> None:
        self.close_calls += 1


class TestRunTurnAcquireReleaseLifecycle:
    """Seam 3 — ``_run_turn`` borrows a backend per turn via the provider."""

    @pytest.mark.asyncio
    async def test_acquire_called_once_before_execute_streaming(self, tmp_path: Path) -> None:
        scripted = ScriptedProviderBackend(
            ScriptedProgramme(steps=(_pi_text_step("hi"),), session_id="prov-1")
        )
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
        provider = _RecordingProvider(adapter)
        agent = ExternalAgent(
            backend_provider=provider,
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
            base_env={"PATH": "/usr/bin"},
        )

        await agent.run(_make_ctx(), RecordingEmitter())

        # acquire ran exactly once, before execute_streaming recorded its opts.
        assert len(provider.acquire_calls) == 1
        modex_sid, turn_context = provider.acquire_calls[0]
        assert modex_sid == "pool1.agent1"
        assert turn_context.provider_kind is ProviderKind.PI
        assert turn_context.workdir == tmp_path
        # The backend was actually used (the adapter recorded the call).
        assert len(adapter.recorded_opts) == 1

    @pytest.mark.asyncio
    async def test_release_turn_failed_false_on_success_path(self, tmp_path: Path) -> None:
        scripted = ScriptedProviderBackend(
            ScriptedProgramme(steps=(_pi_text_step("hi"),), session_id="prov-1")
        )
        adapter = ScriptedStreamingAdapter(scripted, _PiCompatibleParser())
        provider = _RecordingProvider(adapter)
        agent = ExternalAgent(
            backend_provider=provider,
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
            base_env={"PATH": "/usr/bin"},
        )

        await agent.run(_make_ctx(), RecordingEmitter())

        # release ran exactly once with turn_failed=False on the success path.
        assert len(provider.release_calls) == 1
        released_backend, turn_failed = provider.release_calls[0]
        assert released_backend is adapter
        assert turn_failed is False

    @pytest.mark.asyncio
    async def test_release_turn_failed_true_on_exception_path(self, tmp_path: Path) -> None:
        # A backend whose execute_streaming always raises a non-stale error.
        boom = RuntimeError("boom")
        provider = _RecordingProvider(_FailingBackend(boom))
        agent = ExternalAgent(
            backend_provider=provider,
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
            base_env={"PATH": "/usr/bin"},
        )

        result = await agent.run(_make_ctx(), RecordingEmitter())

        # The turn surfaced as an error result (the harness catches the
        # exception and emits an AgentResult with stop_reason=ERROR).
        assert result.stop_reason == StopReason.ERROR
        # release was called with turn_failed=True in the finally block,
        # even though the turn failed before reaching _execute_with_retry's
        # own StaleSessionError retry.
        assert len(provider.release_calls) == 1
        _, turn_failed = provider.release_calls[0]
        assert turn_failed is True


class TestStopCallsCloseAll:
    """Seam 3 — ``stop()`` routes through ``BackendProvider.close_all``."""

    @pytest.mark.asyncio
    async def test_stop_calls_provider_close_all(self, tmp_path: Path) -> None:
        backend = _CountingCloseBackend()
        provider = _RecordingProvider(backend)
        agent = ExternalAgent(
            backend_provider=provider,
            session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
            parser=_PiCompatibleParser(),
            provider_kind=ProviderKind.PI,
            spec=_make_spec(tmp_path),
        )

        await agent.stop()

        # close_all was called; the underlying backend.close() was NOT
        # called directly by the agent (the provider owns that routing).
        assert provider.close_all_calls == 1
        assert backend.close_calls == 0
        assert agent._stopped is True


class TestPoolScopedBackendProvider:
    """Seam 3 — the main-agent provider implementation contract."""

    @pytest.mark.asyncio
    async def test_acquire_returns_same_backend_every_time(self, tmp_path: Path) -> None:
        backend = _CountingCloseBackend()
        provider = PoolScopedBackendProvider(backend)
        ctx = TurnContext(provider_kind=ProviderKind.PI, workdir=tmp_path)

        first = await provider.acquire("sid-1", ctx)
        second = await provider.acquire("sid-2", ctx)

        assert first is backend
        assert second is backend

    @pytest.mark.asyncio
    async def test_release_is_no_op(self, tmp_path: Path) -> None:
        backend = _CountingCloseBackend()
        provider = PoolScopedBackendProvider(backend)

        # release must not close the backend or otherwise touch it; the
        # pool owns the lifetime. Both turn_failed values must be accepted.
        await provider.release(backend, turn_failed=False)
        await provider.release(backend, turn_failed=True)

        assert backend.close_calls == 0

    @pytest.mark.asyncio
    async def test_close_all_calls_backend_close(self, tmp_path: Path) -> None:
        backend = _CountingCloseBackend()
        provider = PoolScopedBackendProvider(backend)

        await provider.close_all()

        assert backend.close_calls == 1

    @pytest.mark.asyncio
    async def test_close_all_propagates_backend_close_failure(self, tmp_path: Path) -> None:
        failure = RuntimeError("close failed")

        class _CloseFailingBackend(StreamingProviderBackend):
            async def execute_streaming(
                self,
                opts: ExecOptions,
                env: dict[str, str],
                on_emission: Callable[[Emission], Awaitable[None]],
            ) -> BackendResult:
                return BackendResult(status=BackendStatus.COMPLETED)

            async def close(self) -> None:
                raise failure

        provider = PoolScopedBackendProvider(_CloseFailingBackend())

        with pytest.raises(RuntimeError, match="close failed"):
            await provider.close_all()
