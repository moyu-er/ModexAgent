"""Integration tests for ``OpenCodeTransport`` against a real opencode server.

Server-lifecycle unit tests (spawn, readiness rollback, close-time reap, etc.)
live in ``test_opencode_server_manager.py`` — the singleton
``OpenCodeServerManager`` owns the ``opencode serve`` process, SSE readers,
and the HTTP client. ``OpenCodeTransport`` borrows a ``ServerHandle`` per
turn via ``OpenCodeServerManager.acquire()`` and delegates all
session/prompt/poll operations to V1 endpoints on the shared client.

The tests below are skip-gated behind ``OPENCODE_SSE_INTEGRATION=1`` and a real
``opencode`` binary on PATH. They exercise the real V2 control + V1 SSE event
flow end-to-end and require a running development environment.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="aiohttp not installed")

from modex_agent.agents.external.transports import OpenCodeTransport
from modex_agent.agents.external.types import BackendStatus, ExecOptions
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

_SKIP_REASON = "opencode CLI not installed or OPENCODE_SSE_INTEGRATION not set"


def _opencode_available() -> bool:
    return shutil.which("opencode") is not None and bool(os.environ.get("OPENCODE_SSE_INTEGRATION"))


def _make_env(modex_sid: str = "test_sse.opencode") -> dict[str, str]:
    """Build env matching ExternalEnvBuilder output (full os.environ + MODEX_*)."""
    env = dict(os.environ)
    env.update(
        {
            "MODEX_SESSION_ID": modex_sid,
            "MODEX_AGENT_NAME": "opencode",
            "MODEX_INBOX_ROOT": os.environ.get("TEMP", "/tmp"),
            "MODEX_AGENT_POOL_MAP": "opencode=pool_opencode",
            "MODEX_TARGETS": "",
        }
    )
    return env


def _collect(sink: list[TurnEvent]):
    async def _cb(event: TurnEvent) -> None:
        sink.append(event)

    return _cb


@pytest.mark.skipif(not _opencode_available(), reason=_SKIP_REASON)
@pytest.mark.asyncio
class TestOpenCodeTransportIntegration:
    async def test_simple_prompt_streams_text_delta(self) -> None:
        transport = OpenCodeTransport()
        try:
            opts = ExecOptions(
                prompt="Say hello in exactly three words. Do not use any tools.",
                workdir=Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd())),
            )
            env = _make_env("test_sse_1.opencode")
            events: list[TurnEvent] = []

            result = await transport.execute(opts, env, _collect(events))

            assert result.status is BackendStatus.COMPLETED
            assert result.session_id is not None
            text_events = [e for e in events if isinstance(e, TurnTextEvent)]
            assert len(text_events) > 0
            combined = "".join(e.text for e in text_events)
            assert len(combined) > 0
        finally:
            await transport.close()

    async def test_prompt_with_tool_yields_tool_call_and_result(self) -> None:
        transport = OpenCodeTransport()
        try:
            opts = ExecOptions(
                prompt="Read the first 5 lines of README.md, then summarize in one sentence.",
                workdir=Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd())),
            )
            env = _make_env("test_sse_2.opencode")
            events: list[TurnEvent] = []

            result = await transport.execute(opts, env, _collect(events))

            assert result.status is BackendStatus.COMPLETED
            tool_calls = [e for e in events if isinstance(e, TurnToolCallEvent)]
            assert len(tool_calls) >= 1
            tool_results = [e for e in events if isinstance(e, TurnToolResultEvent)]
            assert len(tool_results) >= 1
            text_events = [e for e in events if isinstance(e, TurnTextEvent)]
            assert len(text_events) > 0
        finally:
            await transport.close()

    async def test_session_resume_reuses_session_id(self) -> None:
        transport = OpenCodeTransport()
        try:
            workdir = Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd()))
            env = _make_env("test_sse_3.opencode")

            opts1 = ExecOptions(prompt="Say hi.", workdir=workdir)
            events1: list[TurnEvent] = []

            result1 = await transport.execute(opts1, env, _collect(events1))
            assert result1.status is BackendStatus.COMPLETED
            assert result1.session_id is not None

            opts2 = ExecOptions(
                prompt="Say bye.",
                workdir=workdir,
                resume_session_id=result1.session_id,
            )
            events2: list[TurnEvent] = []

            result2 = await transport.execute(opts2, env, _collect(events2))
            assert result2.status is BackendStatus.COMPLETED
            assert result2.session_id == result1.session_id
        finally:
            await transport.close()
