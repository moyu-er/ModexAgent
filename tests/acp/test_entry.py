"""Entry lifecycle tests: the async serve ``finally`` closes the backend.

``_serve`` is exercised with a stubbed ``acp.run_agent`` receiving the agent
*instance* (the official SDK 0.12.1 lifecycle — the connection is injected
via ``on_connect``; the real stdio path is covered by ``test_e2e_turn.py``).
Contract: the agent is closed (drain) first, then ``backend.close()`` is
awaited exactly once — including when the serve loop raises or is cancelled.
"""

import asyncio
from typing import Any

import acp
import pytest

from modex_agent.acp.backend import AcpSessionHandle
from modex_agent.acp.entry import _serve
from modex_agent.acp.scripted import ScriptedAcpBackend
from modex_agent.acp.server import ModexAcpAgent
from modex_agent.acp.types import AcpOpenRequest


class CloseTrackingBackend(ScriptedAcpBackend):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _stub_run_agent(
    captured: dict[str, Any], *, raise_after: bool = False
) -> Any:
    async def stub(agent: ModexAcpAgent, **kwargs: Any) -> None:
        captured["agent"] = agent
        if raise_after:
            raise RuntimeError("serve failed")

    return stub


async def test_serve_passes_the_agent_instance_and_closes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CloseTrackingBackend()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(acp, "run_agent", _stub_run_agent(captured))
    await _serve(backend)
    # official 0.12.1 instance lifecycle: run_agent receives the instance,
    # the connection arrives later through agent.on_connect
    assert isinstance(captured["agent"], ModexAcpAgent)
    assert captured["agent"]._backend is backend
    assert backend.close_calls == 1


async def test_serve_closes_backend_even_when_run_agent_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CloseTrackingBackend()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(acp, "run_agent", _stub_run_agent(captured, raise_after=True))
    with pytest.raises(RuntimeError):
        await _serve(backend)
    assert backend.close_calls == 1


async def test_serve_closes_backend_even_when_agent_drain_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing agent drain must not skip the boot owner's backend close:
    both cleanup steps run, then the collected error surfaces."""
    backend = CloseTrackingBackend()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(acp, "run_agent", _stub_run_agent(captured))

    class FailingDrainAgent(ModexAcpAgent):
        async def aclose(self) -> None:
            raise RuntimeError("drain failed")

    monkeypatch.setattr("modex_agent.acp.server.ModexAcpAgent", FailingDrainAgent)
    with pytest.raises(RuntimeError, match="drain failed"):
        await _serve(backend)
    assert backend.close_calls == 1


async def test_serve_re_raises_cancellation_of_run_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the serve task must propagate CancelledError — never
    swallowed, never replaced by an ordinary exception."""

    async def cancellable_serve(agent: ModexAcpAgent, **kwargs: Any) -> None:
        await asyncio.Event().wait()  # blocks until cancelled

    backend = CloseTrackingBackend()
    monkeypatch.setattr(acp, "run_agent", cancellable_serve)
    task = asyncio.create_task(_serve(backend))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.close_calls == 1


async def test_serve_re_raises_cancellation_that_lands_during_agent_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel arriving while cleanup awaits the drain must NOT close the
    backend before the drain finishes: cleanup runs as one entry-owned
    unit the cancel cannot cut short — drain → backend close — and only
    after it completes does the original CancelledError propagate. The
    backend close must observe the drained state, not race it."""

    drain_started = asyncio.Event()
    drain_released = asyncio.Event()

    class BlockingDrainAgent(ModexAcpAgent):
        async def aclose(self) -> None:
            drain_started.set()
            await drain_released.wait()  # drain blocked until the test releases

    class GateObservingBackend(ScriptedAcpBackend):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            self.closed_with_drain_released: bool | None = None

        async def close(self) -> None:
            self.close_calls += 1
            self.closed_with_drain_released = drain_released.is_set()

    backend = GateObservingBackend()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(acp, "run_agent", _stub_run_agent(captured))
    monkeypatch.setattr("modex_agent.acp.server.ModexAcpAgent", BlockingDrainAgent)
    task = asyncio.create_task(_serve(backend))
    await asyncio.wait_for(drain_started.wait(), timeout=5)
    task.cancel()  # cancel lands while the drain is still blocked
    await asyncio.sleep(0)
    assert backend.close_calls == 0  # backend must not close before the drain ends
    drain_released.set()  # the drain now completes
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert backend.close_calls == 1
    assert backend.closed_with_drain_released is True


async def test_serve_cancellation_cleanup_failure_surfaces_not_log_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup failure during the cancel drain window must be VISIBLE —
    never reduced to a log line that a cancelled caller would read as clean.

    The entry's cleanup task fails (agent drain raises) while the serve is
    being cancelled: the CancelledError still propagates (cancel wins), but
    the cleanup failure must escape with it — e.g. on the CancelledError's
    context/cause chain — so the failure is observable, not swallowed."""

    drain_started = asyncio.Event()

    class FailingDrainAgent(ModexAcpAgent):
        async def aclose(self) -> None:
            drain_started.set()
            raise RuntimeError("drain failed during cancel")

    backend = CloseTrackingBackend()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(acp, "run_agent", _stub_run_agent(captured))
    monkeypatch.setattr("modex_agent.acp.server.ModexAcpAgent", FailingDrainAgent)

    async def cancellable_serve(agent: ModexAcpAgent, **kwargs: Any) -> None:
        await asyncio.Event().wait()  # blocks until cancelled

    monkeypatch.setattr(acp, "run_agent", cancellable_serve)
    task = asyncio.create_task(_serve(backend))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await asyncio.wait_for(task, timeout=5)
    assert backend.close_calls == 1
    # the cleanup failure rides the propagated cancel's cause chain
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "drain failed during cancel" in str(exc_info.value.__cause__)


async def test_serve_defaults_to_the_scripted_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(acp, "run_agent", _stub_run_agent(captured))
    await _serve(None)
    agent = captured["agent"]
    assert isinstance(agent, ModexAcpAgent)
    handle = await agent._backend.open(AcpOpenRequest(kind="new", cwd="."))  # type: ignore[arg-type]
    assert isinstance(handle, AcpSessionHandle)


async def test_cancel_during_cleanup_preserves_prior_serve_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    failure = RuntimeError("protocol transport failed")

    class BlockingDrainAgent(ModexAcpAgent):
        async def aclose(self) -> None:
            started.set()
            await release.wait()

    async def failed_serve(agent: ModexAcpAgent, **kwargs: Any) -> None:
        raise failure

    backend = CloseTrackingBackend()
    monkeypatch.setattr(acp, "run_agent", failed_serve)
    monkeypatch.setattr("modex_agent.acp.server.ModexAcpAgent", BlockingDrainAgent)
    task = asyncio.create_task(_serve(backend))
    try:
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 5)
        assert caught.value.__cause__ is failure
        assert backend.close_calls == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_serve_without_open_sessions_still_closes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = CloseTrackingBackend()
    monkeypatch.setattr(acp, "run_agent", _stub_run_agent({}))
    await _serve(backend)
    assert backend.close_calls == 1
