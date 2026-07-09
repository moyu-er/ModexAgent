"""Tests for :mod:`modex_agent.tools.mcp.registry` — the shared-connection overlay.

ADR-0017 Task 3. The registry deduplicates MCP connections by a canonical
config-hash and connects all servers concurrently via per-server supervisor
tasks (anyio-safe same-task stack lifecycle). These tests drive the registry
with a fake ``connect_fn`` so no subprocess is spawned; they pin the parallel
connect, dedup-by-hash, gating-by-absence, and graceful-shutdown contracts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import pytest

from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import BaseMCPClient
from modex_agent.tools.mcp.connection import connect_single_server
from modex_agent.tools.mcp.injector import MCPTransportInjector
from modex_agent.tools.mcp.registry import (
    McpConnectionRegistry,
    McpConnectionState,
    SharedMcpBackend,
    _ConnectionEntry,
)


class _StubClient(BaseMCPClient):
    """Minimal ``BaseMCPClient`` stand-in for registry tests.

    Mirrors the stub shape in ``test_mcp_backend.py``: instantiable without a
    real MCP session, returns canned tool lists. Identity is used to assert
    dedup (``is``) so each instance is a distinct object.
    """

    def __init__(self, name: str = "stub") -> None:  # noqa: D401 - test stub
        super().__init__(name=name)

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def list_tools(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return [{"name": f"{self.name}_tool"}]


def _make_connect_fn(
    *,
    delay: float = 0.0,
    fail_names: set[str] | None = None,
    slow_names: set[str] | None = None,
    slow_event: asyncio.Event | None = None,
) -> Callable[..., Awaitable[BaseMCPClient]]:
    """Build a fake ``connect_fn`` for tests.

    - ``delay``: seconds to sleep before returning (simulates connect latency).
    - ``fail_names``: names whose connect raises (→ FAILED supervisor).
    - ``slow_names``: names whose connect awaits ``slow_event`` and never
      resolves (→ stays CONNECTING past the acquire deadline).

    Records ``(name, config)`` calls so dedup / count assertions can inspect it.
    """
    fail_names = fail_names or set()
    slow_names = slow_names or set()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_connect(
        name: str,
        server_config: dict[str, Any],
        *,
        injector: MCPTransportInjector,
        stack: AsyncExitStack,
    ) -> BaseMCPClient:
        calls.append((name, dict(server_config)))
        if name in slow_names:
            # Never resolves — supervisor idles in CONNECTING.
            if slow_event is not None:
                await slow_event.wait()
            else:
                await asyncio.Event().wait()  # waits forever
            return _StubClient(name)  # unreachable; keeps the type checker calm
        if delay:
            await asyncio.sleep(delay)
        if name in fail_names:
            raise RuntimeError(f"connect failed: {name}")
        return _StubClient(name)

    fake_connect.calls = calls  # type: ignore[attr-defined]
    return fake_connect


# ---------------------------------------------------------------------------
# 1. Parallel connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_connect_is_parallel() -> None:
    """Three servers connect concurrently: total wall time ≈ one delay, not 3×."""
    delay = 0.05
    connect_fn = _make_connect_fn(delay=delay)
    servers = {
        "a": {"command": "x"},
        "b": {"command": "y"},
        "c": {"command": "z"},
    }
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    registry.start_connecting(["a", "b", "c"])
    start = time.monotonic()
    backend = await registry.acquire(["a", "b", "c"], timeout=5.0)
    elapsed = time.monotonic() - start

    assert sorted(backend.connected_servers) == ["a", "b", "c"]
    # Parallel: each task sleeps ``delay``; the serial sum is 3*delay. A truly
    # concurrent run completes in well under that — assert <90% of the serial
    # sum, a self-scaling bound that tracks the delay rather than hardcoding.
    assert elapsed < 3 * delay * 0.9, f"connect was not parallel: {elapsed:.3f}s"
    assert len(connect_fn.calls) == 3  # type: ignore[attr-defined]
    await registry.shutdown()


# ---------------------------------------------------------------------------
# 2. Dedup by canonical config-hash
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_by_config_hash() -> None:
    """Two names with byte-identical config share ONE underlying connection."""
    connect_fn = _make_connect_fn()
    same_cfg = {"command": "npx", "args": ["-y", "pkg"]}
    servers = {"a": dict(same_cfg), "b": dict(same_cfg)}
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    backend = await registry.acquire(["a", "b"], timeout=2.0)

    # Only one connect happened (one subprocess simulated).
    assert len(connect_fn.calls) == 1  # type: ignore[attr-defined]
    # Both names resolve to the SAME client object.
    ca = backend._client_for("a")
    cb = backend._client_for("b")
    assert ca is not None and cb is not None
    assert ca is cb
    # One entry keyed by config-hash.
    assert len(registry._entries) == 1
    assert sorted(backend.connected_servers) == ["a", "b"]
    await registry.shutdown()


# ---------------------------------------------------------------------------
# 3. Distinct configs → separate connections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_configs_separate_connections() -> None:
    """Two names with different configs → two entries, two clients."""
    connect_fn = _make_connect_fn()
    servers = {
        "a": {"command": "pkg-a"},
        "b": {"command": "pkg-b"},
    }
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    backend = await registry.acquire(["a", "b"], timeout=2.0)

    assert len(connect_fn.calls) == 2  # type: ignore[attr-defined]
    assert len(registry._entries) == 2
    ca = backend._client_for("a")
    cb = backend._client_for("b")
    assert ca is not None and cb is not None
    assert ca is not cb
    await registry.shutdown()


# ---------------------------------------------------------------------------
# 4. Failed server absent, others present; supervisor idles (recoverable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_server_absent_others_present() -> None:
    """A failing server is absent from the facade; its supervisor idles.

    Under the unified supervisor a FAILED entry does NOT exit — it stays idle
    on ``wake_event`` so a later reconnect could recover it. We assert the
    observable contract instead of task-exit: "bad" absent, "good" present,
    and shutdown completes cleanly (the FAILED supervisor is woken and closes
    its stack in-task — no hang, no RuntimeError).
    """
    connect_fn = _make_connect_fn(fail_names={"bad"})
    servers = {
        "bad": {"command": "x"},
        "good": {"command": "y"},
    }
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    backend = await registry.acquire(["bad", "good"], timeout=2.0)

    assert "good" in backend.connected_servers
    assert "bad" not in backend.connected_servers
    assert backend._client_for("bad") is None
    assert backend._client_for("good") is not None

    # The FAILED entry exists; its supervisor is still alive (idle, not exited).
    bad_entry = next(
        e for e in registry._entries.values() if e.state == McpConnectionState.FAILED
    )
    assert bad_entry.supervisor_task is not None
    assert not bad_entry.supervisor_task.done()

    # Shutdown wakes the idle FAILED supervisor (via wake_event) and it closes
    # its stack in-task — completes cleanly with no hang and no RuntimeError.
    await registry.shutdown()
    assert bad_entry.supervisor_task.done()


# ---------------------------------------------------------------------------
# 4b. Connect failure via anyio cancel-scope / ExceptionGroup (parity with
#     MCPClientManager). A streamable_http/sse transport failure originates in
#     the mcp SDK's *background* task: anyio cancels the host cancel-scope so
#     ``connect_single_server`` raises a CancelledError, and the real error is
#     trapped in the TaskGroup (surfaces on stack close as a BaseExceptionGroup).
#     The supervisor must record FAILED + stay alive — NOT re-raise and crash.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_scope_connect_failure_marks_failed_not_crash() -> None:
    """An anyio cancel-scope ``CancelledError`` during a NON-shutdown connect is
    a transport failure, not a real cancellation. The supervisor must treat it
    as a connect failure (FAILED, stays idle) — re-raising would crash the
    supervisor and orphan the entry. Regression for the 12306-mcp HTTP 412 case
    where the supervisor died and the server was silently absent forever.
    """

    async def cancel_connect(
        name: str, server_config: dict[str, Any], *, injector, stack  # type: ignore[no-untyped-def]
    ) -> BaseMCPClient:
        raise asyncio.CancelledError("Cancelled by cancel scope 2932021d650")

    registry = McpConnectionRegistry({"s": {"command": "x"}}, connect_fn=cancel_connect)
    backend = await registry.acquire(["s"], timeout=2.0)

    assert "s" not in backend.connected_servers
    entry = next(iter(registry._entries.values()))
    assert entry.state == McpConnectionState.FAILED
    # Supervisor survived — still alive (idle), not crashed/orphaned.
    assert entry.supervisor_task is not None
    assert not entry.supervisor_task.done()
    await registry.shutdown()
    assert entry.supervisor_task.done()


@pytest.mark.asyncio
async def test_exception_group_connect_failure_logs_root_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TaskGroup ``ExceptionGroup`` carrying the real transport error is
    unwrapped so the root cause is logged — not hidden as 'ExceptionGroup (1
    sub-exception)'. Parity with ``MCPClientManager._connect_single``.
    """
    real = RuntimeError("Client error '412 Precondition Failed'")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [real])

    async def group_connect(
        name: str, server_config: dict[str, Any], *, injector, stack  # type: ignore[no-untyped-def]
    ) -> BaseMCPClient:
        raise group

    registry = McpConnectionRegistry({"s": {"command": "x"}}, connect_fn=group_connect)
    with caplog.at_level(logging.WARNING, logger="modex_agent.tools.mcp.registry"):
        backend = await registry.acquire(["s"], timeout=2.0)

    assert "s" not in backend.connected_servers
    entry = next(iter(registry._entries.values()))
    assert entry.state == McpConnectionState.FAILED
    # Root cause surfaced in the log, not the opaque group wrapper.
    assert any("412 Precondition Failed" in r.message for r in caplog.records), (
        f"root cause not logged; got {[r.message for r in caplog.records]}"
    )
    await registry.shutdown()


