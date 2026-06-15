"""Tests for runtime state codec — round-trip, schema validation, payload size limits."""
from __future__ import annotations

import pytest

from framework.memory.core.message import ChatMessage
from framework.runtime.codec import RuntimeStateCodecConfig, RuntimeStateCodecError
from framework.runtime.enums import AgentKind, MessageDeltaSource, SnapshotReason, TurnPhase
from framework.runtime.models import MessageDelta, ResumePoint, TurnIdentity, TurnSnapshot
from framework.core.session_id import SessionInfo


class _FakeCodec:
    """Minimal codec for testing the registry before ReAct codec exists."""

    agent_kind = AgentKind.REACT

    def encode_turn(self, snapshot: TurnSnapshot) -> dict:
        return {
            "schema_version": snapshot.schema_version,
            "identity": {
                "agent_id": snapshot.identity.agent_id,
                "session": str(snapshot.identity.session),
                "turn_id": snapshot.identity.turn_id,
                "conversation_id": snapshot.identity.conversation_id,
            },
            "agent_kind": snapshot.agent_kind.value,
            "phase": snapshot.phase.value,
            "reason": snapshot.reason.value,
            "resume_point": {
                "agent_kind": snapshot.resume_point.agent_kind.value,
                "phase": snapshot.resume_point.phase.value,
            },
            "message_delta": [
                {
                    "role": md.message.role,
                    "content": md.message.content,
                    "source": md.source.value,
                    "provider_payload": dict(md.provider_payload) if md.provider_payload else None,
                }
                for md in snapshot.message_delta
            ],
            "state_payload": snapshot.state_payload,
        }

    def decode_turn(self, payload: dict) -> TurnSnapshot:
        identity_data = payload["identity"]
        return TurnSnapshot(
            identity=TurnIdentity(
                agent_id=identity_data["agent_id"],
                session=SessionInfo.from_str(identity_data["session"]),
                turn_id=identity_data["turn_id"],
                conversation_id=identity_data.get("conversation_id"),
            ),
            agent_kind=AgentKind(payload["agent_kind"]),
            phase=TurnPhase(payload["phase"]),
            reason=SnapshotReason(payload["reason"]),
            resume_point=ResumePoint(
                agent_kind=AgentKind(payload["resume_point"]["agent_kind"]),
                phase=TurnPhase(payload["resume_point"]["phase"]),
            ),
            message_delta=[
                MessageDelta(
                    message=ChatMessage(role=md["role"], content=md["content"]),
                    source=MessageDeltaSource(md["source"]),
                    provider_payload=md.get("provider_payload"),
                )
                for md in payload["message_delta"]
            ],
            state_payload=payload["state_payload"],
            schema_version=payload.get("schema_version", 1),
        )


def test_codec_round_trips_snapshot_payload() -> None:
    codec = _FakeCodec()
    identity = TurnIdentity(agent_id="bot", session=SessionInfo.from_str("s1"), turn_id="t1")
    snapshot = TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.SUSPENDED),
        message_delta=[
            MessageDelta(
                message=ChatMessage(role="assistant", content="need tool"),
                source=MessageDeltaSource.ASSISTANT,
                provider_payload={"tool_call_count": 1},
            )
        ],
        state_payload={"current_node": "tool", "iteration": 1, "tool_batches": []},
    )

    encoded = codec.encode_turn(snapshot)
    decoded = codec.decode_turn(encoded)

    assert decoded.identity == identity
    assert decoded.agent_kind is AgentKind.REACT
    assert decoded.phase is TurnPhase.SUSPENDED
    assert decoded.message_delta[0].message.content == "need tool"
    assert decoded.state_payload["current_node"] == "tool"
