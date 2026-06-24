"""Tests for JsonFileTurnStateStore — save, load, delete, list, collision safety."""
from __future__ import annotations

import pytest

from modex_agent.memory.core.message import ChatMessage
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
from modex_agent.runtime.enums import AgentKind, MessageDeltaSource, SnapshotReason, TurnPhase
from modex_agent.runtime.models import MessageDelta, ResumePoint, StateQueryScope, TurnIdentity, TurnSnapshot
from modex_agent.core.session_id import SessionInfo


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

    def decode_turn(self, payload: dict) -> TurnSnapshot:
        idata = payload["identity"]
        identity = TurnIdentity(
            agent_id=idata["agent_id"],
            session=SessionInfo.from_str(idata["session"]),
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


@pytest.fixture
def registry() -> RuntimeStateCodecRegistry:
    return RuntimeStateCodecRegistry({AgentKind.REACT: _FakeCodec()})


async def test_json_file_turn_store_save_load_delete(tmp_path, registry) -> None:
    from modex_agent.runtime.store import JsonFileTurnStateStore

    store = JsonFileTurnStateStore(tmp_path, registry)
    identity = TurnIdentity(agent_id="bot", session=SessionInfo.from_str("group_1"), turn_id="t1")
    snapshot = TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.SUSPENDED),
        message_delta=[
            MessageDelta(
                message=ChatMessage(role="assistant", content="approve?"),
                source=MessageDeltaSource.ASSISTANT,
            )
        ],
        state_payload={"current_node": "tool", "iteration": 1},
    )

    await store.save_turn(snapshot)
    loaded = await store.load_turn(identity)
    active = await store.list_active_turns(
        StateQueryScope(agent_id="bot", session_id="group_1", phase=TurnPhase.SUSPENDED)
    )

    assert loaded is not None
    assert loaded.identity == identity
    assert len(active) == 1

    await store.delete_turn(identity)
    assert await store.load_turn(identity) is None


async def test_json_file_turn_store_handles_path_sanitization(tmp_path, registry) -> None:
    from modex_agent.runtime.store import JsonFileTurnStateStore

    store = JsonFileTurnStateStore(tmp_path, registry)

    first = TurnIdentity(agent_id="bot", session=SessionInfo.from_str("a_b"), turn_id="t1")
    second = TurnIdentity(agent_id="bot", session=SessionInfo.from_str("a:b"), turn_id="t1")

    snapshot_a = TurnSnapshot(
        identity=first,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING),
        message_delta=[],
        state_payload={"current_node": "llm", "iteration": 1},
    )
    snapshot_b = TurnSnapshot(
        identity=second,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING),
        message_delta=[],
        state_payload={"current_node": "llm", "iteration": 1},
    )

    await store.save_turn(snapshot_a)
    await store.save_turn(snapshot_b)
    assert await store.load_turn(first) is not None
    assert await store.load_turn(second) is not None