@pytest.mark.asyncio
async def test_cancel_failed_entry_recoverable_via_reconnect() -> None:
    """A FAILED entry (failed via cancel-scope) keeps a live supervisor that a
    reconnect request can wake and recover — the pre-fix bug orphaned it (dead
    supervisor, no recovery, server absent forever).
    """
    fail = {"yes": True}

    async def connect(
        name: str, server_config: dict[str, Any], *, injector, stack  # type: ignore[no-untyped-def]
    ) -> BaseMCPClient:
        if fail["yes"]:
            raise asyncio.CancelledError("Cancelled by cancel scope")
        return _StubClient(name)

    registry = McpConnectionRegistry({"s": {"command": "x"}}, connect_fn=connect)
    backend = await registry.acquire(["s"], timeout=2.0)
    assert "s" not in backend.connected_servers
    entry = next(iter(registry._entries.values()))
    assert entry.state == McpConnectionState.FAILED

    # The endpoint recovers; ask the (still-alive) supervisor to reconnect.
    fail["yes"] = False
    ok = await registry.request_reconnect(entry)
    assert ok is True
    assert entry.state == McpConnectionState.READY

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 5. Acquire timeout excludes a slow server, keeps the fast one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_timeout_excludes_slow_server() -> None:
    """A server still CONNECTING at the deadline is simply absent."""
    connect_fn = _make_connect_fn(slow_names={"slow"})
    servers = {
        "slow": {"command": "x"},
        "fast": {"command": "y"},
    }
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    backend = await registry.acquire(["slow", "fast"], timeout=0.1)

    assert "fast" in backend.connected_servers
    assert "slow" not in backend.connected_servers
    assert backend._client_for("slow") is None

    await registry.shutdown()

    # Pin the anyio-safe cancellation path: shutdown cancelled the still-
    # CONNECTING supervisor and closed its stack in-task (no cross-task
    # RuntimeError). The slow entry exists, its task is done, and the only
    # acceptable terminal exception is CancelledError (or none at all).
    slow_entry = next(
        e for e in registry._entries.values() if e.name == "slow"
    )
    assert slow_entry.supervisor_task is not None
    assert slow_entry.supervisor_task.done()
    try:
        exc = slow_entry.supervisor_task.exception()
    except asyncio.CancelledError:
        exc = asyncio.CancelledError()  # py<3.8: .exception() re-raises
    assert exc is None or isinstance(
        exc, asyncio.CancelledError
    ), f"unexpected: {exc!r}"


