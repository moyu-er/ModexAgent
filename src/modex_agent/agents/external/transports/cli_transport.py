"""``OpenCodeTransport`` — the CLI-subprocess transport for the OpenCode provider.

This is the external plane's one real transport today. It spawns (or
reuses) the shared ``opencode serve`` CLI subprocess through
:class:`OpenCodeServerManager`, drives one turn over the server's V1
session/prompt endpoints, and reads the subprocess's event stream
(JSONL lines over the ``/event`` SSE endpoint) through
:class:`OpenCodeV2SseReader` + :class:`OpenCodeV2EventParser`, which map
provider records onto core :class:`TurnEvent` records directly.

The server lifecycle (spawn/health/SSE) is owned by
:class:`OpenCodeServerManager` — a process-global singleton that shares
one ``opencode serve`` across ALL transports (main agents, subagents,
peers). This transport borrows the shared collaborators per turn via
``ServerHandle`` and releases them on close.

All session operations — creation, prompt dispatch, status polling,
message fallback, and abort — use V1 endpoints. V1 ``prompt_async`` runs
through ``SessionPrompt`` which injects ``promptOps`` into the tool
context; the ``task`` tool (subagent dispatch) requires this and is NOT
available on the V2 ``SessionRunner`` path.

The ``/event`` SSE stream carries both V2 (``session.next.*``) and V1
(``message.part.*``, ``session.created``) events; the parser and SSE
reader handle both envelope shapes. Provider-minted child sessions
(auto-discovered by the reader) get their delivery target from the
``on_child_event`` factory — core events carry no session identity, so
the reader's per-session callback demux is where the source session
attaches.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import override

import aiohttp

from modex_agent.core.turn_events import TurnEvent, TurnTextEvent

from ..env_builder import write_env_snapshot_for_session
from ..paths import ExternalPaths
from ..providers.opencode.server_manager import OpenCodeServerManager
from ..providers.opencode.session_state import OpenCodeSessionState
from ..providers.opencode.turn_waiter import TurnCompletionWaiter
from ..providers.opencode.v2_client import (
    ModelRef,
    OpencodeV2Client,
    OpencodeV2Error,
)
from ..providers.opencode.v2_sse_reader import OpenCodeV2SseReader
from ..types import BackendResult, BackendStatus, ExecOptions
from .abc import (
    ChildTurnEventCallbackFactory,
    ExternalTransport,
    StaleSessionError,
    TurnEventCallback,
)

logger = logging.getLogger(__name__)

__all__ = ["OpenCodeTransport"]

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


class OpenCodeTransport(ExternalTransport):
    """Shared-server transport — borrows collaborators from ``OpenCodeServerManager``.

    Each :meth:`execute` call acquires a :class:`ServerHandle` from the
    singleton manager. The handle carries the shared HTTP client, parser,
    and SSE reader. The transport does NOT own the server process — that
    lifecycle is the manager's responsibility (refcounted across all
    transports).
    """

    def __init__(self, quiesce_s: float = 3.0) -> None:
        self._handle: OpenCodeServerManager.ServerHandle | None = None
        self._quiesce_s = quiesce_s

    @property
    def _client(self) -> OpencodeV2Client:
        assert self._handle is not None
        return self._handle.client

    @property
    def _sse_reader(self) -> OpenCodeV2SseReader:
        assert self._handle is not None
        return self._handle.sse_reader

    @property
    def _session_state(self) -> OpenCodeSessionState:
        assert self._handle is not None
        return self._handle.session_state

    async def _ensure_server(self, workdir: Path, env: dict[str, str]) -> None:
        self._handle = await OpenCodeServerManager.acquire(workdir, env)

    @override
    async def close(self) -> None:
        # No-op: the shared server lifecycle belongs to
        # OpenCodeServerManager, not to an individual transport.
        pass

    @override
    async def execute(
        self,
        opts: ExecOptions,
        env: Mapping[str, str],
        on_event: TurnEventCallback,
        on_child_event: ChildTurnEventCallbackFactory | None = None,
    ) -> BackendResult:
        spawn_env = dict(env)
        await self._ensure_server(opts.workdir, spawn_env)

        workdir_str = str(opts.workdir)

        if opts.resume_session_id:
            session_id = opts.resume_session_id
        else:
            session_id = await self._client.create_session_v1(workdir_str)
        self._handle.register_session(session_id)

        write_env_snapshot_for_session(ExternalPaths(opts.workdir), spawn_env, session_id)

        text_seen = False

        async def _on_event_tracked(event: TurnEvent) -> None:
            nonlocal text_seen
            if event.kind == "text":
                text_seen = True
            await on_event(event)

        self._sse_reader.register_session(session_id, _on_event_tracked, on_child_event)

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
                await self._emit_fallback_text(session_id, on_event, directory=workdir_str)

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
        on_event: TurnEventCallback,
        *,
        directory: str,
    ) -> None:
        """No streamed text arrived — recover the assistant text from REST.

        The provider occasionally completes a turn without delivering any
        text delta on the event stream; the authoritative message list
        still has it. Best-effort: on fetch error, silently give up.
        """
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
                        await on_event(TurnTextEvent(text=text))
                        return
            return
