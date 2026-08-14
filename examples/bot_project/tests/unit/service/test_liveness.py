"""Tests for DefaultLivenessProvider — dual-signal liveness + deletion reservation.

Six scenarios:
1. Active durable turn → True
2. No active turn + no registry → False
3. Turn store raises → True (fail-safe)
4. FILE-backend (no turn store) → False, or True if registry active
5. try_reserve_deletion: True first, False second, release clears
6. is_session_active still works after reservation
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.liveness import DefaultLivenessProvider, LivenessProvider

from modex_agent.core.session_id import SessionInfo
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from modex_agent.runtime.models import ResumePoint, TurnIdentity, TurnSnapshot
from modex_agent.runtime.store import InMemoryTurnStateStore, TurnStateStore

_SESSION_ID = "abc123.default"
_AGENT_ID = "default"
_WORKSPACE = Path("/fake/workspace")


def _make_snapshot(session_id: str, agent_id: str, phase: TurnPhase) -> TurnSnapshot:
    identity = TurnIdentity(
        agent_id=agent_id,
        session=SessionInfo(session_id=session_id, agent_name=agent_id),
        turn_id="turn-1",
    )
    return TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=phase,
        reason=SnapshotReason.ITERATION,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=phase),
        message_delta=[],
        state_payload={},
    )


class _RaisingTurnStateStore(TurnStateStore):
    """TurnStateStore that raises on every query — simulates DB connection loss."""

    async def save_turn(self, snapshot: TurnSnapshot) -> None:
        pass

    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None:
        return None

    async def delete_turn(self, identity: TurnIdentity) -> None:
        pass

    async def list_active_turns(self, scope) -> list[TurnSnapshot]:
        raise RuntimeError("simulated DB connection lost")


def _noop_turn_store_resolver(store: TurnStateStore | None):
    async def _resolver(_sid: str, _root: Path) -> TurnStateStore | None:
        return store

    return _resolver


def _noop_registry_resolver(registry: TurnSessionRegistry | None):
    def _resolver(_sid: str, _root: Path) -> TurnSessionRegistry | None:
        return registry

    return _resolver


# ── Scenario 1: active durable turn → True ──────────────────────────────────


async def test_active_durable_turn_returns_true() -> None:
    store = InMemoryTurnStateStore()
    await store.save_turn(_make_snapshot(_SESSION_ID, _AGENT_ID, TurnPhase.RUNNING))

    provider = DefaultLivenessProvider(
        turn_store_resolver=_noop_turn_store_resolver(store),
    )
    assert await provider.is_session_active(_SESSION_ID, _WORKSPACE) is True


# ── Scenario 2: no active turn + no registry → False ────────────────────────


async def test_no_active_turn_no_registry_returns_false() -> None:
    store = InMemoryTurnStateStore()
    # A completed turn is NOT active
    await store.save_turn(_make_snapshot(_SESSION_ID, _AGENT_ID, TurnPhase.COMPLETED))

    provider = DefaultLivenessProvider(
        turn_store_resolver=_noop_turn_store_resolver(store),
    )
    assert await provider.is_session_active(_SESSION_ID, _WORKSPACE) is False


# ── Scenario 3: turn store raises → True (fail-safe) ────────────────────────


async def test_turn_store_raises_returns_true_fail_safe() -> None:
    store = _RaisingTurnStateStore()
    provider = DefaultLivenessProvider(
        turn_store_resolver=_noop_turn_store_resolver(store),
    )
    assert await provider.is_session_active(_SESSION_ID, _WORKSPACE) is True


# ── Scenario 4: FILE-backend (no turn store) → fail-safe True ──────────────


async def test_file_backend_no_turn_store() -> None:
    # No turn store + no registry → fail-safe True (assume active when
    # the durable signal cannot be resolved).
    provider = DefaultLivenessProvider(
        turn_store_resolver=_noop_turn_store_resolver(None),
    )
    assert await provider.is_session_active(_SESSION_ID, _WORKSPACE) is True

    # Phase B: no turn store + active registry → True
    registry = TurnSessionRegistry()
    task = asyncio.ensure_future(asyncio.Event().wait())
    registry.register_task(_SESSION_ID, task)
    try:
        provider_with_registry = DefaultLivenessProvider(
            turn_store_resolver=_noop_turn_store_resolver(None),
            registry_resolver=_noop_registry_resolver(registry),
        )
        assert (
            await provider_with_registry.is_session_active(_SESSION_ID, _WORKSPACE)
            is True
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ── Scenario 5: try_reserve_deletion lifecycle ──────────────────────────────


async def test_reserve_deletion_lifecycle() -> None:
    provider = DefaultLivenessProvider(
        turn_store_resolver=_noop_turn_store_resolver(None),
    )

    # First call acquires
    assert await provider.try_reserve_deletion(_SESSION_ID, _WORKSPACE) is True
    # Second call for same session is rejected
    assert await provider.try_reserve_deletion(_SESSION_ID, _WORKSPACE) is False
    # Release clears the reservation
    await provider.release_deletion(_SESSION_ID)
    # After release, can acquire again
    assert await provider.try_reserve_deletion(_SESSION_ID, _WORKSPACE) is True
    # Release is idempotent (no-op if not held)
    await provider.release_deletion("nonexistent.session")
    await provider.release_deletion(_SESSION_ID)


# ── Scenario 6: is_session_active still works after reservation ─────────────


async def test_liveness_check_unaffected_by_reservation() -> None:
    store = InMemoryTurnStateStore()
    await store.save_turn(_make_snapshot(_SESSION_ID, _AGENT_ID, TurnPhase.RUNNING))

    provider = DefaultLivenessProvider(
        turn_store_resolver=_noop_turn_store_resolver(store),
    )

    assert await provider.try_reserve_deletion(_SESSION_ID, _WORKSPACE) is True
    # Liveness check must still detect the active turn despite the reservation
    assert await provider.is_session_active(_SESSION_ID, _WORKSPACE) is True


# ── ABC enforcement ─────────────────────────────────────────────────────────


def test_liveness_provider_is_abstract() -> None:
    with pytest.raises(TypeError):
        LivenessProvider()  # type: ignore[abstract]