# ---------------------------------------------------------------------------
# 6. Unknown name skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_unknown_name_skipped() -> None:
    """An unknown name logs and is absent; no task spawned, no exception."""
    connect_fn = _make_connect_fn()
    registry = McpConnectionRegistry({}, connect_fn=connect_fn)

    backend = await registry.acquire(["unknown"], timeout=1.0)

    assert backend.connected_servers == []
    assert len(connect_fn.calls) == 0  # type: ignore[attr-defined]
    assert len(registry._entries) == 0
    await registry.shutdown()


# ---------------------------------------------------------------------------
# 7. Shutdown closes all supervisors safely; terminal afterward
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_closes_all_supervisors_safely() -> None:
    """Shutdown awaits every supervisor and closes each stack in-task."""
    connect_fn = _make_connect_fn()
    servers = {
        "a": {"command": "x"},
        "b": {"command": "y"},
        "c": {"command": "z"},
    }
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)
    backend = await registry.acquire(["a", "b", "c"], timeout=2.0)
    assert len(backend.connected_servers) == 3

    await registry.shutdown()

    # Every supervisor task is done (no dangling live task).
    for entry in registry._entries.values():
        assert entry.supervisor_task is not None
        assert entry.supervisor_task.done()

    # Shutdown is terminal: a subsequent acquire raises a clear error.
    with pytest.raises(RuntimeError, match="registry shut down"):
        await registry.acquire(["a"], timeout=0.1)

    # Shutdown is idempotent.
    await registry.shutdown()


