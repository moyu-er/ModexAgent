"""TurnStateStore conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`JsonFileTurnStateStore` (over a workspace dir + codec registry).
SQLite: :class:`SqliteTurnStateStore` (over ``ConnectionManager`` + codec registry).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.react.state import ReActRuntimeStateCodec
from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
from modex_agent.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from modex_agent.runtime.models import ResumePoint, StateQueryScope, TurnIdentity, TurnSnapshot
from modex_agent.runtime.store import JsonFileTurnStateStore, TurnStateStore


def _make_registry() -> RuntimeStateCodecRegistry:
    return RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})


def _make_snapshot(
    *,
    agent_id: str = "main",
    session_id: str = "s1.main",
    turn_id: str = "t1",
    phase: TurnPhase = TurnPhase.SUSPENDED,
    reason: SnapshotReason = SnapshotReason.TOOL_APPROVAL_REQUIRED,
) -> TurnSnapshot:
    identity = TurnIdentity(
        agent_id=agent_id,
        session=SessionInfo.from_str(session_id),
        turn_id=turn_id,
    )
    state_payload: dict[str, Any] = {
        "current_node": "tool",
        "iteration": 1,
        "tool_batches": [],
    }
    return TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=phase,
        reason=reason,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=phase),
        message_delta=[],
        state_payload=state_payload,
    )


@pytest.fixture(params=["file", "sqlite"])
async def turn_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[TurnStateStore]:
    """Parametrized TurnStateStore — file (JsonFileTurnStateStore) or sqlite."""
    registry = _make_registry()
    if request.param == "file":
        yield JsonFileTurnStateStore(tmp_path / "turns_file", registry)
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteTurnStateStore(mgr, registry)
        await mgr.close()


class TestTurnStateStoreConformance:
    """Same behavior on both backends."""

    async def test_load_missing_returns_none(self, turn_store: TurnStateStore) -> None:
        identity = TurnIdentity(
            agent_id="main",
            session=SessionInfo.from_str("s1.main"),
            turn_id="nope",
        )
        assert await turn_store.load_turn(identity) is None

    async def test_save_then_load_roundtrip(self, turn_store: TurnStateStore) -> None:
        snapshot = _make_snapshot()
        await turn_store.save_turn(snapshot)
        loaded = await turn_store.load_turn(snapshot.identity)
        assert loaded is not None
        assert loaded.identity.turn_id == "t1"
        assert loaded.phase == TurnPhase.SUSPENDED
        assert loaded.agent_kind == AgentKind.REACT

    async def test_delete_removes_turn(self, turn_store: TurnStateStore) -> None:
        snapshot = _make_snapshot()
        await turn_store.save_turn(snapshot)
        await turn_store.delete_turn(snapshot.identity)
        assert await turn_store.load_turn(snapshot.identity) is None

    async def test_delete_missing_is_noop(self, turn_store: TurnStateStore) -> None:
        identity = TurnIdentity(
            agent_id="main",
            session=SessionInfo.from_str("s1.main"),
            turn_id="nope",
        )
        await turn_store.delete_turn(identity)  # must not raise

    async def test_list_active_turns_empty(self, turn_store: TurnStateStore) -> None:
        result = await turn_store.list_active_turns(StateQueryScope())
        assert result == []

    async def test_list_active_turns_filters_by_session(self, turn_store: TurnStateStore) -> None:
        await turn_store.save_turn(_make_snapshot(turn_id="t1"))
        await turn_store.save_turn(
            _make_snapshot(
                agent_id="main",
                session_id="s2.main",
                turn_id="t2",
            )
        )
        scope = StateQueryScope(agent_id="main", session_id="s1.main")
        result = await turn_store.list_active_turns(scope)
        assert len(result) == 1
        assert result[0].identity.turn_id == "t1"

    async def test_completed_turn_does_not_block_new_turn(self, turn_store: TurnStateStore) -> None:
        await turn_store.save_turn(_make_snapshot(turn_id="t1", phase=TurnPhase.COMPLETED))
        # a new running turn should be allowed since the old one is completed
        await turn_store.save_turn(_make_snapshot(turn_id="t2", phase=TurnPhase.RUNNING))
        loaded = await turn_store.load_turn(
            TurnIdentity(
                agent_id="main",
                session=SessionInfo.from_str("s1.main"),
                turn_id="t2",
            )
        )
        assert loaded is not None
        assert loaded.phase == TurnPhase.RUNNING
