"""ReAct typed turn state, snapshot policy, and runtime state codec.

Per ADR-0033 Stage 2 (ticket 03): ``ReActTurnState`` migrates from a plain
``TurnStateBase`` dataclass subclass to a ``GraphState(BaseModel)`` subclass
with per-field ``Annotated[T, LastValue]`` channel declarations.
``ReActSnapshotPolicy`` is simplified from ~310 lines of hand-written payload
flattening to ~50 lines calling ``state.checkpoint()`` /
``state.from_checkpoint()``.

ReAct still uses the old ``core/graph/`` engine for execution — only the state
type and snapshot path changed. Ticket 04 will switch to the new
``modex_graph`` engine and disassemble the god nodes.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import Field

from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.llm_struct import LLMErrorInfo  # noqa: F401 — needed for model_rebuild()
from modex_agent.core.message import ChatMessage  # noqa: F401 — needed for model_rebuild()
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import LLMResponse
from modex_agent.runtime.codec import RuntimeStateCodec, RuntimeStateCodecConfig
from modex_agent.runtime.enums import (
    AgentKind,
    MessageDeltaSource,
    OperationKind,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)
from modex_agent.runtime.models import (
    ApprovalTransaction,
    CancellationState,
    JsonValue,
    MessageDelta,
    OperationState,
    ResumePoint,
    ToolBatchState,
    ToolCallState,
    TurnIdentity,
    TurnSnapshot,
    TurnStateBase,
)
from modex_agent.runtime.policy import SnapshotPolicy
from modex_graph.channel import LastValue
from modex_graph.state import GraphState

from .constants import ReActNode

# =========================================================================
# ReActTurnState — GraphState(BaseModel) + TurnStateBase
# =========================================================================


class ReActTurnState(GraphState, TurnStateBase):
    """ReAct-specific turn state — a ``GraphState(BaseModel)`` subclass.

    Inherits ``TurnStateBase`` (for ``isinstance`` compatibility and shared
    methods like ``add_operation`` / ``update_operation``) and ``GraphState``
    (for per-field channel declaration + ``checkpoint()`` /
    ``from_checkpoint()``).

    All fields are declared with ``Annotated[T, LastValue]`` per ADR-0033 D4.
    The new explicit ``result`` field replaces the old
    ``custom[TurnCustomKey.GRAPH_RESULT]`` pattern (ADR-0033 D9.3).

    ``checkpoint()`` / ``from_checkpoint()`` are overridden to use Pydantic's
    ``model_dump(mode='json')`` / ``model_validate()`` — this handles all
    field types (BaseModels, dataclasses, enums, primitives, nested types)
    without per-type codec registration for non-Pydantic types like
    ``TurnIdentity`` / ``LLMResponse`` / ``AgentResult``.
    """

    # ---- TurnStateBase fields (re-declared for Pydantic compatibility) ----
    # TurnStateBase is a @dataclass; its fields use dataclasses.field() for
    # defaults, which Pydantic v2 doesn't process when inherited. Re-declaring
    # with pydantic.Field() ensures correct default handling + Annotated channel
    # metadata. The types match TurnStateBase exactly.
    identity: Annotated[TurnIdentity, LastValue] = Field(
        default_factory=lambda: TurnIdentity(
            agent_id="react",
            session=SessionInfo.from_str("default"),
            turn_id="default",
        )
    )
    agent_kind: Annotated[AgentKind, LastValue] = AgentKind.REACT
    phase: Annotated[TurnPhase, LastValue] = TurnPhase.CREATED
    created_at: Annotated[float, LastValue] = Field(default_factory=time.time)
    updated_at: Annotated[float, LastValue] = Field(default_factory=time.time)
    message_delta: Annotated[list[MessageDelta], LastValue] = Field(default_factory=list)
    operations: Annotated[list[OperationState], LastValue] = Field(default_factory=list)
    cancellation: Annotated[CancellationState | None, LastValue] = None
    custom: Annotated[dict[str, Any], LastValue] = Field(default_factory=dict)

    # ---- ReAct-specific fields ----
    current_node: Annotated[ReActNode, LastValue] = ReActNode.START
    iteration: Annotated[int, LastValue] = 0
    llm_response: Annotated[LLMResponse | None, LastValue] = None
    tool_batches: Annotated[list[ToolBatchState], LastValue] = Field(default_factory=list)
    approval: Annotated[ApprovalTransaction | None, LastValue] = None
    # NEW (ADR-0033 D9.3): explicit result field replaces
    # custom[TurnCustomKey.GRAPH_RESULT]. Written by EndNode, read by
    # ReActAgent.run() after engine returns.
    result: Annotated[AgentResult | None, LastValue] = None

    # ---- checkpoint / from_checkpoint (override GraphState per-channel
    #      mechanism with Pydantic model_dump / model_validate) ----
    #
    # Pydantic's built-in serialization handles all field types correctly:
    # - Pydantic BaseModels (ApprovalTransaction, ToolBatchState, etc.) via
    #   model_dump / model_validate.
    # - Dataclasses (TurnIdentity, LLMResponse, AgentResult, MessageDelta,
    #   OperationState, CancellationState) via Pydantic's dataclass support.
    # - Enums (ReActNode, AgentKind, TurnPhase, etc.) via value serialization.
    # - Primitives (int, float, str, bool, None) pass through.
    #
    # The per-channel mechanism in GraphState.checkpoint() uses
    # encode_value() / decode_value() which has a known limitation: PEP 604
    # pipe unions (``T | None``) produce ``types.UnionType`` which
    # ``decode_value`` doesn't handle (it only checks ``origin is Union`` from
    # ``typing``). Using ``Optional[T]`` (typing.Optional) works around this
    # for BaseModel fields with registered codecs, but dataclass types without
    # codecs still fall through to the lossy ``str(value)`` fallback.
    # ``model_dump`` / ``model_validate`` avoids all these issues.

    def checkpoint(self) -> dict[str, JsonValue]:
        """Serialize state to ``dict[str, JsonValue]`` via Pydantic model_dump.

        Round-trips with ``from_checkpoint(data)``. The returned dict is
        JSON-serializable and can be persisted directly as
        ``TurnSnapshot.state_payload``.
        """
        return self.model_dump(mode="json")

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> ReActTurnState:
        """Reconstruct a state instance from a checkpoint dict.

        Round-trips with ``checkpoint()``. Uses Pydantic's ``model_validate``
        which handles all field types including nested dataclasses and
        BaseModels.
        """
        return cls.model_validate(data)

    # ---- tool batch helpers ----

    def create_tool_batch(
        self,
        iteration: int,
        calls: list[ToolCallState],
    ) -> ToolBatchState:
        batch_id = uuid4_hex()
        op = self.add_operation(OperationKind.TOOL_BATCH, batch_id)
        batch = ToolBatchState(
            batch_id=batch_id,
            iteration=iteration,
            calls=calls,
            operation_id=op.operation_id,
        )
        self.tool_batches.append(batch)
        return batch

    def active_tool_batch(self) -> ToolBatchState | None:
        for b in reversed(self.tool_batches):
            if b.status not in (
                ToolBatchStatus.COMPLETED,
                ToolBatchStatus.FAILED,
                ToolBatchStatus.CANCELLED,
            ):
                return b
        return None

    def completed_tool_calls(self) -> list[ToolCallState]:
        return [
            tc
            for batch in self.tool_batches
            for tc in batch.calls
            if tc.status is ToolCallStatus.COMPLETED
        ]

    def mark_completed(self) -> None:
        self.phase = TurnPhase.COMPLETED
        self.updated_at = time.time()


def uuid4_hex() -> str:
    """Generate a UUID4 hex string (isolated for testability)."""
    from uuid import uuid4

    return uuid4().hex


def require_react_state(ctx: AgentContext) -> ReActTurnState:
    """Validate and return typed ReActTurnState from AgentContext.runtime.state."""
    runtime = ctx.runtime
    if runtime is None or not hasattr(runtime, "state"):
        raise TypeError("AgentContext.runtime is not an AgentRuntime")
    state = runtime.state
    if isinstance(state, ReActTurnState):
        return state
    raise TypeError(f"ReAct requires ReActTurnState, got {type(state).__name__}")


def get_react_state(ctx: AgentContext) -> ReActTurnState | None:
    """Safely extract ReActTurnState from AgentContext, returning None if unavailable."""
    if ctx.identity is None or ctx.runtime is None:
        return None
    state = ctx.runtime.state
    return state if isinstance(state, ReActTurnState) else None


# Resolve forward references. MessageDelta references ChatMessage (imported
# under TYPE_CHECKING in runtime/models.py) and LLMResponse references
# LLMErrorInfo (imported under TYPE_CHECKING in core/types.py). The runtime
# imports above (ChatMessage, LLMResponse) make these resolvable.
ReActTurnState.model_rebuild()


# =========================================================================
# ReActSnapshotPolicy — simplified via state.checkpoint() / from_checkpoint()
# =========================================================================


class ReActSnapshotPolicy(SnapshotPolicy):
    """ReAct-specific snapshot policy.

    Captures the full ``ReActTurnState`` via ``state.checkpoint()`` (Pydantic
    ``model_dump``) and restores via ``state.from_checkpoint()`` (Pydantic
    ``model_validate``). The old ``_build_payload`` (~230 lines),
    ``serialize_approval`` (~30 lines), ``approval_from_snapshot`` (~60 lines),
    and ``state_from_snapshot`` (~80 lines) are collapsed to ~50 lines total.

    ``ApprovalTransaction`` is now a ``LastValue`` channel field that uses
    Pydantic's built-in serialization — no hand-written codec pair.
    """

    def should_capture(self, state: TurnStateBase, reason: SnapshotReason) -> bool:
        return True

    def capture(self, state: TurnStateBase, reason: SnapshotReason) -> TurnSnapshot:
        if not isinstance(state, ReActTurnState):
            raise TypeError(
                f"ReActSnapshotPolicy requires ReActTurnState, got {type(state).__name__}"
            )
        return TurnSnapshot(
            identity=state.identity,
            agent_kind=AgentKind.REACT,
            phase=state.phase,
            reason=reason,
            resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=state.phase),
            message_delta=list(state.message_delta),
            state_payload=state.checkpoint(),
        )

    @staticmethod
    def replace_approval(snapshot: TurnSnapshot, tx: ApprovalTransaction) -> TurnSnapshot:
        """Replace the approval transaction in a snapshot.

        Deserializes the state from ``snapshot.state_payload`` via
        ``ReActTurnState.from_checkpoint()``, assigns ``state.approval = tx``
        (mutable, not frozen), re-checkpoints via ``state.checkpoint()``, and
        replaces ``snapshot.state_payload``.

        Semantic preserved: the external approval decision path
        (``ApprovalRenderer`` → ``apply_decision`` → ``replace_approval``)
        works identically — ``ApprovalTransaction.decisions`` dict is mutated
        by ``apply_decision``, then ``replace_approval`` persists the updated
        transaction into the snapshot.
        """
        state = ReActTurnState.from_checkpoint(dict(snapshot.state_payload))
        state.approval = tx
        snapshot.state_payload = state.checkpoint()
        return snapshot

    @staticmethod
    def state_from_snapshot(snapshot: TurnSnapshot) -> ReActTurnState:
        """Restore a ``ReActTurnState`` from a ``TurnSnapshot``."""
        return ReActTurnState.from_checkpoint(dict(snapshot.state_payload))

    @staticmethod
    def approval_from_snapshot(snapshot: TurnSnapshot) -> ApprovalTransaction | None:
        """Extract the approval transaction from a snapshot.

        Thin wrapper around ``ReActTurnState.from_checkpoint()`` — the OLD
        60-line manual field-by-field reconstruction is replaced by Pydantic
        ``model_validate``. Kept as a static method for backward compatibility
        with call sites in ``pipeline/approval_resumer.py``,
        ``pipeline/approval_renderer.py``, and tests.
        """
        return ReActTurnState.from_checkpoint(dict(snapshot.state_payload)).approval


# =========================================================================
# ReActRuntimeStateCodec
# =========================================================================


class ReActRuntimeStateCodec(RuntimeStateCodec):
    """Codec that round-trips ReAct snapshot payloads.

    Essentially unchanged from pre-migration: encodes ``TurnSnapshot`` to a
    JSON-compatible dict (payload ``dict[str, JsonValue]`` ↔ JSON bytes) and
    decodes back. The structure of ``state_payload`` changed (field names
    instead of custom enum keys), but the codec doesn't care about the
    internal structure — it just passes the dict through.
    """

    agent_kind = AgentKind.REACT

    def __init__(self, config: RuntimeStateCodecConfig | None = None) -> None:
        super().__init__(config)

    def encode_turn(self, snapshot: TurnSnapshot) -> Mapping[str, JsonValue]:
        for md in snapshot.message_delta:
            self._validate_provider_payload(md.provider_payload)

        return {
            "schema_version": snapshot.schema_version,
            "identity": {
                "agent_id": snapshot.identity.agent_id,
                "session_id": str(snapshot.identity.session),
                "turn_id": snapshot.identity.turn_id,
            },
            "agent_kind": snapshot.agent_kind.value,
            "phase": snapshot.phase.value,
            "reason": snapshot.reason.value,
            "created_at": snapshot.created_at,
            "resume_point": {
                "agent_kind": snapshot.resume_point.agent_kind.value,
                "phase": snapshot.resume_point.phase.value,
            },
            "message_delta": [self._encode_message_delta(md) for md in snapshot.message_delta],
            "state_payload": dict(snapshot.state_payload),
        }

    def decode_turn(self, payload: Mapping[str, JsonValue]) -> TurnSnapshot:
        identity_data: Mapping[str, JsonValue] = payload["identity"]  # type: ignore[assignment]
        resume_data: Mapping[str, JsonValue] = payload["resume_point"]  # type: ignore[assignment]
        return TurnSnapshot(
            identity=TurnIdentity(
                agent_id=str(identity_data["agent_id"]),
                session=SessionInfo.from_str(str(identity_data["session_id"])),
                turn_id=str(identity_data["turn_id"]),
            ),
            agent_kind=AgentKind(str(payload["agent_kind"])),
            phase=TurnPhase(str(payload["phase"])),
            reason=SnapshotReason(str(payload["reason"])),
            resume_point=ResumePoint(
                agent_kind=AgentKind(str(resume_data["agent_kind"])),
                phase=TurnPhase(str(resume_data["phase"])),
            ),
            message_delta=[
                self._decode_message_delta(md)  # type: ignore[arg-type]
                for md in payload.get("message_delta", [])  # type: ignore[union-attr]
            ],
            state_payload=dict(payload.get("state_payload", {})),  # type: ignore[arg-type]
            schema_version=int(payload.get("schema_version", 1)),  # type: ignore[arg-type]
            created_at=float(payload.get("created_at", time.time())),  # type: ignore[arg-type]
        )

    def _encode_message_delta(self, md: MessageDelta) -> dict[str, Any]:
        msg = md.message
        return {
            "role": msg.role,
            "content": msg.content,
            "source": md.source.value,
            "provider_payload": dict(md.provider_payload) if md.provider_payload else None,
            "tool_calls": msg.tool_calls,
            "tool_call_id": msg.tool_call_id,
            "name": msg.name,
        }

    def _decode_message_delta(self, data: Mapping[str, JsonValue]) -> MessageDelta:
        return MessageDelta(
            message=ChatMessage(
                role=str(data.get("role", "")),
                content=data.get("content"),  # type: ignore[arg-type]
                tool_calls=data.get("tool_calls"),  # type: ignore[arg-type]
                tool_call_id=data.get("tool_call_id"),  # type: ignore[arg-type]
                name=data.get("name"),  # type: ignore[arg-type]
            ),
            source=MessageDeltaSource(str(data["source"])),
            provider_payload=data.get("provider_payload"),  # type: ignore[arg-type]
        )


# Re-export types that other modules import from this module.
__all__ = [
    "ReActRuntimeStateCodec",
    "ReActSnapshotPolicy",
    "ReActTurnState",
    "get_react_state",
    "require_react_state",
]