# ---------------------------------------------------------------------------
# 8. release() is idempotent and detaches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_is_idempotent_and_detaches() -> None:
    connect_fn = _make_connect_fn()
    servers = {"a": {"command": "x"}}
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    backend = await registry.acquire(["a"], timeout=2.0)
    assert backend.connected_servers == ["a"]
    assert backend._client_for("a") is not None

    await backend.release()
    await backend.release()  # idempotent — no error

    assert backend.connected_servers == []
    assert backend._client_for("a") is None

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 9. SharedMcpBackend is a McpBackend; delegation works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_mcp_backend_is_a_mcp_backend() -> None:
    connect_fn = _make_connect_fn()
    servers = {"a": {"command": "x"}}
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    backend = await registry.acquire(["a"], timeout=2.0)

    assert isinstance(backend, McpBackend)
    assert isinstance(backend, SharedMcpBackend)
    # Delegation: list_tools routes through _client_for for a READY name.
    tools = await backend.list_tools("a")
    assert tools == [{"name": "a_tool"}]
    # Absent name → empty (gating by absence).
    assert await backend.list_tools("absent") == []

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 10. Multiple acquires share the same underlying connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_acquires_share_connection() -> None:
    """Two acquires of the same name hit one subprocess (one connect_fn call)."""
    connect_fn = _make_connect_fn()
    servers = {"a": {"command": "x"}}
    registry = McpConnectionRegistry(servers, connect_fn=connect_fn)

    b1 = await registry.acquire(["a"], timeout=2.0)
    b2 = await registry.acquire(["a"], timeout=2.0)

    assert len(connect_fn.calls) == 1  # type: ignore[attr-defined]
    c1 = b1._client_for("a")
    c2 = b2._client_for("a")
    assert c1 is not None and c2 is not None
    assert c1 is c2

    await b1.release()
    await b2.release()
    await registry.shutdown()


# ---------------------------------------------------------------------------
# Guard: connect_fn default is connect_single_server (test seam contract)
# ---------------------------------------------------------------------------


def test_default_connect_fn_is_connect_single_server() -> None:
    """When no connect_fn is passed, the registry uses the real primitive."""
    import inspect

    sig = inspect.signature(McpConnectionRegistry.__init__)
    # The default must resolve to connect_single_server at construction time.
    registry = McpConnectionRegistry({})
    assert registry._connect_fn is connect_single_server
    # Signature also documents it.
    assert sig.parameters["connect_fn"].default is None  # resolved in __init__


# ===========================================================================
# Drop recovery (reconnect) — ADR-0017 follow-up #3
# ===========================================================================


