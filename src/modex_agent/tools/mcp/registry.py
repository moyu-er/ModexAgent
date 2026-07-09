"""Shared, concurrent, service-scoped MCP connection registry (opt-in overlay).

Per ADR-0017. This is an **optional overlay** on top of the existing
``MCPClientManager``: projects that do not wire the registry keep today's
behavior exactly. The registry deduplicates MCP connections by a **canonical
config-hash** and connects all servers concurrently via per-server supervisor
tasks, exposing a :class:`SharedMcpBackend` facade over the READY subset.

Supervisor-task model (anyio-safe)
-----------------------------------
``connect_single_server`` enters transport/``ClientSession`` contexts into the
passed :class:`~contextlib.AsyncExitStack`. The MCP SDK uses anyio cancel
scopes, and such a stack MUST be closed in the **same asyncio Task that created
it** — a cross-task ``aclose`` raises ``RuntimeError``. Therefore each server
is owned by ONE long-lived supervisor task that: creates the stack → calls the
connect primitive → on success idles (awaits a wake or shutdown signal) → on
shutdown closes the stack **in that same task**. A naive ``asyncio.gather``
where each child enters a stack and then exits would leave no live task able to
close it safely, so it is explicitly avoided.

Drop recovery (passive reconnect)
---------------------------------
The supervisor NEVER exits on connect failure; it idles forever servicing
reconnect requests, so a never-connected or once-dropped server can recover.

Detection is **passive** — there is no active health polling. Because the
shared connection is long-lived across workspace materializations, it can
DROP between two materializations. A dropped session is silent: the mcp SDK
never notifies the idle supervisor (no cancel-scope cancellation reaches
``wake_event.wait()``), so ``entry.state`` stays ``READY``; a dead session
yields ``[]`` from ``list_tools`` and ``{"success": False, "error": ""}`` from
``execute_tool`` — neither of which matches a naive "not connected" check.
So the facade detects the drop itself, at the operation boundary:

- **Registration path** (``SharedMcpBackend.list_tools``): a READY entry that
  *previously served tools* returning ``[]`` is a dropped session → reconnect
  once and retry. A genuinely-empty server (never had tools) does NOT trigger
  a reconnect (no prior evidence it should have tools).
- **Call path** (``execute_tool`` / ``read_resource`` / ``get_prompt``): a
  non-success result whose error is empty or contains "not connected" →
  reconnect once and retry.

Either way the facade asks the registry to reconnect via
:meth:`McpConnectionRegistry.request_reconnect`, which coalesces concurrent
requests onto one attempt. The reconnect runs IN the supervisor task (in-place
stack + client swap, anyio-safe) with exponential backoff mirroring
``MCPClientManager.reconnect_with_retry`` (the reconnect *execution* is thus
borrowed wholesale from MCPClientManager; only the *detection* is new, because
MCPClientManager — whose connections are short-lived and re-created per pool —
never needed to detect a dropped-but-still-present session).

Known limitations (deliberate simplicity, ADR-scoped)
-----------------------------------------------------
- **No retry-on-failure at initial connect.** The initial connect is a single
  attempt: READY or FAILED. (A FAILED entry is recoverable later via a
  reconnect request, but the registry does not itself retry the first connect.)
- **No reference counting.** ADR-0017's literal wording proposes
  reference-counted per-acquisition connections. Because the registry is
  service-scoped and shared, close-on-release-to-zero would kill a connection
  that other workspaces still use — wrong. We deliberately deviate:
  ``release()`` only detaches the facade, and connections close **only at
  shutdown**. This honors the ADR's intent (service-scoped sharing) while
  trading the refcount mechanic for simplicity.

Open-config caveat (type-safety rule 14)
----------------------------------------
Per-server MCP config is genuinely open-shaped: stdio has ``command``/``args``/
``env``, http has ``url``/``headers``, and unknown fields are permitted. It is
therefore modeled as ``dict[str, Any]`` at every boundary (constructor,
``connect_fn`` signature), not as a Pydantic ``BaseModel``. The canonical hash
below is the structured handle on that open shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, suppress
from enum import Enum
from typing import Any

from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import _DEFAULT_TOOL_TIMEOUT, BaseMCPClient
from modex_agent.tools.mcp.connection import connect_single_server
from modex_agent.tools.mcp.injector import (
    JsonFileMCPTransportInjector,
    MCPTransportInjector,
)

__all__ = ["McpConnectionRegistry", "SharedMcpBackend", "McpConnectionState"]

_logger = logging.getLogger(__name__)

# Signature of the per-server connect primitive driven by supervisors.
# Matches ``connect_single_server(name, server_config, *, injector, stack)``.
# ``dict[str, Any]`` is justified per the module docstring (open MCP config).
ConnectFn = Callable[..., Awaitable[BaseMCPClient]]


class McpConnectionState(str, Enum):
    """Lifecycle state of a registry-tracked connection.

    Transitions: ``CONNECTING`` → ``READY`` (success, idles until shutdown) or
    ``CONNECTING`` → ``FAILED`` (connect raised). ``READY`` and ``FAILED`` are
    the states :meth:`McpConnectionRegistry.acquire` waits on, but neither is
    terminal for the supervisor — a FAILED entry stays idle and a later
    reconnect request can move it back to ``CONNECTING`` → ``READY``.
    """

    CONNECTING = "connecting"
    READY = "ready"
    FAILED = "failed"


def _canonicalize(cfg: dict[str, Any]) -> str:
    """Return a stable canonical JSON string for an open MCP server-config dict.

    Sorts keys and coerces non-JSON-native values (e.g. tuples → lists) so the
    result is independent of dict insertion order and Python-only literal
    forms. List order is preserved (e.g. stdio ``args`` order is semantically
    significant); only mapping keys are sorted. This is the canonical form
    :func:`_config_dedup_key` hashes; it is NOT used directly as the dedup key
    (it contains secrets — env/headers — so the key is its SHA-256 instead).
    """
    return json.dumps(cfg, sort_keys=True, default=_json_default, ensure_ascii=False)


def _config_dedup_key(cfg: dict[str, Any]) -> str:
    """Return a stable, secret-redacting dedup key for an MCP server config.

    Two configs with identical keys share one underlying connection. The key is
    the SHA-256 hexdigest of the canonical JSON: stable across dict insertion
    order and Python literal forms, and — unlike the raw canonical string — it
    does not expose secrets (``env``/``headers``) if the entry or its config is
    ever logged or serialized. Note: including secrets in the canonical form is
    the *safe* direction for dedup — two configs differing only by a secret get
    distinct keys and are NOT silently merged.
    """
    return hashlib.sha256(_canonicalize(cfg).encode("utf-8")).hexdigest()


def _json_default(obj: Any) -> Any:  # noqa: ANN401 - json.dumps default= callback contract is inherently Any
    """Coerce Python-only literals to JSON-native forms for canonicalization."""
    if isinstance(obj, tuple | set | frozenset):
        # Sort for determinism when elements are comparable; fall back to str.
        try:
            return sorted(obj)
        except TypeError:
            return sorted(str(x) for x in obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


def _root_cause(exc: BaseException | None) -> BaseException | None:
    """Unpack the first leaf of a :class:`BaseExceptionGroup` (Python 3.11+).

    A streamable_http/sse connect failure (e.g. an ``httpx`` 4xx/5xx, or a
    dropped connection) originates in the mcp transport's *background* task.
    anyio cancels the host cancel-scope (so ``connect_single_server`` raises a
    ``CancelledError``), and the real error is trapped inside the transport's
    TaskGroup, surfacing only when the stack closes — wrapped in a
    ``BaseExceptionGroup``. This unwraps that group so callers can log the
    actual error instead of the opaque "ExceptionGroup (1 sub-exception)".
    """
    while isinstance(exc, BaseExceptionGroup):
        subs = list(exc.exceptions)
        if not subs:
            break
        exc = subs[0]
    return exc


class _ConnectionEntry:
    """Coordination record for one deduplicated connection (internal).

    NOT a frozen dataclass / Pydantic model: it holds a live ``supervisor_task``,
    a mutable ``state``, a ``ready_event``, and an open ``stack`` — a
    coordination object owning state and connections, which architecture rule
    12 explicitly excludes from the frozen-``BaseModel`` policy.

    ``ready_event`` is set on reaching a terminal state (READY or FAILED), so
    :meth:`McpConnectionRegistry.acquire` can wait on it once for either outcome.
    """

    def __init__(self, name: str, config_hash: str, config: dict[str, Any]) -> None:
        self.name: str = name
        self.config_hash: str = config_hash
        # Store a defensive copy so later caller-side mutation of the input dict
        # does not retroactively change what was canonicalized at ensure time.
        self.config: dict[str, Any] = dict(config)
        self.state: McpConnectionState = McpConnectionState.CONNECTING
        self.client: BaseMCPClient | None = None
        self.supervisor_task: asyncio.Task[None] | None = None
        self.ready_event: asyncio.Event = asyncio.Event()
        self.stack: AsyncExitStack = AsyncExitStack()
        # Reconnect coordination (drop recovery). ``wake_event`` is set to wake
        # the idle supervisor (a reconnect request OR shutdown), replacing the
        # old ``await self._shutdown_event.wait()`` idle. A reconnect in flight
        # sets ``reconnect_in_progress`` and a Future coalescing concurrent
        # requests; the requester awaits ``reconnect_future``.
        self.wake_event: asyncio.Event = asyncio.Event()
        self.reconnect_in_progress: bool = False
        self.reconnect_future: asyncio.Future[bool] | None = None
        # Drop-recovery detection for the registration path. Set True the first
        # time a ``list_tools`` query returns a non-empty result. A READY entry
        # later returning ``[]`` means the shared session has died — a dead
        # session yields ``[]`` without raising (confirmed against the real
        # stdio transport: subprocess killed → ``list_tools`` returns ``[]``,
        # ``execute_tool`` returns ``{"success": False, "error": ""}``, and the
        # supervisor is never notified, so ``state`` stays READY). A previously-
        # populated entry going empty is therefore the only reliable drop signal
        # for the registration query; a genuinely-empty server (never had tools)
        # does NOT trigger a reconnect.
        self.had_tools: bool = False

    async def terminal_event(self) -> None:
        """Await the entry's terminal state (READY or FAILED)."""
        await self.ready_event.wait()


