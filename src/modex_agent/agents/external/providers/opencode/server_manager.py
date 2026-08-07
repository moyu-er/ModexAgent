"""Global ``opencode serve`` process manager — singleton with liveness check.

One ``opencode serve`` process shared across ALL ``OpenCodeServerBackend``
instances (main agents, subagents, peer pools). The opencode server supports
multi-workdir routing via the ``x-opencode-directory`` header.

Design:

1. **Lazy singleton** — spawned on first ``acquire()``. Stays alive for the
   bot's lifetime. No refcount, no teardown-until-idle.

2. **Liveness check** — every ``acquire()`` checks if the process is still
   running. If it died (crash, OOM, etc.), a new one is spawned.

3. **Per-workdir SSE readers** — opencode's ``/event`` endpoint filters
   events by ``x-opencode-directory``. Each workdir gets its own SSE
   connection + parser (parser supports multi main-session via
   ``add_main_session``/``remove_main_session``). Readers are cached and
   reused across turns within the same workdir.

4. **Orphan reaping** — on first ``acquire()``, kill any ``opencode serve``
   processes WE spawned in a prior run that were orphaned by a crash.
   On-disk PID registry, one JSON file per PID.

5. **External server** — ``OPENCODE_HOST`` env var connects to an
   already-running server instead of spawning.

6. **Multi-process safety** — the PID registry + orphan reaper ensures that
   even if two bot processes start (e.g. dev + prod), each manages only its
   own spawned processes. A live process owned by another bot is never
   killed.

Concurrency: all mutations go through an ``asyncio.Lock``.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import weakref
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from ...os_layer import (
    _sync_kill_proc,
    resolve_executable,
    spawn_process_group,
    terminate_process_group,
)
from .session_state import OpenCodeSessionState
from .v2_client import OpencodeV2Client
from .v2_parser import OpenCodeV2EventParser
from .v2_sse_reader import OpenCodeV2SseReader

logger = logging.getLogger(__name__)

_SERVER_READY_TIMEOUT: float = 30.0
_HEALTH_POLL_INTERVAL: float = 0.5
_HEALTH_CHECK_INTERVAL: float = 5.0
_MAX_CONSECUTIVE_FAILURES: int = 20
_STALE_BUSY_GRACE: float = 120.0
_SHUTDOWN_WAIT_ACTIVE: float = 5.0


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class _WorkdirEntry:
    sse_reader: OpenCodeV2SseReader
    parser: OpenCodeV2EventParser
    session_state: OpenCodeSessionState
    main_sessions: set[str] = field(default_factory=set)


class OpenCodeServerManager:
    """Singleton managing one shared ``opencode serve`` process.

    The server is spawned on first ``acquire()`` and stays alive. If the
    process dies between calls, the next ``acquire()`` detects it and
    respawns. SSE readers are cached per workdir.
    """

    _instance: OpenCodeServerManager | None = None
    _reaped: bool = False
    _lifecycle_bound: bool = False

    class ServerHandle:
        __slots__ = (
            "server_url",
            "client",
            "parser",
            "sse_reader",
            "session_state",
            "_manager",
            "_workdir",
        )

        def __init__(
            self,
            server_url: str,
            client: OpencodeV2Client,
            parser: OpenCodeV2EventParser,
            sse_reader: OpenCodeV2SseReader,
            session_state: OpenCodeSessionState,
            manager: OpenCodeServerManager,
            workdir: str,
        ) -> None:
            self.server_url = server_url
            self.client = client
            self.parser = parser
            self.sse_reader = sse_reader
            self.session_state = session_state
            self._manager = manager
            self._workdir = workdir

        def register_session(self, session_id: str) -> None:
            self.parser.add_main_session(session_id)
            self._manager._active_sessions.add(session_id)

        def unregister_session(self, session_id: str) -> None:
            self.parser.remove_main_session(session_id)
            self._manager._active_sessions.discard(session_id)

        async def release(self) -> None:
            pass

    class _LifecycleContext:
        """Async context manager for ``OpenCodeServerManager`` lifecycle.

        Bot top-level holds this via ``async with OpenCodeServerManager.lifecycle():``.
        On exit, ``_shutdown()`` triggers graceful cleanup (stops readers,
        terminates the shared process, clears the singleton) — only at bot
        shutdown, never as a standalone public API.
        """

        def __init__(self, manager: OpenCodeServerManager) -> None:
            self._manager = manager

        async def __aenter__(self) -> OpenCodeServerManager:
            return self._manager

        async def __aexit__(self, *args: object) -> None:
            await self._manager._shutdown()

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._server_url: str | None = None
        self._client: OpencodeV2Client | None = None
        self._workdir_entries: dict[str, _WorkdirEntry] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._finalizer: weakref.finalize | None = None
        self._pid_registered: bool = False
        self._active_sessions: set[str] = set()
        self._shutting_down: bool = False
        self._consecutive_failures: int = 0
        self._first_failure_time: float | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_env: dict[str, str] | None = None
        self._last_workdir: Path | None = None

    def _is_process_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @classmethod
    def is_process_dead(cls) -> bool:
        """True if the shared process has exited (``returncode is not None``).

        Returns False when no singleton exists or no process was spawned —
        those are absence states, not death.
        """
        mgr = cls._instance
        if mgr is None or mgr._proc is None:
            return False
        return mgr._proc.returncode is not None

    @classmethod
    async def acquire(cls, workdir: Path, env: dict[str, str]) -> ServerHandle:
        workdir_str = str(workdir)
        mgr = cls._instance
        if mgr is None:
            mgr = cls()
            cls._instance = mgr
        async with mgr._lock:
            if mgr._shutting_down:
                raise RuntimeError("OpenCodeServerManager is shutting down — cannot acquire")
            if not cls._reaped:
                await mgr._reap_orphaned_processes()
                cls._reaped = True

            if mgr._server_url is None or mgr._client is None or not mgr._is_process_alive():
                if mgr._server_url is not None and not mgr._is_process_alive():
                    logger.warning("opencode serve process died — respawning")
                    mgr._last_env = env
                    mgr._last_workdir = workdir
                    await mgr._respawn_locked()
                else:
                    await mgr._start_server(workdir, env)
                    mgr._last_env = env
                    mgr._last_workdir = workdir
                mgr._start_watchdog()

            entry = mgr._workdir_entries.get(workdir_str)
            if entry is None:
                parser = OpenCodeV2EventParser()
                sse_reader = OpenCodeV2SseReader(
                    server_url=mgr._server_url,
                    workdir=workdir_str,
                    parser=parser,
                )
                session_state = OpenCodeSessionState()
                sse_reader.attach_session_state(session_state)
                await sse_reader.start()
                entry = _WorkdirEntry(
                    sse_reader=sse_reader,
                    parser=parser,
                    session_state=session_state,
                )
                mgr._workdir_entries[workdir_str] = entry
                logger.info("SSE reader started for workdir %s", workdir_str)

            assert mgr._server_url is not None
            assert mgr._client is not None
            return cls.ServerHandle(
                server_url=mgr._server_url,
                client=mgr._client,
                parser=entry.parser,
                sse_reader=entry.sse_reader,
                session_state=entry.session_state,
                manager=mgr,
                workdir=workdir_str,
            )

    async def _stop_readers_and_client(self) -> None:
        for entry in self._workdir_entries.values():
            await entry.sse_reader.stop()
        self._workdir_entries.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._server_url = None
        # Terminate the process BEFORE clearing _proc — otherwise the PID
        # reference is lost and _unregister_pid() returns early, leaking
        # the process. Capture the PID for cleanup before nulling _proc.
        pid_to_unregister: int | None = None
        if self._proc is not None and self._proc.returncode is None:
            await terminate_process_group(self._proc)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                if self._proc.returncode is None:
                    self._proc.kill()
                    await self._proc.wait()
        if self._proc is not None:
            pid_to_unregister = self._proc.pid
        self._proc = None
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        if pid_to_unregister is not None:
            self._unregister_pid_for(pid_to_unregister)

    async def _start_server(self, workdir: Path, env: dict[str, str]) -> None:
        external_host = os.environ.get("OPENCODE_HOST", "").strip()
        if external_host:
            self._server_url = external_host.rstrip("/")
            logger.info("Using external opencode server at %s", self._server_url)
        else:
            await self._spawn_server(workdir, env)

        self._client = OpencodeV2Client(self._server_url)
        try:
            await self._wait_ready()
        except BaseException:
            await self._stop_readers_and_client()
            raise
        logger.info("opencode server ready at %s", self._server_url)

    async def _spawn_server(self, workdir: Path, env: dict[str, str]) -> None:
        """Spawn the shared ``opencode serve`` process.

        The process cwd is left as ``None`` (inherits the bot's cwd)
        rather than pinned to ``workdir``. Per-session workdir routing
        is via the ``x-opencode-directory`` HTTP header set by the SSE
        reader and ``create_session_v1``, NOT the process cwd — so a
        stale/deleted first workdir can never poison the shared process.
        ``workdir`` is retained on the signature because the caller
        builds ``env`` (``OPENCODE_PERMISSION`` etc.) from it.
        """
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
        self._proc = await spawn_process_group(
            full_args,
            cwd=None,
            env=spawn_env,
            stdin=asyncio.subprocess.DEVNULL,
        )
        self._server_url = f"http://127.0.0.1:{port}"
        if self._finalizer is not None:
            self._finalizer.detach()
        self._finalizer = weakref.finalize(self, _sync_kill_proc, self._proc.pid)
        self._register_pid(self._proc.pid, port)

    async def _wait_ready(self) -> None:
        assert self._server_url is not None
        assert self._client is not None
        deadline = asyncio.get_event_loop().time() + _SERVER_READY_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                raise RuntimeError(f"opencode serve exited early (code={self._proc.returncode})")
            try:
                if await self._client.health():
                    return
            except (TimeoutError, aiohttp.ClientError, OSError, ConnectionError):
                pass
            await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        raise RuntimeError(f"opencode serve did not become ready within {_SERVER_READY_TIMEOUT}s")

    # ------------------------------------------------------------------
    # Health watchdog
    # ------------------------------------------------------------------

    def _start_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _stop_watchdog(self) -> None:
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
        self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        """Background health monitor — periodic check + auto-respawn.

        Two triggers:

        1. Process provably dead (``returncode is not None``) → immediate
           respawn.
        2. Health check failure → count; respawn after
           ``_MAX_CONSECUTIVE_FAILURES`` consecutive failures, unless an
           active session grants a grace period (up to
           ``_STALE_BUSY_GRACE`` seconds).
        """
        while not self._shutting_down:
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
            if self._shutting_down or self._proc is None:
                continue
            if self._proc.returncode is not None:
                logger.warning(
                    "opencode process died (rc=%s) — respawning",
                    self._proc.returncode,
                )
                try:
                    await self._respawn()
                except Exception:
                    logger.exception("Respawn failed after dead-process detection")
                continue
            try:
                healthy = await self._client.health() if self._client else False
            except Exception:
                healthy = False
            if not healthy:
                if self._consecutive_failures == 0:
                    self._first_failure_time = asyncio.get_event_loop().time()
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    if self._active_sessions and not self._is_stuck_too_long():
                        logger.warning(
                            "Health failing but %d sessions active — grace skip",
                            len(self._active_sessions),
                        )
                        continue
                    logger.warning(
                        "Health failed %d times — respawning",
                        self._consecutive_failures,
                    )
                    try:
                        await self._respawn()
                    except Exception:
                        logger.exception("Respawn failed after health-check threshold")
            else:
                self._consecutive_failures = 0
                self._first_failure_time = None

    def _is_stuck_too_long(self) -> bool:
        """True if health failures have persisted beyond ``_STALE_BUSY_GRACE``."""
        if self._first_failure_time is None:
            return False
        return (asyncio.get_event_loop().time() - self._first_failure_time) > _STALE_BUSY_GRACE

    async def _respawn_locked(self) -> None:
        """Respawn assuming caller holds ``_lock``.

        Stops old readers/client, clears active sessions + failure counters,
        spawns a new process using saved env/workdir.
        """
        await self._stop_readers_and_client()
        self._active_sessions.clear()
        self._consecutive_failures = 0
        self._first_failure_time = None
        if self._last_env is not None and self._last_workdir is not None:
            await self._start_server(self._last_workdir, self._last_env)
            logger.info("opencode process respawned at %s", self._server_url)
        else:
            logger.error("Cannot respawn — no saved env/workdir")

    async def _respawn(self) -> None:
        """Kill old process, spawn new one.

        SSE readers are cleared — next ``acquire()`` rebuilds them.
        Active sessions are lost — backends re-acquire on next turn.
        Holds ``_lock`` to avoid racing with concurrent ``acquire()``.
        """
        async with self._lock:
            if self._shutting_down:
                return
            await self._respawn_locked()

    @classmethod
    def lifecycle(cls) -> _LifecycleContext:
        """Return the lifecycle context manager.

        Bot top-level ``async with OpenCodeServerManager.lifecycle():`` holds
        the singleton's lifetime. On ``__aexit__``, ``_shutdown()`` runs the
        graceful cleanup (stops watchdog + readers, terminates the shared
        process, clears the singleton) — only at bot shutdown, never exposed
        as a standalone public API.

        The singleton has one lifecycle owner: a second ``lifecycle()`` call
        while bound raises ``RuntimeError``. ``acquire()`` still works inside
        or outside the context (lazy spawn either way; ``atexit`` is the
        safety net outside the lifecycle).
        """
        if cls._lifecycle_bound:
            raise RuntimeError(
                "OpenCodeServerManager lifecycle already bound — singleton has one lifecycle owner"
            )
        if cls._instance is None:
            cls._instance = cls()
        cls._lifecycle_bound = True
        return cls._LifecycleContext(cls._instance)

    async def _shutdown(self) -> None:
        """Private graceful cleanup — only called by lifecycle ``__aexit__``.

        Stops the watchdog, waits up to ``_SHUTDOWN_WAIT_ACTIVE`` seconds for
        active sessions to clear (force-closing with a warning on timeout),
        then stops readers/client, terminates the shared process, detaches
        the finalizer, and clears the singleton + lifecycle-bound flag.
        """
        self._shutting_down = True
        await self._stop_watchdog()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _SHUTDOWN_WAIT_ACTIVE
        while self._active_sessions and loop.time() < deadline:
            await asyncio.sleep(0.1)
        if self._active_sessions:
            logger.warning(
                "Force-closing OpenCodeServerManager with %d active sessions",
                len(self._active_sessions),
            )
        try:
            async with self._lock:
                for entry in self._workdir_entries.values():
                    await entry.sse_reader.stop()
                self._workdir_entries.clear()
                if self._client is not None:
                    await self._client.close()
                if self._proc is not None and self._proc.returncode is None:
                    await terminate_process_group(self._proc)
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                    except TimeoutError:
                        if self._proc.returncode is None:
                            self._proc.kill()
                            await self._proc.wait()
                self._unregister_pid()
                self._proc = None
                self._server_url = None
                self._client = None
                if self._finalizer is not None:
                    self._finalizer.detach()
                    self._finalizer = None
                self._active_sessions.clear()
                self._consecutive_failures = 0
                self._first_failure_time = None
        finally:
            OpenCodeServerManager._instance = None
            OpenCodeServerManager._lifecycle_bound = False

    @classmethod
    def reset_for_tests(cls) -> None:
        mgr = cls._instance
        if mgr is not None:
            mgr._shutting_down = True
            if mgr._watchdog_task is not None and not mgr._watchdog_task.done():
                mgr._watchdog_task.cancel()
            mgr._watchdog_task = None
            mgr._unregister_pid()
            if mgr._finalizer is not None:
                mgr._finalizer.detach()
                mgr._finalizer = None
            if mgr._proc is not None and mgr._proc.returncode is None:
                _sync_kill_proc(mgr._proc.pid)
            mgr._proc = None
        cls._instance = None
        cls._reaped = False
        cls._lifecycle_bound = False

    def _registry_dir(self) -> Path:
        return Path.home() / ".config" / "modexagent" / "managed-opencode"

    def _pid_file(self, pid: int) -> Path:
        return self._registry_dir() / f"{pid}.json"

    def _register_pid(self, pid: int, port: int) -> None:
        try:
            self._registry_dir().mkdir(parents=True, exist_ok=True)
            entry = {
                "pid": pid,
                "ownerPid": os.getpid(),
                "port": port,
                "binary": "opencode",
                "startedAt": _now_iso(),
            }
            tmp = self._pid_file(pid).with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
            tmp.rename(self._pid_file(pid))
            self._pid_registered = True
        except Exception:
            self._pid_registered = False

    def _unregister_pid(self) -> None:
        if not self._pid_registered or self._proc is None:
            return
        self._unregister_pid_for(self._proc.pid)

    def _unregister_pid_for(self, pid: int) -> None:
        try:
            self._pid_file(pid).unlink(missing_ok=True)
        except Exception:
            pass
        self._pid_registered = False

    async def _reap_orphaned_processes(self) -> None:
        registry_dir = self._registry_dir()
        if not registry_dir.exists():
            return
        for pid_file in registry_dir.glob("*.json"):
            try:
                entry = json.loads(pid_file.read_text(encoding="utf-8"))
                pid = entry.get("pid")
                if not isinstance(pid, int):
                    continue
                if not _is_pid_alive(pid):
                    pid_file.unlink(missing_ok=True)
                    continue
                owner_pid = entry.get("ownerPid")
                if isinstance(owner_pid, int) and _is_pid_alive(owner_pid):
                    if _is_python_process(owner_pid):
                        continue
                    logger.info(
                        "Reaping orphaned opencode process pid=%d (owner pid=%d recycled to non-python process)",
                        pid,
                        owner_pid,
                    )
                else:
                    logger.info("Reaping orphaned opencode process pid=%d (owner gone)", pid)
                _sync_kill_proc(pid)
                pid_file.unlink(missing_ok=True)
            except Exception:
                try:
                    pid_file.unlink(missing_ok=True)
                except Exception:
                    pass


def _is_python_process(pid: int) -> bool:
    """True if *pid* is a Python interpreter process.

    Guards against PID recycling: after a bot crash, the OS may reuse
    the bot's PID for an unrelated process. ``_is_pid_alive`` would
    return True, causing the orphan reaper to skip a stale
    ``opencode serve``. Checking the process name prevents that.
    """
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return "python" in result.stdout.lower()
        comm_path = Path(f"/proc/{pid}/comm")
        if comm_path.exists():
            return "python" in comm_path.read_text(errors="replace").lower()
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "python" in result.stdout.lower()
    except Exception:
        return False


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).isoformat()


def _atexit_cleanup() -> None:
    mgr = OpenCodeServerManager._instance
    if mgr is not None and mgr._proc is not None and mgr._proc.returncode is None:
        _sync_kill_proc(mgr._proc.pid)
        mgr._unregister_pid()


atexit.register(_atexit_cleanup)