class _ReconnectStubClient(BaseMCPClient):
    """Stub client whose ``call_tool`` is scriptable per instance.

    ``call_result`` is what ``call_tool`` returns. Set it to
    ``{"success": False, "error": "Not connected"}`` to simulate a dropped
    connection, or a success dict for a working client. Identity (``is``) is
    used to assert that a reconnect swapped in a different client.
    """

    def __init__(self, name: str, call_result: dict[str, Any]) -> None:
        super().__init__(name=name)
        self._call_result = call_result

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def call_tool(  # type: ignore[override]
        self,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = 30,
    ) -> dict[str, Any]:
        return dict(self._call_result)

    async def read_resource(  # type: ignore[override]
        self, uri: str, timeout: int = 30
    ) -> dict[str, Any]:
        return dict(self._call_result)

    async def get_prompt(  # type: ignore[override]
        self,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        return dict(self._call_result)

    async def list_tools(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return [{"name": f"{self.name}_tool"}]


def _make_reconnect_connect_fn(
    clients: list[BaseMCPClient],
    *,
    fail_after_first: bool = False,
    block_event: asyncio.Event | None = None,
) -> Callable[..., Awaitable[BaseMCPClient]]:
    """Build a ``connect_fn`` that returns successive clients from ``clients``.

    The first call returns ``clients[0]``, the next ``clients[1]``, etc. — so a
    reconnect (2nd call) yields a DIFFERENT client. Records every call for
    count assertions.

    - ``fail_after_first``: after the first successful connect, every later
      call raises (simulates a server that stays down on reconnect).
    - ``block_event``: when set, the SECOND and later calls block on this event
      (never resolving) to simulate a reconnect that hangs past shutdown.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    idx = {"i": 0}

    async def fake_connect(
        name: str,
        server_config: dict[str, Any],
        *,
        injector: MCPTransportInjector,
        stack: AsyncExitStack,
    ) -> BaseMCPClient:
        calls.append((name, dict(server_config)))
        i = idx["i"]
        idx["i"] += 1
        if i > 0:
            if block_event is not None:
                await block_event.wait()
                # Unreachable in the block test path; keeps types calm.
            if fail_after_first:
                raise RuntimeError(f"reconnect failed: attempt {i}")
        return clients[i]

    fake_connect.calls = calls  # type: ignore[attr-defined]
    return fake_connect


# ---------------------------------------------------------------------------
# 11. Reconnect recovers a dropped connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_recovers_dropped_connection() -> None:
    """A dropped connection is detected and recovered via reconnect."""
    # client_A: dropped (call_tool → "Not connected"). client_B: working.
    client_a = _ReconnectStubClient(
        "s", {"success": False, "error": "Not connected"}
    )
    client_b = _ReconnectStubClient("s", {"success": True, "result": "ok-B"})
    connect_fn = _make_reconnect_connect_fn([client_a, client_b])
    servers = {"s": {"command": "x"}}
    registry = McpConnectionRegistry(
        servers, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )

    facade = await registry.acquire(["s"], timeout=2.0)
    assert facade.connected_servers == ["s"]

    # First execute_tool: client_A returns "Not connected" → detect → reconnect
    # → swap to client_B → retry → success from B.
    result = await facade.execute_tool("s", "t", {})

    assert result == {"success": True, "result": "ok-B"}
    # connect_fn called twice: initial connect + one reconnect.
    assert len(connect_fn.calls) == 2  # type: ignore[attr-defined]
    # The facade now serves client_B.
    assert facade._client_for("s") is client_b

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 12. Concurrent reconnect requests are coalesced onto one attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_coalesces_concurrent_requests() -> None:
    """Two concurrent calls hitting the drop trigger ONE reconnect."""
    client_a = _ReconnectStubClient(
        "s", {"success": False, "error": "Not connected"}
    )
    client_b = _ReconnectStubClient("s", {"success": True, "result": "ok-B"})
    connect_fn = _make_reconnect_connect_fn([client_a, client_b])
    servers = {"s": {"command": "x"}}
    registry = McpConnectionRegistry(
        servers, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )

    facade = await registry.acquire(["s"], timeout=2.0)

    # Two concurrent execute_tool calls both observe the drop. Exactly one
    # reconnect fires (coalesced); both return B's success.
    r1, r2 = await asyncio.gather(
        facade.execute_tool("s", "t", {}),
        facade.execute_tool("s", "t", {}),
    )

    assert r1 == {"success": True, "result": "ok-B"}
    assert r2 == {"success": True, "result": "ok-B"}
    # initial connect (1) + ONE reconnect (1) = 2 total. Not 3.
    assert len(connect_fn.calls) == 2  # type: ignore[attr-defined]

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 13. Reconnect failure returns the original error, no exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_failure_returns_original_error() -> None:
    """When reconnect exhausts retries, the facade returns the original error."""
    client_a = _ReconnectStubClient(
        "s", {"success": False, "error": "Not connected"}
    )
    connect_fn = _make_reconnect_connect_fn([client_a], fail_after_first=True)
    servers = {"s": {"command": "x"}}
    registry = McpConnectionRegistry(
        servers, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )

    facade = await registry.acquire(["s"], timeout=2.0)

    result = await facade.execute_tool("s", "t", {})

    # No exception; the original "not connected" error is returned.
    assert result["success"] is False
    assert "not connected" in str(result["error"]).lower()
    # connect_fn: initial connect (1) + max_retries reconnect attempts (3) = 4.
    assert len(connect_fn.calls) == 4  # type: ignore[attr-defined]
    # The entry ended FAILED.
    entry = next(iter(registry._entries.values()))
    assert entry.state == McpConnectionState.FAILED

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 14. A released facade does not trigger reconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_released_facade_does_not_reconnect() -> None:
    """A released facade returns the error without reconnecting."""
    client_a = _ReconnectStubClient(
        "s", {"success": False, "error": "Not connected"}
    )
    connect_fn = _make_reconnect_connect_fn([client_a])
    servers = {"s": {"command": "x"}}
    registry = McpConnectionRegistry(
        servers, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )

    facade = await registry.acquire(["s"], timeout=2.0)
    await facade.release()

    # Manually point the subset entry's client at a dropping client via the
    # registry entry (release empties the facade's subset, so execute_tool
    # returns the ABC's "not connected" gating error without reconnect).
    result = await facade.execute_tool("s", "t", {})

    assert result["success"] is False
    # No reconnect fired: connect_fn called only once (the initial connect).
    assert len(connect_fn.calls) == 1  # type: ignore[attr-defined]

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 15. Shutdown unblocks a pending reconnect (no deadlock)
# ---------------------------------------------------------------------------


# ===========================================================================
# 16. Drop recovery for a dropped-but-READY shared session
# ===========================================================================
#
# The shared connection is long-lived across workspace materializations and can
# DROP between two of them. A dropped session is SILENT (confirmed against the
# real stdio transport: subprocess killed → the mcp SDK never notifies the idle
# supervisor, ``entry.state`` stays READY, ``list_tools`` returns ``[]`` without
# raising, ``execute_tool`` returns ``{"success": False, "error": ""}``). So the
# facade must detect the drop itself: empty ``list_tools`` from a previously-
# populated entry (registration path), or an empty/''not connected'' error from
# a call (call path). These tests use realistic dead clients (return the silent
# signatures), NOT clients that raise.


class _DropRecoveryClient(BaseMCPClient):
    """Scriptable client mirroring a real ``BaseMCPClient``'s failure shapes.

    ``list_tools`` returns ``tools``; the call members return ``call_result``.
    A dead session is ``tools=[]`` + ``call_result={"success": False, "error": ""}``
    — exactly what the real client yields on a dead transport (it swallows the
    exception and returns an empty list / empty-error dict, never raising).
    """

    def __init__(self, name: str, *, tools: list[dict[str, Any]], call_result: dict[str, Any]) -> None:
        super().__init__(name=name)
        self._tools = tools
        self._call_result = call_result

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def list_tools(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return list(self._tools)

    async def call_tool(  # type: ignore[override]
        self, tool_name: str, params: dict[str, Any], timeout: int = 30
    ) -> dict[str, Any]:
        return dict(self._call_result)

    async def read_resource(  # type: ignore[override]
        self, uri: str, timeout: int = 30
    ) -> dict[str, Any]:
        return dict(self._call_result)

    async def get_prompt(  # type: ignore[override]
        self, prompt_name: str, arguments: dict[str, Any] | None = None, timeout: int = 30
    ) -> dict[str, Any]:
        return dict(self._call_result)


def _make_drop_connect_fn(
    clients: list[BaseMCPClient],
) -> Callable[..., Awaitable[BaseMCPClient]]:
    """Return successive ``clients`` per connect call (reuse last if exhausted).

    First call (initial connect) → ``clients[0]``; a reconnect (2nd call) →
    ``clients[1]``; etc. Reuses the last client past the end so an unexpected
    reconnect never IndexErrors the test. Records calls for count assertions.
    """
    calls: list[str] = []
    idx = {"i": 0}

    async def fake_connect(
        name: str,
        server_config: dict[str, Any],
        *,
        injector: MCPTransportInjector,
        stack: AsyncExitStack,
    ) -> BaseMCPClient:
        calls.append(name)
        c = clients[min(idx["i"], len(clients) - 1)]
        idx["i"] += 1
        return c

    fake_connect.calls = calls  # type: ignore[attr-defined]
    return fake_connect


def _healthy(name: str, result: str = "ok") -> _DropRecoveryClient:
    return _DropRecoveryClient(
        name, tools=[{"name": f"{name}_tool"}], call_result={"success": True, "result": result}
    )


def _dead(name: str) -> _DropRecoveryClient:
    # The real dead-session signature: empty tool list, empty-error call result.
    return _DropRecoveryClient(name, tools=[], call_result={"success": False, "error": ""})


@pytest.mark.asyncio
async def test_list_tools_reconnects_when_previously_populated_then_dropped() -> None:
    """A workspace materializing AFTER a drop still gets the server's tools.

    Home materializes (list_tools → tools, marks ``had_tools``), the shared
    session drops, then workspace B materializes: list_tools returns ``[]`` from
    the dead session → because the entry previously served tools, the facade
    reconnects once and retries → tools recovered. Without this, B would cache
    an empty tool list forever ('MCP missing after switch').
    """
    connect_fn = _make_drop_connect_fn([_healthy("s", "ok-home"), _healthy("s", "ok-reconnect")])
    registry = McpConnectionRegistry(
        {"s": {"command": "x"}}, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )
    backend = await registry.acquire(["s"], timeout=2.0)
    entry = next(iter(registry._entries.values()))

    # Home materialization: tools served, entry marked previously-populated.
    assert await backend.list_tools("s") == [{"name": "s_tool"}]
    assert entry.had_tools is True

    # The shared session drops (subprocess killed): the live client now answers
    # with the dead-session signatures.
    entry.client = _dead("s")

    # Workspace B materialization: [] → had_tools → reconnect → retry → tools.
    assert await backend.list_tools("s") == [{"name": "s_tool"}]
    assert len(connect_fn.calls) == 2  # type: ignore[attr-defined]  # initial + one reconnect

    await registry.shutdown()


@pytest.mark.asyncio
async def test_list_tools_empty_on_never_populated_does_not_reconnect() -> None:
    """A genuinely-empty server (never served tools) does NOT trigger reconnect.

    ``[]`` is ambiguous (dead session vs. server with no tools). The facade
    reconnects only when there is prior evidence the server had tools; a fresh
    entry returning ``[]`` is left alone.
    """
    connect_fn = _make_drop_connect_fn([_dead("s")])
    registry = McpConnectionRegistry(
        {"s": {"command": "x"}}, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )
    backend = await registry.acquire(["s"], timeout=2.0)
    entry = next(iter(registry._entries.values()))

    assert await backend.list_tools("s") == []
    assert entry.had_tools is False
    # No reconnect: only the initial connect.
    assert len(connect_fn.calls) == 1  # type: ignore[attr-defined]

    await registry.shutdown()


@pytest.mark.asyncio
async def test_execute_tool_reconnects_on_empty_error_drop() -> None:
    """A call returning an empty-error result (dead session) reconnects once."""
    connect_fn = _make_drop_connect_fn([_healthy("s", "ok-a"), _healthy("s", "ok-b")])
    registry = McpConnectionRegistry(
        {"s": {"command": "x"}}, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )
    backend = await registry.acquire(["s"], timeout=2.0)
    entry = next(iter(registry._entries.values()))

    # Session drops mid-life: the live client now returns the empty-error call
    # signature of a dead transport.
    entry.client = _dead("s")

    result = await backend.execute_tool("s", "t", {})
    assert result == {"success": True, "result": "ok-b"}
    assert len(connect_fn.calls) == 2  # type: ignore[attr-defined]  # initial + one reconnect

    await registry.shutdown()


@pytest.mark.asyncio
async def test_execute_tool_tool_error_does_not_reconnect() -> None:
    """A genuine tool error (non-empty message) is NOT treated as a drop."""
    error_client = _DropRecoveryClient(
        "s",
        tools=[{"name": "s_tool"}],
        call_result={"success": False, "error": "MCP error [-32603]: boom", "isError": True},
    )
    connect_fn = _make_drop_connect_fn([error_client])
    registry = McpConnectionRegistry(
        {"s": {"command": "x"}}, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )
    backend = await registry.acquire(["s"], timeout=2.0)

    result = await backend.execute_tool("s", "t", {})

    assert result["success"] is False
    assert "boom" in str(result["error"])
    # Tool error, not a drop → no reconnect.
    assert len(connect_fn.calls) == 1  # type: ignore[attr-defined]

    await registry.shutdown()


# ---------------------------------------------------------------------------
# 17. Reconnect backoff is shutdown-responsive (production, non-zero backoff)
# ---------------------------------------------------------------------------
#
# ``test_shutdown_unblocks_pending_reconnect`` exercises the CANCELLED path with
# zero backoff. These cover the path the existing test skips: real (non-zero)
# backoff where shutdown did NOT cancel the supervisor (it snapshotted the entry
# as READY during ``_handle_reconnect``'s stack-aclose window). The backoff must
# bail promptly instead of sleeping through full production delays.


@pytest.mark.asyncio
async def test_connect_with_backoff_bails_immediately_when_shutdown_set() -> None:
    """Shutdown set before the backoff runs → bail at the loop-top check, no sleep."""
    async def fail_connect(
        name: str, server_config: dict[str, Any], *, injector, stack  # type: ignore[no-untyped-def]
    ) -> BaseMCPClient:
        raise RuntimeError("server down")

    registry = McpConnectionRegistry(
        {"s": {"command": "x"}},
        connect_fn=fail_connect,
        reconnect_backoff=(5.0, 5.0, 5.0),  # 10s total if it slept
    )
    entry = _ConnectionEntry("s", "h", {"command": "x"})
    registry._shutdown_event.set()

    start = time.monotonic()
    ok = await registry._connect_with_backoff(entry)
    elapsed = time.monotonic() - start

    assert ok is False
    assert entry.state == McpConnectionState.FAILED
    assert elapsed < 1.0  # bailed immediately, did not sleep


@pytest.mark.asyncio
async def test_connect_with_backoff_sleep_wakes_on_shutdown() -> None:
    """A backoff sleep in progress wakes immediately when shutdown begins."""
    async def fail_connect(
        name: str, server_config: dict[str, Any], *, injector, stack  # type: ignore[no-untyped-def]
    ) -> BaseMCPClient:
        raise RuntimeError("server down")

    registry = McpConnectionRegistry(
        {"s": {"command": "x"}},
        connect_fn=fail_connect,
        reconnect_backoff=(5.0, 5.0, 5.0),
    )
    entry = _ConnectionEntry("s", "h", {"command": "x"})

    task = asyncio.create_task(registry._connect_with_backoff(entry))
    # Let attempt 0 fail and the backoff enter its (shutdown-responsive) sleep.
    await asyncio.sleep(0.1)
    registry._shutdown_event.set()

    ok = await asyncio.wait_for(task, timeout=2.0)

    assert ok is False
    assert entry.state == McpConnectionState.FAILED


@pytest.mark.asyncio
async def test_shutdown_unblocks_pending_reconnect() -> None:
    """A reconnect in flight during shutdown unblocks (future → False)."""
    client_a = _ReconnectStubClient(
        "s", {"success": False, "error": "Not connected"}
    )
    # The reconnect path (2nd connect_fn call) blocks on a never-set event.
    block = asyncio.Event()
    connect_fn = _make_reconnect_connect_fn([client_a], block_event=block)
    servers = {"s": {"command": "x"}}
    registry = McpConnectionRegistry(
        servers, connect_fn=connect_fn, reconnect_backoff=(0.0, 0.0)
    )

    facade = await registry.acquire(["s"], timeout=2.0)

    # Run the dropped-call (which triggers a reconnect that hangs) concurrently
    # with shutdown. Both must complete; no deadlock.
    async def _call() -> dict[str, Any]:
        return await facade.execute_tool("s", "t", {})

    call_task = asyncio.create_task(_call())
    # Let the call observe the drop and enter the blocked reconnect.
    await asyncio.sleep(0.05)
    await registry.shutdown()
    result = await call_task

    # Reconnect was resolved to False by shutdown → no retry → original error.
    assert result["success"] is False
    assert "not connected" in str(result["error"]).lower()

    # Path B regression guard: shutdown cancelled the in-flight reconnect's
    # supervisor (state was CONNECTING inside _connect_with_backoff). The
    # finally re-closes entry.stack — which _handle_reconnect already closed
    # at its start. AsyncExitStack.aclose() is idempotent, so the supervisor
    # must end with at most a CancelledError — NEVER a double-close RuntimeError
    # (which gather(return_exceptions=True) in shutdown would otherwise swallow).
    entry = next(iter(registry._entries.values()))
    sup_task = entry.supervisor_task
    assert sup_task is not None and sup_task.done()
    try:
        exc = sup_task.exception()
    except asyncio.CancelledError:
        exc = asyncio.CancelledError()  # py<3.8: .exception() re-raises
    assert exc is None or isinstance(
        exc, asyncio.CancelledError
    ), f"unexpected supervisor terminal exception: {exc!r}"
