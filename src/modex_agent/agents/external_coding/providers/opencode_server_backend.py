"""``OpenCodeServerBackend`` — SSE-based streaming backend for opencode.

Replaces the subprocess+stdout model (:class:`OpenCodeBackend`) with a
long-running ``opencode serve`` process whose SSE event stream provides
true token-level streaming.

Architecture:

1. **Server lifecycle** — ``_ensure_server`` starts ``opencode serve`` as
   a subprocess on a random localhost port. The server persists across
   turns; it is restarted only when the workspace directory or
   ``MODEX_SESSION_ID`` changes (i.e. a new conversation).
2. **SSE subscription** — a persistent ``GET /event`` connection
   consumes events for all sessions in the directory. Events are
   filtered by ``sessionID`` in the parser.
3. **Session management** — ``POST /session`` creates a new opencode
   session; resumed sessions reuse the stored provider session ID.
4. **Prompt** — ``POST /session/:id/prompt_async`` returns 204
   immediately; all streaming happens through the SSE connection.
5. **Permission** — ``permission.asked`` events trigger automatic
   ``POST /session/:id/permission/:rid/reply`` (replaces
   ``--dangerously-skip-permissions``).
6. **Completion** — ``session.status`` with ``status.type == "idle"``
   signals the turn is done.

Env vars (``MODEX_*``) are injected into the server subprocess so that
``modexctl`` (called by opencode's bash tools) inherits them. The vars
are stable within a conversation, so a single server instance serves all
turns of that conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import override

import aiohttp

from ..agent import StreamingProviderBackend
from ..os_layer import resolve_executable, spawn_process_group, terminate_process_group
from ..types import BackendResult, BackendStatus, Emission, ExecOptions
from .opencode_sse_parser import OpenCodeSSEParser, SSEEventType

logger = logging.getLogger(__name__)

__all__ = ["OpenCodeServerBackend", "SSEUnavailableError"]

_SERVER_READY_TIMEOUT: float = 30.0
_HEALTH_POLL_INTERVAL: float = 0.5
_SSE_READ_TIMEOUT: float = 300.0


class SSEUnavailableError(RuntimeError):
    """Raised when the OpenCode SSE server fails to start or becomes unhealthy."""


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenCodeServerBackend(StreamingProviderBackend):
    """SSE-based backend — ``opencode serve`` + ``GET /event`` stream."""

    def __init__(self) -> None:
        self._server_proc: asyncio.subprocess.Process | None = None
        self._server_url: str | None = None
        self._server_workdir: Path | None = None
        self._server_modex_sid: str | None = None
        self._server_env_fingerprint: str = ""
        self._http_session: aiohttp.ClientSession | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    _PER_TURN_ENV_KEYS: tuple[str, ...] = (
        "MODEX_SESSION_ID",
        "MODEX_TARGETS",
        "MODEX_AGENT_POOL_MAP",
    )

    async def _ensure_server(
        self, workdir: Path, env: dict[str, str]
    ) -> str:
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("OpenCode server backend is closed")
            return await self._ensure_server_locked(workdir, env)

    async def _ensure_server_locked(
        self, workdir: Path, env: dict[str, str]
    ) -> str:
        env_fingerprint = "|".join(
            f"{k}={env.get(k, '')}" for k in self._PER_TURN_ENV_KEYS
        )
        if (
            self._server_proc is not None
            and self._server_url is not None
            and self._server_workdir == workdir
            and self._server_env_fingerprint == env_fingerprint
            and self._server_proc.returncode is None
        ):
            return self._server_url

        await self._stop_server()

        port = _find_free_port()
        resolved = resolve_executable("opencode", logger)
        full_args = [
            resolved.argv0,
            *resolved.extra_args,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ]

        spawn_env = dict(env)
        spawn_env["PWD"] = str(workdir)

        self._server_proc = await spawn_process_group(
            full_args,
            cwd=workdir,
            env=spawn_env,
            stdin=asyncio.subprocess.DEVNULL,
        )

        self._server_url = f"http://127.0.0.1:{port}"
        self._server_workdir = workdir
        self._server_env_fingerprint = env_fingerprint

        try:
            await self._wait_ready()
        except BaseException:  # noqa: BLE001 - startup rollback must include cancellation
            try:  # noqa: SIM105 - a bare raise below preserves the startup exception
                await self._stop_server()
            except BaseException:  # noqa: BLE001 - preserve the active startup exception
                pass
            raise
        logger.info("opencode server ready at %s", self._server_url)
        return self._server_url

    async def _wait_ready(self) -> None:
        assert self._server_url is not None
        deadline = asyncio.get_event_loop().time() + _SERVER_READY_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if self._server_proc is not None and self._server_proc.returncode is not None:
                raise SSEUnavailableError(
                    f"opencode serve exited early (code={self._server_proc.returncode})"
                )
            try:
                session = self._ensure_http_session()
                async with session.get(
                    f"{self._server_url}/global/health",
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as resp:
                    if resp.status == 200:
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass
            await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        raise SSEUnavailableError(
            f"opencode serve did not become ready within {_SERVER_READY_TIMEOUT}s"
        )

    async def _stop_server(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        if self._server_proc is not None and self._server_proc.returncode is None:
            await terminate_process_group(self._server_proc)
            try:
                await asyncio.wait_for(self._server_proc.wait(), timeout=5.0)
            except TimeoutError:
                if self._server_proc.returncode is None:
                    self._server_proc.kill()
                    await self._server_proc.wait()
        self._server_proc = None
        self._server_url = None
        self._server_workdir = None
        self._server_modex_sid = None

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closed = True
            await self._stop_server()

    def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_read=_SSE_READ_TIMEOUT),
            )
        return self._http_session

    async def _create_session(self, server_url: str, workdir: str) -> str:
        session = self._ensure_http_session()
        async with session.post(
            f"{server_url}/session",
            json={},
            headers={"x-opencode-directory": workdir},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["id"]

    async def _send_prompt_async(
        self, server_url: str, session_id: str, workdir: str, prompt: str, model: str | None,
        system_prompt: str | None = None,
    ) -> None:
        session = self._ensure_http_session()
        payload: dict[str, object] = {
            "parts": [{"type": "text", "text": prompt}],
        }
        if model:
            parts = model.split("/", 1)
            payload["model"] = {"providerID": parts[0], "modelID": parts[1]} if len(parts) == 2 else {"providerID": "", "modelID": model}
        if system_prompt:
            payload["system"] = system_prompt
        async with session.post(
            f"{server_url}/session/{session_id}/prompt_async",
            json=payload,
            headers={"x-opencode-directory": workdir},
        ) as resp:
            resp.raise_for_status()

    async def _auto_approve_permission(
        self, server_url: str, session_id: str, workdir: str, request_id: str
    ) -> None:
        session = self._ensure_http_session()
        async with session.post(
            f"{server_url}/session/{session_id}/permission/{request_id}/reply",
            json={"reply": "once"},
            headers={"x-opencode-directory": workdir},
        ) as resp:
            resp.raise_for_status()

    @override
    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        workdir_str = str(opts.workdir)
        server_url = await self._ensure_server(opts.workdir, env)

        parser = OpenCodeSSEParser()

        if opts.resume_session_id:
            session_id = opts.resume_session_id
        else:
            session_id = await self._create_session(server_url, workdir_str)
        parser.set_main_session(session_id)

        http_session = self._ensure_http_session()
        sse_url = f"{server_url}/event"
        headers = {"x-opencode-directory": workdir_str, "Accept": "text/event-stream"}

        # Open SSE subscription BEFORE sending the prompt so no early
        # session.status idle event is missed (fast prompts can complete
        # in <100ms — well within the window of a sequential open-then-send).
        sse_resp = await http_session.get(sse_url, headers=headers)
        turn_active = False
        timeout = opts.timeout if opts.timeout and opts.timeout > 0 else _SSE_READ_TIMEOUT
        try:
            sse_resp.raise_for_status()

            await self._send_prompt_async(
                server_url, session_id, workdir_str, opts.prompt, opts.model,
                system_prompt=opts.system_prompt,
            )

            async def _consume_sse() -> None:
                nonlocal turn_active
                async for raw_line in sse_resp.content:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    json_str = line[len("data:"):].strip()
                    if not json_str:
                        continue
                    try:
                        event_obj = json.loads(json_str)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    evt_type = event_obj.get("type")
                    props = event_obj.get("properties", {})

                    if evt_type == SSEEventType.PERMISSION_ASKED:
                        perm_sid = props.get("sessionID", session_id)
                        perm_rid = props.get("id", "")
                        if perm_rid:
                            asyncio.create_task(
                                self._auto_approve_permission(
                                    server_url, perm_sid, workdir_str, perm_rid
                                )
                            )
                        continue

                    if evt_type == SSEEventType.SESSION_STATUS:
                        status_obj = props.get("status", {})
                        if not isinstance(status_obj, dict):
                            continue
                        status_type = status_obj.get("type")
                        if status_type == "busy":
                            turn_active = True
                            continue
                        if status_type == "idle" and turn_active:
                            if props.get("sessionID") == session_id:
                                break
                        continue

                    if evt_type == SSEEventType.SESSION_ERROR:
                        if props.get("sessionID") == session_id:
                            for emission in parser.parse_line(json_str):
                                await on_emission(emission)
                            break
                        continue

                    for emission in parser.parse_line(json_str):
                        turn_active = True
                        await on_emission(emission)

            await asyncio.wait_for(_consume_sse(), timeout=timeout)
        except asyncio.TimeoutError:
            return BackendResult(status=BackendStatus.TIMEOUT, session_id=session_id)
        except Exception as exc:
            logger.exception("OpenCode SSE backend error")
            return BackendResult(
                status=BackendStatus.FAILED, session_id=session_id, error=str(exc)
            )
        finally:
            sse_resp.close()

        return BackendResult(status=BackendStatus.COMPLETED, session_id=session_id)
