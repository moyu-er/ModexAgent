"""Tests for concurrent turn safety — one active turn per agent-session."""
from __future__ import annotations

import pytest

from framework.runtime.codec import RuntimeStateCodecRegistry
from framework.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from framework.runtime.models import ResumePoint, TurnIdentity, TurnSnapshot
from framework.runtime.store import ActiveTurnConflictError
from framework.core.session_id import SessionId


class _FakeCodec:
    agent_kind = AgentKind.REACT

    def encode_turn(self, snapshot: TurnSnapshot) -> dict:
        return {
            "schema_version": snapshot.schema_version,
            "identity": {
                "agent_id": snapshot.identity.agent_id,
                "session": str(snapshot.identity.session),
                "turn_id": snapshot.identity.turn_id,
            },
            "agent_kind": snapshot.agent_kind.value,
            "phase": snapshot.phase.value,
            "reason": snapshot.reason.value,
            "resume_point": {
                "agent_kind": snapshot.resume_point.agent_kind.value,
                "phase": snapshot.resume_point.phase.value,
            },
            "message_delta": [],
            "state_payload": snapshot.state_payload,
        }

    def decode_turn(self, payload: dict):
        idata = payload["identity"]
        identity = TurnIdentity(
            agent_id=idata["agent_id"],
            session=SessionId.from_str(idata["session"]),
            turn_id=idata["turn_id"],
        )
        return TurnSnapshot(
            identity=identity,
            agent_kind=AgentKind(payload["agent_kind"]),
            phase=TurnPhase(payload["phase"]),
            reason=SnapshotReason(payload["reason"]),
            resume_point=ResumePoint(
                agent_kind=AgentKind(payload["resume_point"]["agent_kind"]),
                phase=TurnPhase(payload["resume_point"]["phase"]),
            ),
            message_delta=[],
            state_payload=payload["state_payload"],
            schema_version=payload.get("schema_version", 1),
        )


async def test_store_rejects_second_active_turn_for_same_agent_session(tmp_path) -> None:
    from framework.runtime.store import JsonFileTurnStateStore

    registry = RuntimeStateCodecRegistry({AgentKind.REACT: _FakeCodec()})
    store = JsonFileTurnStateStore(tmp_path, registry)

    first = TurnSnapshot(
        identity=TurnIdentity(agent_id="bot", session=SessionId.from_str("s1"), turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING),
        message_delta=[],
        state_payload={"current_node": "llm", "iteration": 1},
    )
    second = TurnSnapshot(
        identity=TurnIdentity(agent_id="bot", session=SessionId.from_str("s1"), turn_id="t2"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.SUSPENDED),
        message_delta=[],
        state_payload={"current_node": "tool", "iteration": 1},
    )

    await store.save_turn(first)

    with pytest.raises(ActiveTurnConflictError):
        await store.save_turn(second)
