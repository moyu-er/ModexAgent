"""Integration tests for ``OpenCodeServerBackend`` against a real opencode server.

Server-lifecycle unit tests (spawn, readiness rollback, close-time reap, etc.)
have moved to ``test_opencode_server_manager.py`` — the singleton
``OpenCodeServerManager`` now owns the ``opencode serve`` process, SSE readers,
and the HTTP client. ``OpenCodeServerBackend`` is a thin wrapper that borrows a
``ServerHandle`` per turn via ``OpenCodeServerManager.acquire()`` and delegates
all session/prompt/poll operations to V1 endpoints on the shared client.

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

from modex_agent.agents.external import Emission, ExternalEvent
from modex_agent.agents.external.providers.opencode.server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external.types import BackendStatus, ExecOptions

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


@pytest.mark.skipif(not _opencode_available(), reason=_SKIP_REASON)
@pytest.mark.asyncio
class TestOpenCodeServerBackendIntegration:
    async def test_simple_prompt_streams_text_delta(self) -> None:
        backend = OpenCodeServerBackend()
        try:
            opts = ExecOptions(
                prompt="Say hello in exactly three words. Do not use any tools.",
                workdir=Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd())),
            )
            env = _make_env("test_sse_1.opencode")
            emissions: list[Emission] = []

            async def on_emission(e: Emission) -> None:
                emissions.append(e)

            result = await backend.execute_streaming(opts, env, on_emission)

            assert result.status is BackendStatus.COMPLETED
            assert result.session_id is not None
            text_emissions = [e for e in emissions if e.event is ExternalEvent.TEXT_DELTA]
            assert len(text_emissions) > 0
            combined = "".join(e.text or "" for e in text_emissions)
            assert len(combined) > 0
        finally:
            await backend.close()

    async def test_prompt_with_tool_yields_tool_use_and_result(self) -> None:
        backend = OpenCodeServerBackend()
        try:
            opts = ExecOptions(
                prompt="Read the first 5 lines of README.md, then summarize in one sentence.",
                workdir=Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd())),
            )
            env = _make_env("test_sse_2.opencode")
            emissions: list[Emission] = []

            async def on_emission(e: Emission) -> None:
                emissions.append(e)

            result = await backend.execute_streaming(opts, env, on_emission)

            assert result.status is BackendStatus.COMPLETED
            tool_uses = [e for e in emissions if e.event is ExternalEvent.TOOL_USE]
            assert len(tool_uses) >= 1
            tool_results = [e for e in emissions if e.event is ExternalEvent.TOOL_RESULT]
            assert len(tool_results) >= 1
            text_emissions = [e for e in emissions if e.event is ExternalEvent.TEXT_DELTA]
            assert len(text_emissions) > 0
        finally:
            await backend.close()

    async def test_session_resume_reuses_session_id(self) -> None:
        backend = OpenCodeServerBackend()
        try:
            workdir = Path(os.environ.get("OPENCODE_TEST_WORKDIR", os.getcwd()))
            env = _make_env("test_sse_3.opencode")

            opts1 = ExecOptions(prompt="Say hi.", workdir=workdir)
            emissions1: list[Emission] = []

            async def on_e1(e: Emission) -> None:
                emissions1.append(e)

            result1 = await backend.execute_streaming(opts1, env, on_e1)
            assert result1.status is BackendStatus.COMPLETED
            assert result1.session_id is not None

            opts2 = ExecOptions(
                prompt="Say bye.",
                workdir=workdir,
                resume_session_id=result1.session_id,
            )
            emissions2: list[Emission] = []

            async def on_e2(e: Emission) -> None:
                emissions2.append(e)

            result2 = await backend.execute_streaming(opts2, env, on_e2)
            assert result2.status is BackendStatus.COMPLETED
            assert result2.session_id == result1.session_id
        finally:
            await backend.close()
