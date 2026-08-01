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

from ...agent import StaleSessionError, StreamingProviderBackend, write_env_snapshot_for_session
from ...events import ExternalEvent
from ...paths import ExternalPaths
from ...types import BackendResult, BackendStatus, Emission, ExecOptions
from .server_manager import OpenCodeServerManager
from .session_state import OpenCodeSessionState
from .turn_waiter import TurnCompletionWaiter
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

    def __init__(self, quiesce_s: float = 3.0) -> None:
        self._handle: OpenCodeServerManager.ServerHandle | None = None
        self._quiesce_s = quiesce_s

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

    @property
    def _session_state(self) -> OpenCodeSessionState:
        assert self._handle is not None
        return self._handle.session_state

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

        write_env_snapshot_for_session(ExternalPaths(opts.workdir), env, session_id)

        text_seen = False

        async def _on_emission_tracked(emission: Emission) -> None:
            nonlocal text_seen
            if emission.event is ExternalEvent.TEXT_DELTA:
                text_seen = True
            await on_emission(emission)

        self._sse_reader.register_session(session_id, _on_emission_tracked)

        registry = self._session_state
        waiter = TurnCompletionWaiter(
            session_id,
            registry,
            client=self._client,
            directory=workdir_str,
            quiesce_s=self._quiesce_s,
        )
        registry.register_waiter(waiter)

        try:
            model_ref = _model_ref_from_str(opts.model)
            try:
                await self._client.prompt_async_v1(
                    session_id, opts.prompt, model=model_ref, directory=workdir_str
                )
            except OpencodeV2Error as exc:
                if exc.tag == "SessionNotFoundError":
                    raise StaleSessionError(f"OpenCode session {session_id} not found") from exc
                raise

            # Disconnect fallback: only poll busy if reader is reconnecting.
            # After reconnect, rebuild the subtree from authoritative REST state.
            if registry.is_reconnect_pending():
                await self._wait_busy_fallback(session_id, directory=workdir_str)
                await registry.rebuild_subtree(session_id, self._client, workdir_str)

            timeout = opts.timeout if opts.timeout and opts.timeout > 0 else _SSE_READ_TIMEOUT
            try:
                await asyncio.wait_for(waiter.wait_complete(), timeout=timeout)
            except TimeoutError:
                with contextlib.suppress(OpencodeV2Error):
                    await self._client.abort_session_v1(session_id, directory=workdir_str)
                return BackendResult(status=BackendStatus.TIMEOUT, session_id=session_id)
            except OpencodeV2Error as exc:
                logger.exception("OpenCode turn error")
                return BackendResult(
                    status=BackendStatus.FAILED, session_id=session_id, error=str(exc)
                )

            # Root session gone (opencode process restarted) → ERROR
            if registry.is_root_missing(session_id):
                return BackendResult(
                    status=BackendStatus.FAILED,
                    session_id=session_id,
                    error="OpenCode session lost (process may have restarted)",
                )

            if not text_seen:
                await self._emit_fallback_text(session_id, on_emission, directory=workdir_str)

            return BackendResult(status=BackendStatus.COMPLETED, session_id=session_id)
        finally:
            registry.unregister_waiter(waiter)
            # NOT unregister_session — output route preserved for cross-turn
            # reuse (design 5.6: "finally 不再 unregister_session"). Idle sids
            # are cleaned by LRU, not per-turn teardown.

    async def _wait_busy_fallback(self, session_id: str, *, directory: str) -> None:
        """Disconnect fallback: poll until the session becomes busy or idle.

        ONLY used when ``is_reconnect_pending()`` is True (SSE reader
        reconnecting). Confirms the prompt was received by the server before
        the ``TurnCompletionWaiter`` takes over via the event-driven path
        after reconnect + ``rebuild_subtree``.

        Does NOT wait for idle as a turn-completion signal — the waiter
        handles that. Best-effort: on network error or timeout, logs a
        warning and returns; the waiter + ``rebuild_subtree`` handle the rest.
        """
        deadline = asyncio.get_event_loop().time() + _BUSY_WAIT_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if OpenCodeServerManager.is_process_dead():
                return
            try:
                status = await self._client.get_session_status_v1(session_id, directory=directory)
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                if OpenCodeServerManager.is_process_dead():
                    return
                logger.warning(
                    "wait_busy_fallback: status poll failed for %s: %s",
                    session_id,
                    exc,
                )
                return
            if status in ("busy", "retry", "idle", "unknown"):
                return
            await asyncio.sleep(_ACTIVE_POLL_INTERVAL)
        logger.warning(
            "OpenCode session %s never became busy within %.1fs during reconnect fallback",
            session_id,
            _BUSY_WAIT_TIMEOUT,
        )

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
