"""``OpenCodeServerBackend`` — shared server + V1 session/prompt backend.

The server lifecycle (spawn/health/SSE) is owned by
:class:`OpenCodeServerManager` — a process-global singleton that shares one
``opencode serve`` across ALL backends (main agents, subagents, peers).
This backend borrows the shared collaborators per turn via
``ServerHandle`` and releases them on close.

All session operations — creation, prompt dispatch, status polling, message
fallback, and abort — use V1 endpoints. V1 ``prompt_async`` runs through
``SessionPrompt`` which injects ``promptOps`` into the tool context; the
``task`` tool (subagent dispatch) requires this and is NOT available on the
V2 ``SessionRunner`` path.

The ``/event`` SSE stream carries both V2 (``session.next.*``) and V1
(``message.part.*``, ``session.created``) events through the same
``EventV2Bridge``. The parser and SSE reader handle both envelope shapes.

When ``OPENCODE_HOST`` is set in the environment, the manager connects to
an external already-running server instead of spawning its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import override

import aiohttp

from ...agent import StaleSessionError, StreamingProviderBackend
from ...events import ExternalEvent
from ...types import BackendResult, BackendStatus, Emission, ExecOptions
from .server_manager import OpenCodeServerManager
from .v2_client import (
    ModelRef,
    OpencodeV2Client,
    OpencodeV2Error,
)
from .v2_parser import OpenCodeV2EventParser
from .v2_sse_reader import OpenCodeV2SseReader

logger = logging.getLogger(__name__)

__all__ = ["OpenCodeServerBackend"]

_SSE_READ_TIMEOUT: float = 300.0
_ACTIVE_POLL_INTERVAL: float = 0.5
_BUSY_WAIT_TIMEOUT: float = 15.0


def _model_ref_from_str(model: str | None) -> ModelRef | None:
    if not model:
        return None
    parts = model.split("/", 1)
    if len(parts) == 2:
        return ModelRef(id=parts[1], providerID=parts[0])
    return ModelRef(id=model, providerID="")


class OpenCodeServerBackend(StreamingProviderBackend):
    """Shared-server backend — borrows collaborators from ``OpenCodeServerManager``.

    Each ``execute_streaming`` call acquires a :class:`ServerHandle` from the
    singleton manager. The handle carries the shared HTTP client, parser, and
    SSE reader. The backend does NOT own the server process — that lifecycle
    is the manager's responsibility (refcounted across all backends).
    """

    def __init__(self) -> None:
        self._handle: OpenCodeServerManager.ServerHandle | None = None

    @property
    def _client(self) -> OpencodeV2Client:
        assert self._handle is not None
        return self._handle.client

    @property
    def _parser(self) -> OpenCodeV2EventParser:
        assert self._handle is not None
        return self._handle.parser

    @property
    def _sse_reader(self) -> OpenCodeV2SseReader:
        assert self._handle is not None
        return self._handle.sse_reader

    @property
    def _server_url(self) -> str:
        assert self._handle is not None
        return self._handle.server_url

    async def _ensure_server(self, workdir: Path, env: dict[str, str]) -> None:
        self._handle = await OpenCodeServerManager.acquire(workdir, env)

    async def close(self) -> None:
        pass

    @override
    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        await self._ensure_server(opts.workdir, env)

        workdir_str = str(opts.workdir)

        if opts.resume_session_id:
            session_id = opts.resume_session_id
        else:
            session_id = await self._client.create_session_v1(workdir_str)
        self._handle.register_session(session_id)

        text_seen = False

        async def _on_emission_tracked(emission: Emission) -> None:
            nonlocal text_seen
            if emission.event is ExternalEvent.TEXT_DELTA:
                text_seen = True
            await on_emission(emission)

        self._sse_reader.register_session(session_id, _on_emission_tracked)

        try:
            model_ref = _model_ref_from_str(opts.model)
            try:
                await self._client.prompt_async_v1(session_id, opts.prompt, model=model_ref, directory=workdir_str)
            except OpencodeV2Error as exc:
                if exc.tag == "SessionNotFoundError":
                    raise StaleSessionError(f"OpenCode session {session_id} not found") from exc
                raise

            timeout = opts.timeout if opts.timeout and opts.timeout > 0 else _SSE_READ_TIMEOUT
            try:
                await asyncio.wait_for(self._poll_status_v1(session_id, directory=workdir_str), timeout=timeout)
            except TimeoutError:
                with contextlib.suppress(OpencodeV2Error):
                        await self._client.abort_session_v1(session_id, directory=workdir_str)
                return BackendResult(status=BackendStatus.TIMEOUT, session_id=session_id)
            except OpencodeV2Error as exc:
                logger.exception("OpenCode status polling error")
                return BackendResult(
                    status=BackendStatus.FAILED, session_id=session_id, error=str(exc)
                )

            if not text_seen:
                await self._emit_fallback_text(session_id, on_emission, directory=workdir_str)

            return BackendResult(status=BackendStatus.COMPLETED, session_id=session_id)
        finally:
            self._sse_reader.unregister_session(session_id)
            self._handle.unregister_session(session_id)

    async def _poll_status_v1(self, session_id: str, *, directory: str) -> None:
        """Poll ``GET /session/status`` — wait for busy, then wait for idle.

        V1 ``prompt_async`` is fire-and-forget (returns 204 immediately). The
        opencode server forks the prompt fiber via ``Effect.forkIn`` and sets
        ``{type: "busy"}`` inside the fiber (``prompt.ts:1089``). There is a
        race window between the 204 response and the fiber setting "busy":
        during this window the session is absent from the status map, which
        opencode's ``SessionStatus.get`` defaults to ``{type: "idle"}``.

        Without waiting for "busy" first, the first poll sees "unknown"
        (session not in map) → treated as "idle" → returns immediately → turn
        ends in ~1.9s with zero SSE events captured.

        Two-phase poll:
          1. Wait for busy — poll until ``"busy"`` or ``"retry"`` (timeout:
             ``_BUSY_WAIT_TIMEOUT``). ``"idle"`` returns immediately. ``"unknown"``
             keeps polling.
          2. Wait for idle — poll until ``"idle"`` or ``"unknown"``.
        """
        deadline = asyncio.get_event_loop().time() + _BUSY_WAIT_TIMEOUT
        saw_busy = False
        while asyncio.get_event_loop().time() < deadline:
            status = await self._client.get_session_status_v1(session_id, directory=directory)
            if status in ("busy", "retry"):
                saw_busy = True
                break
            if status == "idle":
                return
            await asyncio.sleep(_ACTIVE_POLL_INTERVAL)

        if not saw_busy:
            logger.warning(
                "OpenCode session %s never became busy within %.1fs — "
                "prompt_async may have completed instantly or failed silently",
                session_id,
                _BUSY_WAIT_TIMEOUT,
            )
            return

        while True:
            if OpenCodeServerManager.is_process_dead():
                raise RuntimeError(
                    f"opencode process died during turn (session {session_id}) — "
                    "watchdog will respawn; next turn should recover"
                )
            try:
                status = await self._client.get_session_status_v1(session_id, directory=directory)
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                if OpenCodeServerManager.is_process_dead():
                    raise RuntimeError(
                        f"opencode process died during turn (session {session_id}): {exc}"
                    ) from exc
                raise
            if status in ("idle", "unknown"):
                return
            await asyncio.sleep(_ACTIVE_POLL_INTERVAL)

    async def _emit_fallback_text(
        self,
        session_id: str,
        on_emission: Callable[[Emission], Awaitable[None]],
        *,
        directory: str,
    ) -> None:
        try:
            messages = await self._client.get_messages_v1(session_id, directory=directory)
        except OpencodeV2Error:
            return
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            info = msg.get("info", {})
            if info.get("role") != "assistant":
                continue
            parts = msg.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text", "")
                    if text:
                        await on_emission(Emission(event=ExternalEvent.TEXT_DELTA, text=text))
                        return
            return
