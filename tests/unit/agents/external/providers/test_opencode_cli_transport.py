"""Unit tests for the OpenCode CLI transport (scripted event-driven model).

Uses scripted SSE events fed through the real ``OpenCodeSessionState``
registry + ``TurnCompletionWaiter`` — NOT mocked polling. The transport's
``execute`` creates a waiter internally; tests feed ``registry.on_event(...)``
to simulate the SSE event sequence and drive the waiter's
ACTIVE/QUIESCING/COMPLETE state machine.

Test cases (design 7.4, carried over from the retired backend tests):

  4.1 — scripted: turn COMPLETED only after whole tree quiesced (NOT first idle)
  4.2 — scripted: child session events routed via the child callback factory
  4.3 — scripted: reader reconnect_pending → fallback busy poll
  4.4 — turn end: ``unregister_waiter`` called; ``unregister_session`` NOT called
  4.5 — timeout → abort + TIMEOUT result
  4.6 — scripted: opencode process restart → root 404 → ERROR result
  4.7 — scripted: reader disconnect → reconnect → rebuild → COMPLETED

Plus adapted carry-over tests: resume, stale session, text fallback, close
no-op.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("aiohttp", reason="aiohttp not installed")

from modex_agent.agents.external.providers.opencode.server_manager import (
    OpenCodeServerManager,
)
from modex_agent.agents.external.providers.opencode.session_state import (
    OpenCodeSessionState,
    SessionActivity,
)
from modex_agent.agents.external.providers.opencode.v2_client import OpencodeV2Error
from modex_agent.agents.external.transports import (
    StaleSessionError,
)
from modex_agent.agents.external.transports.cli_transport import OpenCodeTransport
from modex_agent.agents.external.types import BackendStatus, ExecOptions
from modex_agent.core.turn_events import TurnEvent, TurnTextEvent

_WORKDIR = str(Path("/tmp/test"))


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_transport_with_mocks(
    monkeypatch: pytest.MonkeyPatch,
    quiesce_s: float = 0.05,
) -> tuple[OpenCodeTransport, AsyncMock, AsyncMock, OpenCodeSessionState]:
    """Build a transport with a mock ServerHandle carrying a real registry.

    Bypasses ``_ensure_server`` (server lifecycle is owned by
    ``OpenCodeServerManager``). The mock handle carries mock client/SSE
    reader and a REAL ``OpenCodeSessionState`` registry so tests can feed
    scripted events through ``registry.on_event(...)``.
    """
    transport = OpenCodeTransport(quiesce_s=quiesce_s)

    mock_client = AsyncMock()
    mock_parser = Mock()
    mock_reader = AsyncMock()

    mock_reader.start = AsyncMock()
    mock_reader.stop = AsyncMock()
    mock_reader.register_session = Mock()
    mock_reader.unregister_session = Mock()

    registry = OpenCodeSessionState()

    handle = OpenCodeServerManager.ServerHandle(
        server_url="http://127.0.0.1:9999",
        client=mock_client,
        parser=mock_parser,
        sse_reader=mock_reader,
        session_state=registry,
        manager=Mock(),
        workdir=_WORKDIR,
    )
    transport._handle = handle

    monkeypatch.setattr(transport, "_ensure_server", AsyncMock(return_value=None))

    return transport, mock_client, mock_reader, registry


def _make_opts(
    prompt: str = "hello",
    workdir: Path = Path("/tmp/test"),
    resume_session_id: str | None = None,
    timeout: float | None = None,
) -> ExecOptions:
    return ExecOptions(
        prompt=prompt,
        workdir=workdir,
        resume_session_id=resume_session_id,
        timeout=timeout,
    )


def _default_client_mocks(mock_client: AsyncMock) -> None:
    """Set up the default mock client responses for scripted tests."""
    mock_client.get_children = AsyncMock(return_value=[])
    mock_client.abort_session_v1 = AsyncMock()


def _collect(sink: list[TurnEvent]):
    async def _cb(event: TurnEvent) -> None:
        sink.append(event)

    return _cb


async def _wait_for_turn_start(task: asyncio.Task[object], delay: float = 0.02) -> None:
    """Let ``execute`` reach ``waiter.wait_complete()``."""
    await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# 4.1 — scripted: turn COMPLETED only after whole tree quiesced
# ---------------------------------------------------------------------------


class TestScriptedCompleteLoop:
    """4.1 — turn COMPLETED only after quiesce window, NOT on first idle."""

    async def test_completed_after_quiesce_not_first_idle(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_1")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        events: list[TurnEvent] = []

        task = asyncio.create_task(transport.execute(_make_opts(), {}, _collect(events)))
        await _wait_for_turn_start(task)

        # Feed root busy → root idle
        registry.on_event("ses_1", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("ses_1", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0.02)

        # Turn should NOT be done yet (quiesce window 0.05s not elapsed)
        assert not task.done(), "Turn completed before quiesce window elapsed"

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED
        assert result.session_id == "ses_1"
        mock_client.prompt_async_v1.assert_awaited_once()
        mock_reader.register_session.assert_called_once()


# ---------------------------------------------------------------------------
# 4.2 — scripted: child session events routed via the child factory
# ---------------------------------------------------------------------------


class TestScriptedChildRouting:
    """4.2 — child session events routed to the child callback factory."""

    async def test_child_event_routed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_root")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        main_events: list[TurnEvent] = []
        child_events: list[TurnEvent] = []

        def child_factory(provider_sid: str):
            return _collect(child_events)

        task = asyncio.create_task(
            transport.execute(_make_opts(), {}, _collect(main_events), child_factory)
        )
        await _wait_for_turn_start(task)

        # Get the emission callback registered by execute
        captured_cb = mock_reader.register_session.call_args[0][1]
        captured_factory = mock_reader.register_session.call_args[0][2]

        # Feed: root busy → child created → child text → child idle → root idle
        registry.on_event("ses_root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("ses_child", "session.created", parent_sid="ses_root")
        await asyncio.sleep(0)

        # Emit child text via the factory-obtained callback (simulates the
        # reader's per-session demux delivering a child event)
        await captured_factory("ses_child")(TurnTextEvent(text="child output"))
        await captured_cb(TurnTextEvent(text="root output"))

        registry.on_event("ses_child", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0)
        registry.on_event("ses_root", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED

        assert child_events == [TurnTextEvent(text="child output")]
        assert main_events == [TurnTextEvent(text="root output")]


# ---------------------------------------------------------------------------
# 4.3 — scripted: reader reconnect_pending → fallback busy poll
# ---------------------------------------------------------------------------


class TestScriptedReconnectFallback:
    """4.3 — reconnect_pending triggers ``_wait_busy_fallback`` poll."""

    async def test_reconnect_pending_triggers_busy_poll(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_recon")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_children = AsyncMock(return_value=[])
        mock_client.abort_session_v1 = AsyncMock()

        status_calls: list[str] = []

        async def get_status(sid: str, *, directory: str | None = None) -> str:
            status_calls.append(sid)
            return "busy"

        mock_client.get_session_status_v1 = get_status

        # Mark reconnect pending BEFORE starting the turn
        registry.mark_reconnect_pending()

        task = asyncio.create_task(transport.execute(_make_opts(), {}, _collect([])))

        # Wait for _wait_busy_fallback + rebuild_subtree to complete
        await asyncio.sleep(0.05)

        # _wait_busy_fallback should have polled get_session_status_v1
        assert len(status_calls) > 0, "wait_busy_fallback did not poll status"
        assert status_calls[0] == "ses_recon"

        # reconnect_pending should be cleared by rebuild_subtree (no fetch error)
        assert not registry.is_reconnect_pending()

        # Feed root idle → quiesce → COMPLETE
        # (rebuild_subtree already set root to BUSY via get_session_status_v1)
        registry.on_event("ses_recon", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED


# ---------------------------------------------------------------------------
# 4.4 — turn end: unregister_waiter called; unregister_session NOT called
# ---------------------------------------------------------------------------


class TestTurnEndCleanup:
    """4.4 — waiter unregistered; SSE output route preserved (NOT unregistered)."""

    async def test_waiter_unregistered_session_route_preserved(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_clean")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        task = asyncio.create_task(transport.execute(_make_opts(), {}, _collect([])))
        await _wait_for_turn_start(task)

        registry.on_event("ses_clean", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0.01)
        registry.on_event("ses_clean", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED

        # Waiter should be unregistered (use-and-discard per turn)
        assert len(registry._waiters) == 0, "waiter not unregistered after turn"

        # unregister_session should NOT be called — output route preserved
        # for cross-turn reuse (design 5.6: "finally 不再 unregister_session")
        mock_reader.unregister_session.assert_not_called()


# ---------------------------------------------------------------------------
# 4.5 — timeout → abort + TIMEOUT result
# ---------------------------------------------------------------------------


class TestTimeoutAborts:
    """4.5 — timeout → abort_session_v1 + TIMEOUT BackendResult."""

    async def test_timeout_aborts_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_timeout")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        # Very short timeout — no events fed, waiter never completes
        result = await transport.execute(_make_opts(timeout=0.05), {}, _collect([]))

        assert result.status is BackendStatus.TIMEOUT
        assert result.session_id == "ses_timeout"
        mock_client.abort_session_v1.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4.6 — scripted: opencode process restart → root 404 → ERROR result
# ---------------------------------------------------------------------------


class TestScriptedRootMissing:
    """4.6 — root session 404 → rebuild_subtree marks root_missing → FAILED."""

    async def test_root_missing_returns_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_dead")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.abort_session_v1 = AsyncMock()

        # get_children raises 404 → rebuild_subtree marks root missing
        mock_client.get_children = AsyncMock(
            side_effect=OpencodeV2Error(
                tag="SessionNotFoundError",
                message="not found",
                status=404,
                body=None,
            )
        )
        # _wait_busy_fallback needs a status response
        mock_client.get_session_status_v1 = AsyncMock(return_value="busy")

        # Mark reconnect pending to trigger _wait_busy_fallback + rebuild_subtree
        registry.mark_reconnect_pending()

        result = await asyncio.wait_for(
            transport.execute(_make_opts(), {}, _collect([])),
            timeout=2.0,
        )

        assert result.status is BackendStatus.FAILED
        assert result.session_id == "ses_dead"
        assert result.error is not None


# ---------------------------------------------------------------------------
# 4.7 — scripted: reader disconnect → reconnect → rebuild → COMPLETED
# ---------------------------------------------------------------------------


class TestScriptedDisconnectRecovery:
    """4.7 — disconnect → reconnect → rebuild_subtree → re-judge → COMPLETED."""

    async def test_disconnect_reconnect_completes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_disc")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_children = AsyncMock(return_value=[])
        mock_client.abort_session_v1 = AsyncMock()

        # _wait_busy_fallback + rebuild_subtree use these
        mock_client.get_session_status_v1 = AsyncMock(return_value="busy")

        # Mark reconnect pending (simulates reader disconnect)
        registry.mark_reconnect_pending()

        task = asyncio.create_task(transport.execute(_make_opts(), {}, _collect([])))

        # Wait for _wait_busy_fallback + rebuild_subtree to complete
        await asyncio.sleep(0.05)

        # reconnect_pending should be cleared by rebuild_subtree (success)
        assert not registry.is_reconnect_pending()

        # rebuild_subtree set root to BUSY — feed root idle to trigger quiesce
        registry.on_event("ses_disc", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


class TestExecuteResume:
    """Turn 2+: resume_session_id skips create_session_v1."""

    async def test_resume_skips_create_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="should-not-be-called")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        task = asyncio.create_task(
            transport.execute(
                _make_opts(resume_session_id="ses_existing"), {}, _collect([])
            )
        )
        await _wait_for_turn_start(task)

        registry.on_event("ses_existing", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0.01)
        registry.on_event("ses_existing", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED
        assert result.session_id == "ses_existing"
        mock_client.create_session_v1.assert_not_awaited()
        mock_client.prompt_async_v1.assert_awaited_once()
        assert mock_client.prompt_async_v1.call_args[0][0] == "ses_existing"

    async def test_resume_propagates_session_id_to_sse_registration(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock()
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        task = asyncio.create_task(
            transport.execute(
                _make_opts(resume_session_id="ses_resume_42"), {}, _collect([])
            )
        )
        await _wait_for_turn_start(task)

        registry.on_event("ses_resume_42", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0.01)
        registry.on_event("ses_resume_42", "session.status", activity=SessionActivity.IDLE)

        await asyncio.wait_for(task, timeout=1.0)

        registered_sid = mock_reader.register_session.call_args[0][0]
        assert registered_sid == "ses_resume_42"
        # unregister_session NOT called (design 5.6)
        mock_reader.unregister_session.assert_not_called()


# ---------------------------------------------------------------------------
# Stale session
# ---------------------------------------------------------------------------


class TestExecuteStaleSession:
    async def test_stale_session_raises_when_prompt_returns_404(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_stale")
        error = OpencodeV2Error(
            tag="SessionNotFoundError",
            message="not found",
            status=404,
            body=None,
        )
        mock_client.prompt_async_v1 = AsyncMock(side_effect=error)

        with pytest.raises(StaleSessionError):
            await transport.execute(
                _make_opts(resume_session_id="ses_stale"), {}, _collect([])
            )

        # unregister_session NOT called (design 5.6)
        mock_reader.unregister_session.assert_not_called()


# ---------------------------------------------------------------------------
# Text fallback
# ---------------------------------------------------------------------------


class TestTextFallback:
    async def test_fallback_emits_text_when_sse_delivered_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_fb")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        v1_messages = [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "fallback text"}],
            }
        ]
        mock_client.get_messages_v1 = AsyncMock(return_value=v1_messages)

        events: list[TurnEvent] = []

        task = asyncio.create_task(transport.execute(_make_opts(), {}, _collect(events)))
        await _wait_for_turn_start(task)

        # Feed root busy → root idle (no text emitted via SSE)
        registry.on_event("ses_fb", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0.01)
        registry.on_event("ses_fb", "session.status", activity=SessionActivity.IDLE)

        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED

        text_events = [e for e in events if isinstance(e, TurnTextEvent)]
        assert len(text_events) == 1
        assert text_events[0].text == "fallback text"


# ---------------------------------------------------------------------------
# Close is no-op
# ---------------------------------------------------------------------------


class TestCloseIsNoOp:
    async def test_close_does_not_release_handle(self) -> None:
        transport = OpenCodeTransport()
        transport._handle = Mock()
        await transport.close()


# ---------------------------------------------------------------------------
# Integration: full loop end-to-end (scripted)
# ---------------------------------------------------------------------------


class TestIntegrationFullLoopEndToEnd:
    """Full loop: root → subagent → inject → root resumes → COMPLETE.

    Verifies the core scenario from design 7.5: "完整循环端到端:输出全流入
    emitter、回合整树静默后结束。" All three text events (root initial,
    child, root final) must reach their delivery targets, and the turn must
    NOT complete at the first root idle (step 4) — only after the final root
    idle + quiesce window.
    """

    async def test_full_loop_all_events_and_late_completion(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_root")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        main_events: list[TurnEvent] = []
        child_events: list[TurnEvent] = []

        def child_factory(provider_sid: str):
            return _collect(child_events)

        task = asyncio.create_task(
            transport.execute(_make_opts(), {}, _collect(main_events), child_factory)
        )
        await _wait_for_turn_start(task)

        captured_cb = mock_reader.register_session.call_args[0][1]
        captured_factory = mock_reader.register_session.call_args[0][2]

        # --- Steps 1-2: root busy + root text "working on it..." ---
        registry.on_event("ses_root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        await captured_cb(TurnTextEvent(text="working on it..."))

        # --- Step 3: child created (background subagent) ---
        registry.on_event("ses_child", "session.created", parent_sid="ses_root")
        await asyncio.sleep(0)

        # --- Step 4: root idle (root paused waiting for subagent) ---
        registry.on_event("ses_root", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0)

        # --- Step 5: child busy ---
        registry.on_event("ses_child", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)

        # --- Step 6: child text "child output" ---
        await captured_factory("ses_child")(TurnTextEvent(text="child output"))

        # --- Step 7: child idle (child done) ---
        registry.on_event("ses_child", "session.status", activity=SessionActivity.IDLE)
        await asyncio.sleep(0)

        # Turn should NOT be done — tree just went all-idle, quiesce window
        # (0.05s) has not elapsed. This is the critical assertion: the turn
        # did NOT complete at step 4 (first root idle) or step 7.
        assert not task.done(), "Turn completed prematurely (before inject/resume)"

        # --- Step 9: root busy (inject resumed root) ---
        registry.on_event("ses_root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)

        # --- Step 10: root text "final output" ---
        await captured_cb(TurnTextEvent(text="final output"))

        # --- Step 11: root idle ---
        registry.on_event("ses_root", "session.status", activity=SessionActivity.IDLE)

        # --- Steps 12-13: quiesce window passes → COMPLETE ---
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result.status is BackendStatus.COMPLETED
        assert result.session_id == "ses_root"

        # All text events received on the right targets
        assert [e.text for e in main_events if isinstance(e, TurnTextEvent)] == [
            "working on it...",
            "final output",
        ]
        assert [e.text for e in child_events if isinstance(e, TurnTextEvent)] == [
            "child output"
        ]


# ---------------------------------------------------------------------------
# Integration: cross-turn wakeup + no state leak
# ---------------------------------------------------------------------------


class TestIntegrationCrossTurnAndNoStateLeak:
    """Cross-turn wakeup (T1 completes → T2 on same sid) and no residual
    state between turns (waiter discarded, registry node preserved)."""

    async def test_cross_turn_wakeup_no_residual_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_root")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        # Capture baseline tasks before T1 to detect leaks
        baseline_tasks = asyncio.all_tasks()

        # ===== T1: root busy → root idle → quiesce → COMPLETE =====
        task_t1 = asyncio.create_task(
            transport.execute(_make_opts(prompt="T1"), {}, _collect([]))
        )
        await _wait_for_turn_start(task_t1)

        registry.on_event("ses_root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("ses_root", "session.status", activity=SessionActivity.IDLE)

        result_t1 = await asyncio.wait_for(task_t1, timeout=1.0)
        assert result_t1.status is BackendStatus.COMPLETED
        assert result_t1.session_id == "ses_root"

        # ===== Between T1 and T2: verify no residual state =====
        assert len(registry._waiters) == 0, "waiter not unregistered after T1"

        # No leaked asyncio tasks from T1's waiter.
        after_t1_tasks = asyncio.all_tasks()
        leaked = after_t1_tasks - baseline_tasks
        assert not leaked, f"Leaked tasks after T1: {leaked}"

        # Root session node preserved for cross-turn reuse (design 5.6).
        assert "ses_root" in registry._nodes, "root node removed after T1"

        # ===== T2: same sid, new prompt → root busy → text → idle → COMPLETE =====
        events_t2: list[TurnEvent] = []
        task_t2 = asyncio.create_task(
            transport.execute(
                _make_opts(prompt="T2", resume_session_id="ses_root"),
                {},
                _collect(events_t2),
            )
        )
        await _wait_for_turn_start(task_t2)

        # T2 reused the session — create_session_v1 not called again
        assert mock_client.create_session_v1.await_count == 1

        # Capture T2's callback (register_session called again)
        captured_cb_t2 = mock_reader.register_session.call_args[0][1]

        # Feed T2 events: root busy → root text → root idle
        registry.on_event("ses_root", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        await captured_cb_t2(TurnTextEvent(text="T2 output"))
        registry.on_event("ses_root", "session.status", activity=SessionActivity.IDLE)

        result_t2 = await asyncio.wait_for(task_t2, timeout=1.0)
        assert result_t2.status is BackendStatus.COMPLETED
        assert result_t2.session_id == "ses_root"

        # T2 output is correct
        text_t2 = [e for e in events_t2 if isinstance(e, TurnTextEvent)]
        assert len(text_t2) == 1
        assert text_t2[0].text == "T2 output"

        # prompt_async_v1 called once per turn
        assert mock_client.prompt_async_v1.await_count == 2

        # register_session called twice (T1 + T2), both with "ses_root"
        assert mock_reader.register_session.call_count == 2
        assert mock_reader.register_session.call_args_list[0][0][0] == "ses_root"
        assert mock_reader.register_session.call_args_list[1][0][0] == "ses_root"

    async def test_registry_node_preserved_waiter_discarded_t2_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        transport, mock_client, mock_reader, registry = _make_transport_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="ses_loop")
        mock_client.prompt_async_v1 = AsyncMock()
        _default_client_mocks(mock_client)

        # ===== T1 =====
        task_t1 = asyncio.create_task(
            transport.execute(_make_opts(prompt="first"), {}, _collect([]))
        )
        await _wait_for_turn_start(task_t1)

        registry.on_event("ses_loop", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("ses_loop", "session.status", activity=SessionActivity.IDLE)

        result_t1 = await asyncio.wait_for(task_t1, timeout=1.0)
        assert result_t1.status is BackendStatus.COMPLETED

        # After T1: waiter unregistered, node preserved
        assert len(registry._waiters) == 0
        assert "ses_loop" in registry._nodes
        assert registry._nodes["ses_loop"].activity is SessionActivity.IDLE

        # ===== T2: new waiter on same root_sid =====
        task_t2 = asyncio.create_task(
            transport.execute(
                _make_opts(prompt="second", resume_session_id="ses_loop"),
                {},
                _collect([]),
            )
        )
        await _wait_for_turn_start(task_t2)

        # T2 registered a new waiter
        assert len(registry._waiters) == 1, "T2 did not register a new waiter"

        # Feed T2 events
        registry.on_event("ses_loop", "session.status", activity=SessionActivity.BUSY)
        await asyncio.sleep(0)
        registry.on_event("ses_loop", "session.status", activity=SessionActivity.IDLE)

        result_t2 = await asyncio.wait_for(task_t2, timeout=1.0)
        assert result_t2.status is BackendStatus.COMPLETED

        # After T2: waiter unregistered again
        assert len(registry._waiters) == 0
        # Node still preserved
        assert "ses_loop" in registry._nodes