class McpConnectionRegistry:
    """Service-scoped registry of shared MCP connections.

    Deduplicates connections by canonical config-hash, connects all requested
    servers concurrently via per-server supervisor tasks, and exposes a
    :class:`SharedMcpBackend` facade over the READY subset via :meth:`acquire`.

    The registry is a coordination object owning live tasks and connections, so
    it is a regular mutable class (architecture rule 12 exception).
    """

    def __init__(
        self,
        servers: dict[str, dict[str, Any]],
        *,
        injector: MCPTransportInjector | None = None,
        connect_fn: ConnectFn | None = None,
        reconnect_backoff: tuple[float, ...] | None = None,
    ) -> None:
        # Defensive shallow copy of the name→cfg map: callers may mutate their
        # own dict after construction; the registry snapshots the mapping.
        self._server_configs: dict[str, dict[str, Any]] = {
            name: dict(cfg) for name, cfg in servers.items()
        }
        self._injector: MCPTransportInjector = (
            injector if injector is not None else JsonFileMCPTransportInjector()
        )
        self._connect_fn: ConnectFn = (
            connect_fn if connect_fn is not None else connect_single_server
        )
        # Test seam for ``_connect_with_backoff``: explicit inter-retry delays.
        # When None, backoff is computed as ``base_delay * 2**attempt`` (capped
        # at ``max_delay``), mirroring ``MCPClientManager.reconnect_with_retry``.
        # Tests pass e.g. ``reconnect_backoff=(0.0, 0.0)`` for instant retries.
        self._reconnect_backoff: tuple[float, ...] | None = reconnect_backoff
        # Keyed by canonical config-hash (the dedup key).
        self._entries: dict[str, _ConnectionEntry] = {}
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._closed: bool = False

    # -- internal: ensure a supervisor exists for a name (idempotent) ---------

    def _ensure(self, name: str) -> _ConnectionEntry | None:
        """Resolve ``name`` → config, ensure a supervisor exists for its hash.

        Returns the entry (existing or freshly spawned), or ``None`` if the name
        is unknown (logged and skipped). Idempotent: a second call for the same
        name returns the same entry; a different name with an identical config
        hash returns the shared entry (dedup). Spawns a supervisor task that
        owns the stack lifecycle.
        """
        if self._closed:
            raise RuntimeError("registry shut down")
        cfg = self._server_configs.get(name)
        if cfg is None:
            _logger.warning("[MCP Registry] unknown server name skipped: %s", name)
            return None
        config_hash = _config_dedup_key(cfg)
        existing = self._entries.get(config_hash)
        if existing is not None:
            return existing
        entry = _ConnectionEntry(name=name, config_hash=config_hash, config=cfg)
        self._entries[config_hash] = entry
        entry.supervisor_task = asyncio.create_task(
            self._run_supervisor(entry),
            name=f"mcp-supervisor:{name}",
        )
        return entry

    async def _run_supervisor(self, entry: _ConnectionEntry) -> None:
        """Own one connection's stack for its entire lifetime, in this task.

        Unified supervisor: a single initial connect attempt, then idle forever
        servicing reconnect requests. NEVER exits due to connect failure — even
        a FAILED entry stays idle so a later reconnect request can recover a
        never-connected or once-dropped server. Only shutdown exits the loop.

        Phase 1: initial connect (single attempt, current semantics). READY or
        FAILED, but the supervisor does NOT exit on failure.
        Phase 2: idle on ``wake_event`` until shutdown; a wake with
        ``reconnect_in_progress`` set drives ``_handle_reconnect`` (in-place
        stack + client swap, in THIS task — anyio-safe).

        The outer ``finally`` closes the CURRENT stack IN THIS TASK on every
        exit path (anyio requirement). Exit paths:
        - Shutdown wakes an idle supervisor (READY/FAILED): ``wake_event`` set,
          loop sees ``_shutdown_event`` and breaks → finally.
        - Shutdown cancels a CONNECTING supervisor (stuck inside ``connect_fn``
          during initial connect OR inside ``_connect_with_backoff`` during a
          reconnect): ``CancelledError`` unwinds through the same finally.

        ``CancelledError`` is NEVER swallowed — it propagates through
        ``_attempt_connect`` / ``_connect_with_backoff`` / ``wake_event.wait()``
        to the finally, which closes the partial stack in-task.
        """
        try:
            # Phase 1: initial connect attempt. Single attempt (current
            # semantics): READY or FAILED, but do NOT exit on failure — stay
            # idle so a later reconnect request can recover even a
            # never-connected server.
            await self._attempt_connect(entry)
            # Phase 2: idle until shutdown, servicing reconnect requests.
            while not self._shutdown_event.is_set():
                await entry.wake_event.wait()
                entry.wake_event.clear()
                if self._shutdown_event.is_set():
                    break
                if entry.reconnect_in_progress:
                    await self._handle_reconnect(entry)
        finally:
            # Close the CURRENT stack IN THIS TASK on every exit path (anyio).
            try:
                await entry.stack.aclose()
            except Exception as exc:  # noqa: BLE001 - never let close crash shutdown
                _logger.warning(
                    "[MCP Registry] error closing stack for %s: %s: %s",
                    entry.name,
                    type(exc).__name__,
                    exc,
                )

    async def _attempt_connect(self, entry: _ConnectionEntry) -> None:
        """Single initial connect attempt using ``entry.stack``.

        On success: assigns the client, READY, ``ready_event``. On failure:
        FAILED, ``ready_event`` (supervisor stays idle — does NOT exit, so a
        later reconnect request can recover it).

        ``CancelledError`` is ambiguous here. A *genuine* shutdown cancels
        CONNECTING supervisors (``shutdown()`` sets ``_shutdown_event`` then
        cancels the task) — that must propagate to unwind the supervisor. But a
        streamable_http/sse transport failure (e.g. an HTTP 4xx/5xx during
        ``session.initialize()``) makes the mcp SDK tear down its anyio
        TaskGroup, which surfaces as a cancel-scope ``CancelledError`` whose
        real cause is trapped inside the TaskGroup (visible only on stack
        close, as a ``BaseExceptionGroup``). Re-raising THAT would crash this
        supervisor and orphan the entry. So when shutdown is NOT in progress,
        every failure — ``Exception``, ``BaseExceptionGroup``, or a
        cancel-scope ``CancelledError`` — is recorded as a connect failure via
        :meth:`_fail_connect` (parity with ``MCPClientManager._connect_single``).
        """
        try:
            client = await self._connect_fn(
                entry.name,
                entry.config,
                injector=self._injector,
                stack=entry.stack,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except asyncio.CancelledError:
            # Genuine shutdown (event set before cancel) → unwind. Anything
            # else is an anyio cancel-scope cancellation from a background
            # transport failure → treat as a connect failure, do NOT crash.
            if self._shutdown_event.is_set():
                raise
            await self._fail_connect(entry, None)
            return
        except BaseException as exc:  # noqa: BLE001 - mirror MCPClientManager; covers ExceptionGroup
            await self._fail_connect(entry, exc)
            return
        entry.client = client
        entry.state = McpConnectionState.READY
        entry.ready_event.set()

    async def _fail_connect(
        self, entry: _ConnectionEntry, exc: BaseException | None
    ) -> None:
        """Record a connect failure: safely close the (possibly half-entered)
        stack IN THIS TASK, surface the real root cause, mark FAILED.

        The stack may be half-entered when the transport fails mid-handshake.
        Closing it can itself raise the ``BaseExceptionGroup`` that carries the
        real transport error (e.g. an ``httpx`` 4xx) — anyio cancel scopes must
        be closed in the task that entered them, so this MUST happen here, not
        be deferred. The supervisor's ``finally`` later re-closes the (then
        empty) stack harmlessly. Mirrors ``MCPClientManager._connect_single``'s
        ``BaseException`` handling + ``_safe_aclose``.
        """
        close_root: BaseException | None = None
        try:
            await entry.stack.aclose()
        except BaseException as close_exc:  # noqa: BLE001 - never let close crash connect
            close_root = _root_cause(close_exc)

        # Prefer the original exception's root cause; fall back to whatever the
        # stack-close surfaced (the cancel-scope CancelledError case carries no
        # useful info itself — its cause is in the close-time ExceptionGroup).
        root = _root_cause(exc) if exc is not None else close_root
        if root is not None:
            _logger.warning(
                "[MCP Registry] connect failed for %s: %s: %s",
                entry.name,
                type(root).__name__,
                root,
            )
        else:
            _logger.warning(
                "[MCP Registry] connect failed for %s: cancelled", entry.name
            )
        entry.state = McpConnectionState.FAILED
        entry.ready_event.set()

    async def _connect_with_backoff(
        self,
        entry: _ConnectionEntry,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> bool:
        """Retry ``connect_fn`` with exponential backoff; in-place stack swap.

        Mirrors ``MCPClientManager.reconnect_with_retry`` params. A FAILED
        connect partially enters its stack, so each attempt manages a FRESH
        ``AsyncExitStack`` (created and closed in THIS task). On success the
        live stack is assigned to ``entry.stack`` and the client/state/ready
        are set. On exhaustion a fresh EMPTY stack is assigned to
        ``entry.stack`` (so the supervisor's finally always closes something
        valid), state FAILED/``ready_event`` set, returns False. Each failed
        attempt's stack is closed IN-TASK. ``CancelledError`` closes the
        attempt's stack and re-raises.

        Delays go ONLY between retries (not after the last). When
        ``self._reconnect_backoff`` is set (test seam), those delays are used
        verbatim; otherwise ``min(base_delay * 2**attempt, max_delay)``.
        """
        for attempt in range(max_retries):
            # Shutdown may have begun mid-reconnect. ``_handle_reconnect`` sets
            # ``state = CONNECTING`` only AFTER its ``stack.aclose()``; if
            # ``shutdown()`` snapshotted the entry during that window it saw
            # READY and so did NOT cancel this task. A plain ``asyncio.sleep``
            # would then ignore the non-cancelling shutdown and delay registry
            # teardown by up to the full backoff. So bail before each attempt.
            if self._shutdown_event.is_set():
                return self._abort_reconnect(entry)
            attempt_stack: AsyncExitStack = AsyncExitStack()
            try:
                client = await self._connect_fn(
                    entry.name,
                    entry.config,
                    injector=self._injector,
                    stack=attempt_stack,
                )
            except asyncio.CancelledError:
                # Shutdown cancelled the reconnect's connect_fn. Close this
                # attempt's partial stack in-task, then unwind to the
                # supervisor finally (which closes entry.stack — the old
                # dropped stack already closed by _handle_reconnect).
                with suppress(Exception):
                    await attempt_stack.aclose()
                raise
            except Exception:  # noqa: BLE001 - any failure → retry/backoff
                # Failed attempt: close this attempt's partial stack in-task.
                with suppress(Exception):
                    await attempt_stack.aclose()
            else:
                # Success: hand the live stack to the entry, swap client.
                entry.stack = attempt_stack
                entry.client = client
                entry.state = McpConnectionState.READY
                entry.ready_event.set()
                return True
            # Backoff before the next retry — but NOT after the last attempt.
            if attempt < max_retries - 1:
                if self._reconnect_backoff is not None:
                    delay = (
                        self._reconnect_backoff[attempt]
                        if attempt < len(self._reconnect_backoff)
                        else self._reconnect_backoff[-1]
                    )
                else:
                    delay = min(base_delay * (2**attempt), max_delay)
                # Shutdown-responsive backoff: wake immediately if shutdown
                # begins, otherwise sleep the full delay. (Plain sleep would
                # outlast a non-cancelling shutdown — see the loop-top check.)
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=delay
                    )
                except TimeoutError:
                    pass  # delay elapsed normally; proceed to the next attempt
                else:
                    return self._abort_reconnect(entry)
        # Exhausted. Fresh EMPTY stack so the supervisor finally closes valid.
        return self._abort_reconnect(entry)

    def _abort_reconnect(self, entry: _ConnectionEntry) -> bool:
        """Leave a FAILED entry with an empty stack and return False.

        Shared by the exhausted-retries and shutdown-abort exit paths of
        :meth:`_connect_with_backoff`. A fresh empty ``AsyncExitStack`` ensures
        the supervisor's ``finally`` always closes something valid.
        """
        entry.stack = AsyncExitStack()
        entry.client = None
        entry.state = McpConnectionState.FAILED
        entry.ready_event.set()
        return False

    async def _handle_reconnect(self, entry: _ConnectionEntry) -> None:
        """Service one reconnect request, in THIS supervisor task.

        Closes the current (dropped) stack in-task, transitions to CONNECTING,
        retries connect with backoff, then resolves the requester's future
        (whoever asked for this reconnect). The future is guarded ``not
        done()`` so a concurrent shutdown that already resolved it to False
        cannot be double-set.
        """
        # Close the current (dropped) stack in-task before swapping.
        try:
            await entry.stack.aclose()
        except Exception as exc:  # noqa: BLE001 - never let close crash reconnect
            _logger.warning(
                "[MCP Registry] error closing dropped stack for %s: %s: %s",
                entry.name,
                type(exc).__name__,
                exc,
            )
        entry.client = None
        entry.state = McpConnectionState.CONNECTING
        success = await self._connect_with_backoff(entry)
        # Resolve the requester's future (whoever asked for this reconnect).
        fut = entry.reconnect_future
        entry.reconnect_in_progress = False
        entry.reconnect_future = None
        if fut is not None and not fut.done():
            fut.set_result(success)

    # -- public API ------------------------------------------------------------

    def start_connecting(self, names: Iterable[str]) -> None:
        """Fire-and-forget pre-warm. Idempotent.

        Ensures a supervisor exists for each named server's config (dedup by
        canonical hash). Unknown names are logged and skipped. Does not block.
        """
        for name in names:
            self._ensure(name)

    async def acquire(
        self,
        selection: list[str],
        *,
        timeout: float = 8.0,
    ) -> SharedMcpBackend:
        """Ensure supervisors for ``selection``, wait for terminal, return facade.

        For each name: ensure a supervisor exists (same as
        :meth:`start_connecting`). Then wait for every entry to reach a terminal
        state (READY/FAILED) within ``timeout``. If the deadline fires first,
        proceed with whatever is READY (still-CONNECTING names are absent —
        gating by absence). Unknown names are logged and skipped (absent).
        """
        entries: dict[str, _ConnectionEntry] = {}
        for name in selection:
            entry = self._ensure(name)
            if entry is not None:
                entries[name] = entry

        if entries:
            # One deadline over all awaits. gather returns when each entry's
            # ready_event is set (READY or FAILED); a still-CONNECTING entry
            # simply is not ready when the deadline fires. A timeout is not an
            # error: we proceed with whatever is READY (slow servers are absent).
            awaitable = asyncio.gather(
                *(e.terminal_event() for e in entries.values())
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(awaitable, timeout=timeout)

        subset = {
            name: entry
            for name, entry in entries.items()
            if entry.state == McpConnectionState.READY
        }
        return SharedMcpBackend(self, subset)

    async def request_reconnect(self, entry: _ConnectionEntry) -> bool:
        """Ask the entry's supervisor to re-establish its connection.

        Coalesces concurrent requests onto one reconnect attempt. Returns True
        if the connection is READY afterward, False if it failed or the
        registry is shutting down. Never raises (failures → False).

        There is NO ``state == READY`` short-circuit: passive detection means
        the facade knows the connection dropped (a call failed) while
        ``entry.state`` still reads READY (the registry has no health check),
        so the request is always honored. The claim prefix is synchronous (no
        ``await``) so two concurrent callers cannot both claim — exactly one
        reconnect fires.
        """
        if self._closed:
            return False
        # Coalesce: a reconnect is already in flight → await its result.
        if entry.reconnect_in_progress and entry.reconnect_future is not None:
            try:
                return await asyncio.shield(entry.reconnect_future)
            except asyncio.CancelledError:
                return False
        # Claim the slot. The prefix below is synchronous (no await) so two
        # concurrent callers cannot both claim — exactly one reconnect fires.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        entry.reconnect_in_progress = True
        entry.reconnect_future = fut
        entry.wake_event.set()
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            return False

    async def shutdown(self) -> None:
        """Signal every supervisor to close and await them. Idempotent.

        Each supervisor closes its own stack in its own task (anyio-safe). Safe
        to call even if some supervisors already FAILED (they are now idle on
        ``wake_event``, not exited). A single supervisor's close failure is
        logged and does not abort closing the rest. After shutdown the registry
        is closed: :meth:`acquire` raises.

        Idle READY/FAILED supervisors are WOKEN via ``wake_event`` (they return
        normally to their finally); only CONNECTING supervisors (stuck inside
        ``connect_fn`` — either the initial connect or a reconnect's
        ``_connect_with_backoff``) are cancelled, their ``CancelledError``
        unwinding through their finally (same task). Any pending reconnect
        future is resolved to False so a facade awaiting reconnect during
        shutdown unblocks (the cancelled ``_handle_reconnect`` does NOT resolve
        it; shutdown does, and ``_handle_reconnect`` guards ``not fut.done()``).
        """
        if self._closed:
            return
        self._closed = True
        self._shutdown_event.set()
        connecting_tasks: list[asyncio.Task[None]] = []
        live_tasks: list[asyncio.Task[None]] = []
        for entry in self._entries.values():
            entry.wake_event.set()  # wake any idle supervisor
            fut = entry.reconnect_future
            if fut is not None and not fut.done():
                fut.set_result(False)  # unblock a facade awaiting reconnect
            task = entry.supervisor_task
            if task is None or task.done():
                continue
            live_tasks.append(task)
            if entry.state == McpConnectionState.CONNECTING:
                connecting_tasks.append(task)
        for task in connecting_tasks:
            task.cancel()
        if live_tasks:
            # return_exceptions: cancelled/failed supervisors surface as
            # results, never raising into shutdown itself.
            await asyncio.gather(*live_tasks, return_exceptions=True)


class SharedMcpBackend(McpBackend):
    """Facade over a READY subset of a :class:`McpConnectionRegistry`.

    Implements the :class:`McpBackend` consumed surface (architecture rule 6:
    the second concrete backend that justifies the ABC seam). ``connected_servers``
    and ``_client_for`` gate by READY state (absence, not a flag); the query /
    invocation members are inherited unchanged from :class:`McpBackend`.

    ``release()`` only detaches this facade — it does NOT close the underlying
    connections (see the module docstring's no-refcount choice). Idempotent.
    """

    def __init__(
        self,
        registry: McpConnectionRegistry,
        subset: dict[str, _ConnectionEntry],
    ) -> None:
        self._registry: McpConnectionRegistry = registry
        self._subset: dict[str, _ConnectionEntry] = dict(subset)
        self._released: bool = False

    @property
    def connected_servers(self) -> list[str]:
        if self._released:
            return []
        return [
            name
            for name, entry in self._subset.items()
            if entry.state == McpConnectionState.READY
        ]

    def _client_for(self, name: str) -> BaseMCPClient | None:
        if self._released:
            return None
        entry = self._subset.get(name)
        if entry is None or entry.state != McpConnectionState.READY:
            return None
        return entry.client

    async def release(self) -> None:
        """Detach this facade from the registry. Idempotent, does not close.

        The underlying connections are shared and service-scoped; closing them
        on release would kill connections other workspaces still use. They close
        only at registry shutdown.
        """
        self._released = True
        self._subset = {}

    # -- invocation with reconnect-on-disconnect --------------------------------
    #
    # Mirror MCPClientManager's per-call reconnect pattern, broadened for the
    # shared connection's long-lived nature. MCPClientManager reconnects when a
    # call reports "not connected" — which for it means the client was never
    # connected or was explicitly disconnected (``_client_for`` → None). The
    # shared registry additionally suffers the *dropped-but-still-READY*
    # session: ``state`` stays READY, the client is still present, but the
    # transport is dead — and a dead session returns an EMPTY error, not "not
    # connected" (confirmed against the real transport). So the reconnect
    # trigger matches BOTH "not connected" and an empty error string.
    #
    # Detection stays passive — no health polling. On a trigger the facade asks
    # the registry to reconnect (coalesced, in-supervisor, MCPClientManager-
    # style backoff via ``_connect_with_backoff``) and retries once. A released
    # facade must NOT trigger reconnect (guard is explicit).

    @staticmethod
    def _looks_dropped(result: dict[str, Any]) -> bool:
        """True if a non-success call result signals a dropped connection.

        Covers both the explicit "not connected" gating error (the entry was
        never READY / reconnecting) and the silent empty-error signature of a
        dead-but-READY session. A genuine tool error (``McpError``, ``isError``)
        carries a non-empty message and is NOT treated as a drop.
        """
        if result.get("success"):
            return False
        err = str(result.get("error", "")).lower()
        return "not connected" in err or err.strip() == ""

    async def _request_reconnect(self, server_name: str) -> bool:
        """Ask the registry to reconnect one server's shared connection.

        Returns True if the connection is READY afterward. No-op (False) for a
        released facade, an unknown server, or a failed/aborted reconnect.
        """
        if self._released or self._registry is None:
            return False
        entry = self._subset.get(server_name)
        if entry is None:
            return False
        _logger.warning(
            "[MCP:%s] shared session appears dropped, reconnecting...", server_name
        )
        return await self._registry.request_reconnect(entry)

    async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
        """List tools, reconnecting once if a previously-populated session dropped.

        Registration path for ``MCPToolAdapter.register_tools``: called once per
        workspace materialization against the long-lived shared connection. A
        dead session returns ``[]`` without raising, so without this a workspace
        materializing after a drop would cache an empty tool list forever — the
        "MCP missing after workspace switch" symptom. We reconnect only when the
        entry has previously served tools (``had_tools``); a genuinely-empty
        server never sets that flag, so it is not hammered with reconnects.
        """
        tools = await McpBackend.list_tools(self, server_name)
        entry = self._subset.get(server_name)
        if tools:
            if entry is not None:
                entry.had_tools = True
            return tools
        # Empty result: a dropped-but-READY session yields []. Reconnect once
        # only if this entry has previously served tools.
        if entry is not None and entry.had_tools and await self._request_reconnect(server_name):
            tools = await McpBackend.list_tools(self, server_name)
            if tools:
                entry.had_tools = True
        return tools

    async def execute_tool(
        self,
        server_name: str,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Execute a tool, reconnecting once on a dropped shared connection."""
        result = await super().execute_tool(server_name, tool_name, params, timeout=timeout)
        if not self._released and self._looks_dropped(result):
            if await self._request_reconnect(server_name):
                result = await super().execute_tool(
                    server_name, tool_name, params, timeout=timeout
                )
        return result

    async def read_resource(
        self,
        server_name: str,
        uri: str,
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Read a resource, reconnecting once on a dropped shared connection."""
        result = await super().read_resource(server_name, uri, timeout=timeout)
        if not self._released and self._looks_dropped(result):
            if await self._request_reconnect(server_name):
                result = await super().read_resource(server_name, uri, timeout=timeout)
        return result

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Get a prompt, reconnecting once on a dropped shared connection."""
        result = await super().get_prompt(
            server_name, prompt_name, arguments=arguments, timeout=timeout
        )
        if not self._released and self._looks_dropped(result):
            if await self._request_reconnect(server_name):
                result = await super().get_prompt(
                    server_name, prompt_name, arguments=arguments, timeout=timeout
                )
        return result
