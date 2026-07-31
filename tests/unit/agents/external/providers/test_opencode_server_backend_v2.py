"""Unit tests for the V2 OpenCodeServerBackend (control + event channels).

Tests the V2 execute_streaming flow with mocked V2 client and SSE reader:
- COMPLETED: session leaves active set
- TIMEOUT: polling times out → interrupt
- FAILED: 503 from active polling
- StaleSessionError: 404 SessionNotFoundError from prompt
- SSE reader lifecycle (start, register, unregister)
- Text fallback from context when SSE delivered no text
- close() stops SSE reader before server
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from modex_agent.agents.external import Emission, ExternalEvent
from modex_agent.agents.external.providers.opencode import server_backend as opencode_server_backend
from modex_agent.agents.external.providers.opencode.server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external.providers.opencode.v2_client import (
    OpencodeV2Error,
)
from modex_agent.agents.external.types import BackendStatus, ExecOptions


def _make_backend_with_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OpenCodeServerBackend, AsyncMock, AsyncMock]:
    """Build a backend with a mock ServerHandle.

    Bypasses ``_ensure_server`` (server lifecycle is owned by
    ``OpenCodeServerManager``). The mock handle carries mock client/parser/
    SSE reader so ``execute_streaming`` can be tested in isolation.
    """
    from modex_agent.agents.external.providers.opencode.server_manager import (
        OpenCodeServerManager,
    )

    backend = OpenCodeServerBackend()

    mock_client = AsyncMock()
    mock_parser = Mock()
    mock_reader = AsyncMock()

    mock_reader.start = AsyncMock()
    mock_reader.stop = AsyncMock()
    mock_reader.register_session = Mock()
    mock_reader.unregister_session = Mock()

    handle = OpenCodeServerManager.ServerHandle(
        server_url="http://127.0.0.1:9999",
        client=mock_client,
        parser=mock_parser,
        sse_reader=mock_reader,
        manager=Mock(),
        workdir=str(Path("/tmp/test")),
    )
    backend._handle = handle

    monkeypatch.setattr(backend, "_ensure_server", AsyncMock(return_value=None))

    return backend, mock_client, mock_reader


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


class TestExecuteStreamingCompleted:
    async def test_completed_when_session_leaves_active_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-123")
        mock_client.prompt_async_v1 = AsyncMock()
        status_calls = 0

        async def get_status_v1(sid: str, *, directory: str | None = None) -> str:
            nonlocal status_calls
            status_calls += 1
            if status_calls < 3:
                return "busy"
            return "idle"

        mock_client.get_session_status_v1 = get_status_v1
        mock_client.abort_session_v1 = AsyncMock()

        emissions: list[Emission] = []

        async def on_emission(e: Emission) -> None:
            emissions.append(e)

        result = await backend.execute_streaming(_make_opts(), {}, on_emission)

        assert result.status is BackendStatus.COMPLETED
        assert result.session_id == "sess-123"
        assert status_calls == 3
        mock_client.prompt_async_v1.assert_awaited_once()
        mock_reader.register_session.assert_called_once()
        mock_reader.unregister_session.assert_called_once()


class TestExecuteStreamingResume:
    """Turn 2+: resume_session_id is set → backend skips create_session_v1
    and sends prompt_async_v1 to the existing provider session.
    """

    async def test_resume_skips_create_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="should-not-be-called")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_session_status_v1 = AsyncMock(side_effect=["busy", "idle"])
        mock_client.abort_session_v1 = AsyncMock()

        async def on_emission(e: Emission) -> None:
            pass

        result = await backend.execute_streaming(
            _make_opts(resume_session_id="ses_existing"), {}, on_emission
        )

        assert result.status is BackendStatus.COMPLETED
        assert result.session_id == "ses_existing"
        mock_client.create_session_v1.assert_not_awaited()
        mock_client.prompt_async_v1.assert_awaited_once()
        assert mock_client.prompt_async_v1.call_args[0][0] == "ses_existing"

    async def test_resume_propagates_session_id_to_sse_registration(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock()
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_session_status_v1 = AsyncMock(return_value="idle")
        mock_client.abort_session_v1 = AsyncMock()

        async def on_emission(e: Emission) -> None:
            pass

        await backend.execute_streaming(
            _make_opts(resume_session_id="ses_resume_42"), {}, on_emission
        )

        registered_sid = mock_reader.register_session.call_args[0][0]
        assert registered_sid == "ses_resume_42"
        mock_reader.unregister_session.assert_called_once_with("ses_resume_42")


class TestExecuteStreamingRaceCondition:
    """Regression: prompt_async forks the prompt fiber (Effect.forkIn) and
    returns 204 immediately. The session is absent from the status map until
    the fiber sets ``{type: "busy"}``. The old code treated "unknown" (session
    not in map) as "idle" → returned immediately → turn ended in ~1.9s with
    zero SSE events. The fix waits for "busy" before polling for "idle".
    """

    async def test_unknown_before_busy_does_not_return_early(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)
        monkeypatch.setattr(opencode_server_backend, "_ACTIVE_POLL_INTERVAL", 0.001)
        monkeypatch.setattr(opencode_server_backend, "_BUSY_WAIT_TIMEOUT", 5.0)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-race")
        mock_client.prompt_async_v1 = AsyncMock()

        status_sequence = ["unknown", "unknown", "busy", "busy", "unknown"]
        call_idx = 0

        async def get_status_v1(sid: str, *, directory: str | None = None) -> str:
            nonlocal call_idx
            idx = min(call_idx, len(status_sequence) - 1)
            call_idx += 1
            return status_sequence[idx]

        mock_client.get_session_status_v1 = get_status_v1
        mock_client.abort_session_v1 = AsyncMock()

        emissions: list[Emission] = []

        async def on_emission(e: Emission) -> None:
            emissions.append(e)

        result = await backend.execute_streaming(_make_opts(), {}, on_emission)

        assert result.status is BackendStatus.COMPLETED
        assert result.session_id == "sess-race"
        assert call_idx >= 3
        assert status_sequence[min(call_idx - 1, len(status_sequence) - 1)] == "unknown"

    async def test_idle_returns_immediately_in_wait_for_busy(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-fast")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_session_status_v1 = AsyncMock(return_value="idle")
        mock_client.abort_session_v1 = AsyncMock()

        emissions: list[Emission] = []

        async def on_emission(e: Emission) -> None:
            emissions.append(e)

        result = await backend.execute_streaming(_make_opts(), {}, on_emission)

        assert result.status is BackendStatus.COMPLETED
        assert result.session_id == "sess-fast"
        mock_client.get_session_status_v1.assert_awaited_once()


class TestExecuteStreamingTimeout:
    async def test_timeout_interrupts_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-t")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_session_status_v1 = AsyncMock(return_value="busy")
        mock_client.abort_session_v1 = AsyncMock()

        monkeypatch.setattr(opencode_server_backend, "_ACTIVE_POLL_INTERVAL", 0.01)

        async def on_emission(e: Emission) -> None:
            pass

        result = await backend.execute_streaming(_make_opts(timeout=0.05), {}, on_emission)

        assert result.status is BackendStatus.TIMEOUT
        assert result.session_id == "sess-t"
        mock_client.abort_session_v1.assert_awaited_once_with("sess-t", directory=str(Path("/tmp/test")))
        mock_reader.unregister_session.assert_called_once()


class TestExecuteStreamingFailed:
    async def test_failed_on_503_from_active_polling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-f")
        mock_client.prompt_async_v1 = AsyncMock()

        error = OpencodeV2Error(
            tag="ServiceUnavailableError",
            message="unavailable",
            status=503,
            body=None,
        )
        mock_client.get_session_status_v1 = AsyncMock(side_effect=error)
        mock_client.abort_session_v1 = AsyncMock()

        async def on_emission(e: Emission) -> None:
            pass

        result = await backend.execute_streaming(_make_opts(), {}, on_emission)

        assert result.status is BackendStatus.FAILED
        assert result.session_id == "sess-f"
        mock_reader.unregister_session.assert_called_once()


class TestExecuteStreamingStaleSession:
    async def test_stale_session_raises_when_prompt_returns_404(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from modex_agent.agents.external.agent import StaleSessionError

        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-stale")
        error = OpencodeV2Error(
            tag="SessionNotFoundError",
            message="not found",
            status=404,
            body=None,
        )
        mock_client.prompt_async_v1 = AsyncMock(side_effect=error)

        async def on_emission(e: Emission) -> None:
            pass

        with pytest.raises(StaleSessionError):
            await backend.execute_streaming(
                _make_opts(resume_session_id="sess-stale"), {}, on_emission
            )

        mock_reader.unregister_session.assert_called_once()


class TestSseReaderLifecycle:
    async def test_session_registered_and_unregistered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-lc")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_session_status_v1 = AsyncMock(return_value="idle")
        mock_client.abort_session_v1 = AsyncMock()

        async def on_emission(e: Emission) -> None:
            pass

        await backend.execute_streaming(_make_opts(), {}, on_emission)

        # Reader start/stop is owned by OpenCodeServerManager.acquire(), not
        # the backend. The backend only registers/unregisters the turn's
        # session on the borrowed reader.
        registered_sid = mock_reader.register_session.call_args[0][0]
        assert registered_sid == "sess-lc"
        mock_reader.unregister_session.assert_called_once_with("sess-lc")


class TestTextFallback:
    async def test_fallback_emits_text_when_sse_delivered_none(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        backend, mock_client, mock_reader = _make_backend_with_mocks(monkeypatch)

        mock_client.create_session_v1 = AsyncMock(return_value="sess-fb")
        mock_client.prompt_async_v1 = AsyncMock()
        mock_client.get_session_status_v1 = AsyncMock(return_value="idle")
        mock_client.abort_session_v1 = AsyncMock()

        v1_messages = [
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "fallback text"}],
            }
        ]

        mock_client.get_messages_v1 = AsyncMock(return_value=v1_messages)

        emissions: list[Emission] = []

        async def on_emission(e: Emission) -> None:
            emissions.append(e)

        await backend.execute_streaming(_make_opts(), {}, on_emission)

        text_emissions = [e for e in emissions if e.event is ExternalEvent.TEXT_DELTA]
        assert len(text_emissions) == 1
        assert text_emissions[0].text == "fallback text"


class TestCloseIsNoOp:
    async def test_close_does_not_release_handle(self) -> None:
        backend = OpenCodeServerBackend()
        backend._handle = Mock()
        await backend.close()
