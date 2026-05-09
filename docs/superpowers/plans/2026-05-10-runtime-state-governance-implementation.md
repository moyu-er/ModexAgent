# Runtime State Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace duplicated ReAct runtime, checkpoint, approval, resume, and control persistence with typed runtime state, turn snapshots, and scoped stores, then fully delete the old incompatible implementation paths.

**Architecture:** Add a new `framework/runtime/` package for generic runtime identities, state models, snapshot policies, codecs, and stores. ReAct owns only ReAct-specific state and snapshot payloads; Pipeline and AgentSession create `AgentRuntimeServices` and `ReActTurnState`; approval becomes a transaction inside turn state. Final cleanup removes legacy metadata/extension state paths, old approval stores, old resume stores, old checkpoint stores, and any compatibility aliases.

**Tech Stack:** Python 3.12 dataclasses, `StrEnum`, ABCs, `pytest`, `mypy`, `ruff`, existing `framework.core.types`, `framework.memory`, ReAct graph nodes, Pipeline, HookRunner, InterceptorChain, and bot project tests.

---

## Design Source

Implement from:

- `docs/superpowers/specs/2026-05-09-runtime-state-governance-design.md`

Key decisions that are not optional:

- Final implementation is breaking. Do not keep backward-compatible re-exports, fallback reads, deprecated aliases, legacy metadata keys, or old approval/checkpoint formats.
- The new persisted turn store is `TurnStateStore`, not `RuntimeStateStore`.
- Approval is `ApprovalTransaction` inside turn state, not `ApprovalStateStore`.
- `ResumePoint` is only query metadata: `agent_kind` and `phase`.
- ReAct resume position is decoded from ReAct snapshot payload.
- Clean mode still creates typed turn state; it only disables optional services and persistence.
- `message_delta` stores current-turn messages only. It must not copy full session history into approval or runtime snapshots.

## File Structure

### New Framework Runtime Package

- Create `framework/runtime/__init__.py`
  Exports stable runtime models, services, codec registry, and store ABCs.

- Create `framework/runtime/enums.py`
  Owns `StateScope`, `AgentKind`, `TurnPhase`, `OperationKind`, `OperationStatus`, `MessageDeltaSource`, `ToolBatchStatus`, `ToolCallStatus`, `CancellationSource`, `ControlCommandKind`, `SnapshotReason`, `ApprovalSubjectType`, and `ApprovalDenyPolicy`.

- Create `framework/runtime/models.py`
  Owns `JsonValue`, `ToolArguments`, `TurnIdentity`, `RuntimeErrorState`, `CancellationState`, `MessageDelta`, `OperationState`, `ApprovalRequestState`, `ApprovalTransaction`, `ToolCallState`, `ToolBatchState`, `ControlCommandState`, `ControlMutation`, `ResumePoint`, `TurnSnapshot`, `StateQueryScope`, and `TurnSummary`.

- Create `framework/runtime/services.py`
  Owns `AgentRuntime`, `AgentRuntimeServices`, `require_runtime_state`, and no-op service helpers.

- Create `framework/runtime/policy.py`
  Owns `SnapshotPolicy` ABC and shared snapshot-policy helpers.

- Create `framework/runtime/codec.py`
  Owns `RuntimeStateCodec`, `RuntimeStateCodecConfig`, `RuntimeStateCodecRegistry`, codec errors, JSON value validation, enum conversion helpers, and provider payload size validation.

- Create `framework/runtime/store.py`
  Owns `TurnStateStore`, `RuntimeCommandStore`, `InMemoryTurnStateStore`, `NoOpTurnStateStore`, `JsonFileTurnStateStore`, `InMemoryRuntimeCommandStore`, `NoOpRuntimeCommandStore`, and `JsonFileRuntimeCommandStore`.

### ReAct Runtime State

- Modify `framework/agents/react/state.py`
  Delete `TurnResumeState`, `TurnResumeStateStore`, `InMemoryTurnResumeStateStore`, and `StateStoreTurnResumeStateStore`. Replace with `ReActTurnState`, `ReActSnapshotPolicy`, `ReActRuntimeStateCodec`, and helper methods for operation indexing and tool-batch updates.

- Modify `framework/agents/react/runtime.py`
  Replace old `ReActRuntime` with usage of `AgentRuntimeServices`. Remove extension consumption from this module.

- Modify `framework/agents/react/approval.py`
  Retain only approval classification and deny policy. Remove `suspend_strategy` from `ApprovalRuntime`.

- Modify `framework/agents/react/constants.py`
  Remove metadata keys that represented runtime state: approval denial, deny-as-cancel, pre-approved tool IDs, iteration messages, resume state, tool decisions, LLM response, current iteration, cancellation reason, and injection cycle counters after their replacements exist.

- Modify `framework/agents/react/agent.py`
  Use `context.runtime.state`, not `context.metadata`, for ReAct lifecycle, checkpoint, cancellation, and cleanup.

- Modify `framework/agents/react/nodes/start.py`
  Initialize `ReActTurnState` phase, node, and iteration.

- Modify `framework/agents/react/nodes/llm.py`
  Write LLM response, assistant `MessageDelta`, and LLM operation state.

- Modify `framework/agents/react/nodes/tool.py`
  Create `ToolBatchState` and `ApprovalTransaction`, save suspended snapshots through `TurnStateStore`, resume from state, update tool-call results, and snapshot progress after each tool call.

- Modify `framework/agents/react/nodes/end.py`
  Commit `message_delta` to memory and delete turn snapshots on terminal success.

### Pipeline, Session, Hooks, Interceptors, and Control

- Modify `framework/core/agent.py`
  Make `AgentContext` non-generic, add `identity: TurnIdentity`, replace loose `runtime` typing with `AgentRuntime`, and remove final dependency on `extensions` and `metadata` for runtime state.

- Modify `framework/pipeline/pipeline.py`
  Create `TurnIdentity`, `AgentRuntimeServices`, and ReAct state per message; route approval commands through `TurnStateStore`; enforce one active turn per `(agent_id, session_id)`; stop constructing old checkpoint IDs.

- Modify `framework/pipeline/context_assembler.py`
  Stop placing runtime services in `ctx.extensions`; return or accept `AgentRuntimeServices` explicitly.

- Modify `framework/pipeline/approval_renderer.py`
  Render approval requests from `ApprovalTransaction` loaded from `TurnStateStore`; stop reading old approval state files and old resume state files.

- Modify `framework/session/agent_session.py`
  Create the same runtime shape as Pipeline; use `NoOpTurnStateStore` when persistence is disabled.

- Modify `framework/interceptor/abc.py`
  Add `turn_state: TurnStateBase` to `TurnContext`, `IterationContext`, `LLMCallContext`, `LLMStreamContext`, and `ToolCallContext`; add `LLMRequest`.

- Modify `framework/interceptor/chain.py`
  Construct updated interceptor contexts and pass `turn_state`.

- Modify builtin interceptors under `framework/interceptor/builtin/`
  Replace metadata flags with typed state mutations or typed decisions.

- Modify `framework/hook/abc.py` and `framework/hook/runner.py`
  Ensure hooks receive `AgentContext` with typed runtime state.

- Modify builtin hooks under `framework/hook/builtin/`
  Replace `RuntimeContext` and metadata usage with typed state access.

- Modify `framework/control/runtime.py`
  Keep command-plane behavior; handlers return `ControlMutation`; durable command persistence uses `RuntimeCommandStore`.

- Modify `framework/control/store.py`
  Delete or move old `ControlStore` implementation after `RuntimeCommandStore` callers are migrated.

- Delete `framework/control/checkpoint.py` after all imports are removed.

### Approval Package Cleanup

- Modify `framework/approval/constants.py`
  Keep shared enums only if they remain framework-wide; otherwise move runtime-owned state enums to `framework/runtime/enums.py` and update imports.

- Modify `framework/approval/types.py`
  Keep command parsing and user-facing approval action types only.

- Delete `framework/approval/state.py` after `ApprovalRequest` and `ApprovalState` are replaced by runtime models.

- Delete `framework/approval/store.py` after old stores have no imports.

### Bot Project

- Modify `examples/bot_project/bot/service/core.py`
  Configure one `JsonFileTurnStateStore` and one `JsonFileRuntimeCommandStore`; inject via `AgentRuntimeServices`; remove approval workspace/store special cases.

- Modify `examples/bot_project/tests/test_approval_flow.py`
  Assert approval suspend/resume uses runtime turn snapshots and stores no full session history.

- Modify `examples/bot_project/tests/test_runtime_defaults.py`
  Assert bot default runtime services use new stores.

- Modify `examples/bot_project/docs/memory-system.md`
  Clarify runtime snapshots are separate from memory compression/governance.

### Tests

- Create `tests/unit/runtime/test_models.py`
- Create `tests/unit/runtime/test_codec.py`
- Create `tests/unit/runtime/test_codec_registry.py`
- Create `tests/unit/runtime/test_file_store.py`
- Create `tests/unit/runtime/test_command_store.py`
- Create `tests/unit/runtime/test_runtime_services.py`
- Create `tests/unit/runtime/test_turn_lifecycle.py`
- Create `tests/unit/runtime/test_concurrent_turns.py`
- Create `tests/unit/runtime/test_clean_mode.py`
- Create `tests/unit/agents/react/test_turn_state.py`
- Create or update `tests/unit/approval/test_transaction_state.py`
- Create or update `tests/unit/pipeline/test_runtime_resume.py`
- Create or update `tests/unit/interceptor/test_runtime_state_interceptors.py`
- Create or update `tests/unit/hook/test_runtime_state_hooks.py`
- Create `tests/unit/runtime/test_legacy_cleanup.py`

---

## Task 1: Runtime Enums and Models

**Files:**
- Create: `framework/runtime/__init__.py`
- Create: `framework/runtime/enums.py`
- Create: `framework/runtime/models.py`
- Test: `tests/unit/runtime/test_models.py`

- [ ] **Step 1: Write failing model tests**

Add this test file:

```python
from __future__ import annotations

from framework.runtime.enums import (
    AgentKind,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    ToolBatchStatus,
    TurnPhase,
)
from framework.runtime.models import (
    MessageDelta,
    OperationState,
    ToolArguments,
    TurnIdentity,
    TurnStateBase,
)
from framework.memory.core.message import ChatMessage


def test_turn_identity_is_explicit_and_stable() -> None:
    identity = TurnIdentity(
        agent_id="bot",
        session_id="session-1",
        turn_id="turn-1",
        conversation_id="conversation-1",
    )

    assert identity.agent_id == "bot"
    assert identity.session_id == "session-1"
    assert identity.turn_id == "turn-1"
    assert identity.conversation_id == "conversation-1"


def test_turn_state_starts_without_full_session_history() -> None:
    identity = TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1")
    state = TurnStateBase(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )

    assert state.message_delta == []
    assert state.operations == []
    assert state.cancellation is None


def test_message_delta_wraps_normalized_message_and_source() -> None:
    message = ChatMessage(role="assistant", content="hello")
    delta = MessageDelta(
        message=message,
        source=MessageDeltaSource.ASSISTANT,
        provider_payload={"tool_call_count": 0},
    )

    assert delta.message.content == "hello"
    assert delta.source is MessageDeltaSource.ASSISTANT
    assert delta.provider_payload == {"tool_call_count": 0}


def test_tool_arguments_are_a_typed_value_object() -> None:
    args = ToolArguments(values={"path": "notes.md", "limit": 3})

    assert args.values["path"] == "notes.md"
    assert args.values["limit"] == 3


def test_operation_index_tracks_lifecycle_without_payload_duplication() -> None:
    op = OperationState(
        operation_id="op-1",
        kind=OperationKind.TOOL_BATCH,
        status=OperationStatus.CREATED,
        subject_id="batch-1",
    )

    assert op.kind is OperationKind.TOOL_BATCH
    assert op.status is OperationStatus.CREATED
    assert op.subject_id == "batch-1"


def test_runtime_enums_use_typed_values() -> None:
    assert AgentKind.REACT.value == "react"
    assert TurnPhase.SUSPENDED.value == "suspended"
    assert ToolBatchStatus.SUSPENDED.value == "suspended"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
python -m pytest tests/unit/runtime/test_models.py -v
```

Expected: fail with `ModuleNotFoundError: No module named 'framework.runtime'`.

- [ ] **Step 3: Implement runtime enums**

Create `framework/runtime/enums.py`:

```python
from __future__ import annotations

from enum import StrEnum


class StateScope(StrEnum):
    PROCESS = "process"
    AGENT = "agent"
    SESSION = "session"
    TURN = "turn"
    OPERATION = "operation"


class AgentKind(StrEnum):
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"


class TurnPhase(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationKind(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_BATCH = "tool_batch"
    TOOL_CALL = "tool_call"
    APPROVAL = "approval"
    CONTROL_COMMAND = "control_command"


class OperationStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class MessageDeltaSource(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class ToolBatchStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    PREEMPTED = "preempted"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CancellationSource(StrEnum):
    USER_COMMAND = "user_command"
    TIMEOUT = "timeout"
    POLICY = "policy"
    TOOL_DENIAL = "tool_denial"
    CONTROL_COMMAND = "control_command"


class ControlCommandKind(StrEnum):
    CANCEL_TURN = "cancel_turn"
    PAUSE_TURN = "pause_turn"
    RESUME_TURN = "resume_turn"
    INJECT_INPUT = "inject_input"


class SnapshotReason(StrEnum):
    LLM_COMPLETED = "llm_completed"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    TOOL_BATCH_PROGRESS = "tool_batch_progress"
    TURN_INTERRUPTED = "turn_interrupted"
    ERROR_RECOVERY = "error_recovery"


class ApprovalSubjectType(StrEnum):
    TOOL_CALL = "tool_call"
    TOOL_BATCH = "tool_batch"
    PLAN_STEP = "plan_step"


class ApprovalDenyPolicy(StrEnum):
    TOOL_RESULT_ONLY = "tool_result_only"
    CANCEL_TURN = "cancel_turn"
```

- [ ] **Step 4: Implement runtime models**

Create `framework/runtime/models.py` with the public dataclasses from the design. Use `field(default_factory=time.time)` for timestamp defaults, and implement `TurnStateBase.add_operation()` plus `update_operation()`.

```python
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeAlias
from uuid import uuid4

from framework.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from framework.core.tool_manager import ToolResult
from framework.memory.core.message import ChatMessage

from .enums import (
    AgentKind,
    ApprovalSubjectType,
    CancellationSource,
    ControlCommandKind,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    SnapshotReason,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class ToolArguments:
    values: Mapping[str, JsonValue]


@dataclass(frozen=True)
class TurnIdentity:
    agent_id: str
    session_id: str
    turn_id: str
    conversation_id: str | None = None


@dataclass
class RuntimeErrorState:
    error_type: str
    message: str
    retryable: bool


@dataclass
class CancellationState:
    reason: str
    source: CancellationSource
    requested_at: float = field(default_factory=time.time)
    operation_id: str | None = None


@dataclass
class MessageDelta:
    message: ChatMessage
    source: MessageDeltaSource
    provider_payload: Mapping[str, JsonValue] | None = None


@dataclass
class OperationState:
    operation_id: str
    kind: OperationKind
    status: OperationStatus
    subject_id: str | None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: RuntimeErrorState | None = None


@dataclass
class TurnStateBase:
    identity: TurnIdentity
    agent_kind: AgentKind
    phase: TurnPhase
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    message_delta: list[MessageDelta] = field(default_factory=list)
    operations: list[OperationState] = field(default_factory=list)
    cancellation: CancellationState | None = None

    def add_operation(
        self,
        kind: OperationKind,
        subject_id: str | None,
        status: OperationStatus = OperationStatus.CREATED,
    ) -> OperationState:
        op = OperationState(
            operation_id=uuid4().hex,
            kind=kind,
            status=status,
            subject_id=subject_id,
        )
        self.operations.append(op)
        self.updated_at = time.time()
        return op

    def update_operation(
        self,
        operation_id: str,
        status: OperationStatus,
        error: RuntimeErrorState | None = None,
    ) -> None:
        for op in self.operations:
            if op.operation_id == operation_id:
                op.status = status
                op.error = error
                op.updated_at = time.time()
                self.updated_at = op.updated_at
                return
        raise KeyError(f"operation not found: {operation_id}")


@dataclass
class ApprovalRequestState:
    request_id: str
    approval_id: str
    tool_call_id: str
    tool_name: str
    arguments: ToolArguments
    tier: ApprovalTier
    iteration: int
    created_at: float = field(default_factory=time.time)


@dataclass
class ApprovalTransaction:
    approval_id: str
    turn_id: str
    subject_type: ApprovalSubjectType
    subject_ids: list[str]
    requests: list[ApprovalRequestState]
    decisions: dict[str, ApprovalDecision] = field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    deny_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class ToolCallState:
    call_id: str
    tool_name: str
    arguments: ToolArguments
    approval_id: str | None = None
    decision: ApprovalDecision | None = None
    result: ToolResult | None = None
    status: ToolCallStatus = ToolCallStatus.PENDING
    operation_id: str | None = None


@dataclass
class ToolBatchState:
    batch_id: str
    iteration: int
    calls: list[ToolCallState]
    approval_id: str | None = None
    status: ToolBatchStatus = ToolBatchStatus.CREATED
    operation_id: str | None = None


@dataclass
class ControlCommandState:
    command_id: str
    kind: ControlCommandKind
    agent_id: str
    session_id: str | None
    payload: Mapping[str, JsonValue]
    status: OperationStatus = OperationStatus.CREATED
    created_at: float = field(default_factory=time.time)
    applied_at: float | None = None


@dataclass(frozen=True)
class ControlMutation:
    command_id: str
    operation_id: str
    target_phase: TurnPhase | None = None
    cancellation: CancellationState | None = None
    snapshot_reason: SnapshotReason | None = None


@dataclass(frozen=True)
class ResumePoint:
    agent_kind: AgentKind
    phase: TurnPhase


@dataclass
class TurnSnapshot:
    identity: TurnIdentity
    agent_kind: AgentKind
    phase: TurnPhase
    reason: SnapshotReason
    resume_point: ResumePoint
    message_delta: list[MessageDelta]
    state_payload: Mapping[str, JsonValue]
    schema_version: int = 1
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class StateQueryScope:
    agent_id: str | None = None
    session_id: str | None = None
    agent_kind: AgentKind | None = None
    phase: TurnPhase | None = None
    reason: SnapshotReason | None = None
    created_before: float | None = None


@dataclass
class TurnSummary:
    identity: TurnIdentity
    agent_kind: AgentKind
    final_phase: TurnPhase
    completed_at: float
    operation_count: int
    error: RuntimeErrorState | None = None
    cancellation: CancellationState | None = None
```

- [ ] **Step 5: Export runtime package**

Create `framework/runtime/__init__.py`:

```python
from __future__ import annotations

from .enums import (
    AgentKind,
    ApprovalDenyPolicy,
    ApprovalSubjectType,
    CancellationSource,
    ControlCommandKind,
    MessageDeltaSource,
    OperationKind,
    OperationStatus,
    SnapshotReason,
    StateScope,
    ToolBatchStatus,
    ToolCallStatus,
    TurnPhase,
)
from .models import (
    ApprovalRequestState,
    ApprovalTransaction,
    CancellationState,
    ControlCommandState,
    ControlMutation,
    JsonValue,
    MessageDelta,
    OperationState,
    ResumePoint,
    RuntimeErrorState,
    StateQueryScope,
    ToolArguments,
    ToolBatchState,
    ToolCallState,
    TurnIdentity,
    TurnSnapshot,
    TurnStateBase,
    TurnSummary,
)

__all__ = [
    "AgentKind",
    "ApprovalDenyPolicy",
    "ApprovalRequestState",
    "ApprovalSubjectType",
    "ApprovalTransaction",
    "CancellationSource",
    "CancellationState",
    "ControlCommandKind",
    "ControlCommandState",
    "ControlMutation",
    "JsonValue",
    "MessageDelta",
    "MessageDeltaSource",
    "OperationKind",
    "OperationState",
    "OperationStatus",
    "ResumePoint",
    "RuntimeErrorState",
    "SnapshotReason",
    "StateQueryScope",
    "StateScope",
    "ToolArguments",
    "ToolBatchState",
    "ToolBatchStatus",
    "ToolCallState",
    "ToolCallStatus",
    "TurnIdentity",
    "TurnPhase",
    "TurnSnapshot",
    "TurnStateBase",
    "TurnSummary",
]
```

- [ ] **Step 6: Run tests**

Run:

```bash
python -m pytest tests/unit/runtime/test_models.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add framework/runtime/__init__.py framework/runtime/enums.py framework/runtime/models.py tests/unit/runtime/test_models.py
git commit -m "feat: add runtime state models"
```

---

## Task 2: Runtime Services, Snapshot Policy, Codecs, and Stores

**Files:**
- Create: `framework/runtime/services.py`
- Create: `framework/runtime/policy.py`
- Create: `framework/runtime/codec.py`
- Create: `framework/runtime/store.py`
- Modify: `framework/runtime/__init__.py`
- Test: `tests/unit/runtime/test_codec.py`
- Test: `tests/unit/runtime/test_codec_registry.py`
- Test: `tests/unit/runtime/test_file_store.py`
- Test: `tests/unit/runtime/test_command_store.py`
- Test: `tests/unit/runtime/test_runtime_services.py`
- Test: `tests/unit/runtime/test_concurrent_turns.py`

- [ ] **Step 1: Write codec tests**

Add `tests/unit/runtime/test_codec.py`:

```python
from __future__ import annotations

import pytest

from framework.memory.core.message import ChatMessage
from framework.runtime.codec import RuntimeStateCodecConfig, RuntimeStateCodecError
from framework.runtime.enums import AgentKind, MessageDeltaSource, SnapshotReason, TurnPhase
from framework.runtime.models import MessageDelta, ResumePoint, TurnIdentity, TurnSnapshot
from framework.agents.react.state import ReActRuntimeStateCodec


def test_react_codec_round_trips_snapshot_payload() -> None:
    codec = ReActRuntimeStateCodec()
    identity = TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1")
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


def test_codec_rejects_large_provider_payload() -> None:
    codec = ReActRuntimeStateCodec(
        config=RuntimeStateCodecConfig(max_provider_payload_keys=1)
    )
    identity = TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1")
    snapshot = TurnSnapshot(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING),
        message_delta=[
            MessageDelta(
                message=ChatMessage(role="assistant", content="x"),
                source=MessageDeltaSource.ASSISTANT,
                provider_payload={"a": 1, "b": 2},
            )
        ],
        state_payload={"current_node": "llm", "iteration": 1},
    )

    with pytest.raises(RuntimeStateCodecError, match="provider_payload"):
        codec.encode_turn(snapshot)
```

Add `tests/unit/runtime/test_codec_registry.py`:

```python
from __future__ import annotations

import pytest

from framework.agents.react.state import ReActRuntimeStateCodec
from framework.runtime.codec import RuntimeStateCodecRegistry, UnsupportedAgentKindError
from framework.runtime.enums import AgentKind


def test_codec_registry_dispatches_by_agent_kind() -> None:
    react_codec = ReActRuntimeStateCodec()
    registry = RuntimeStateCodecRegistry({AgentKind.REACT: react_codec})

    assert registry.get(AgentKind.REACT) is react_codec


def test_codec_registry_rejects_missing_agent_kind() -> None:
    registry = RuntimeStateCodecRegistry({})

    with pytest.raises(UnsupportedAgentKindError, match="react"):
        registry.get(AgentKind.REACT)
```

- [ ] **Step 2: Write store tests**

Add `tests/unit/runtime/test_file_store.py`:

```python
from __future__ import annotations

from framework.agents.react.state import ReActRuntimeStateCodec
from framework.memory.core.message import ChatMessage
from framework.runtime.codec import RuntimeStateCodecRegistry
from framework.runtime.enums import AgentKind, MessageDeltaSource, SnapshotReason, TurnPhase
from framework.runtime.models import MessageDelta, ResumePoint, StateQueryScope, TurnIdentity, TurnSnapshot
from framework.runtime.store import JsonFileTurnStateStore


async def test_json_file_turn_store_save_load_delete(tmp_path) -> None:
    registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    store = JsonFileTurnStateStore(tmp_path, registry)
    identity = TurnIdentity(agent_id="bot", session_id="group/1", turn_id="t1")
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
        StateQueryScope(agent_id="bot", session_id="group/1", phase=TurnPhase.SUSPENDED)
    )

    assert loaded is not None
    assert loaded.identity == identity
    assert len(active) == 1

    await store.delete_turn(identity)
    assert await store.load_turn(identity) is None


async def test_json_file_turn_store_rejects_sanitized_path_collision(tmp_path) -> None:
    registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    store = JsonFileTurnStateStore(tmp_path, registry)

    first = TurnIdentity(agent_id="bot", session_id="a/b", turn_id="t1")
    second = TurnIdentity(agent_id="bot", session_id="a:b", turn_id="t1")

    snapshot = TurnSnapshot(
        identity=first,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING),
        message_delta=[],
        state_payload={"current_node": "llm", "iteration": 1},
    )
    await store.save_turn(snapshot)

    colliding = TurnSnapshot(
        identity=second,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING),
        message_delta=[],
        state_payload={"current_node": "llm", "iteration": 1},
    )

    await store.save_turn(colliding)
    assert await store.load_turn(first) is not None
    assert await store.load_turn(second) is not None
```

Add `tests/unit/runtime/test_command_store.py`:

```python
from __future__ import annotations

from framework.runtime.enums import ControlCommandKind, OperationStatus
from framework.runtime.models import ControlCommandState, StateQueryScope
from framework.runtime.store import InMemoryRuntimeCommandStore


async def test_command_store_lifecycle() -> None:
    store = InMemoryRuntimeCommandStore()
    command = ControlCommandState(
        command_id="cmd-1",
        kind=ControlCommandKind.CANCEL_TURN,
        agent_id="bot",
        session_id="s1",
        payload={"reason": "user"},
    )

    await store.save_command(command)
    pending = await store.load_pending_commands(StateQueryScope(agent_id="bot", session_id="s1"))

    assert [item.command_id for item in pending] == ["cmd-1"]

    await store.mark_command_applied("cmd-1")
    assert command.status is OperationStatus.COMPLETED
    assert await store.load_pending_commands(StateQueryScope(agent_id="bot", session_id="s1")) == []
```

- [ ] **Step 3: Write runtime service and concurrency tests**

Add `tests/unit/runtime/test_runtime_services.py`:

```python
from __future__ import annotations

from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices, require_runtime_state
from framework.runtime.store import NoOpRuntimeCommandStore, NoOpTurnStateStore


def test_runtime_services_are_not_part_of_turn_state() -> None:
    services = AgentRuntimeServices(
        turn_store=NoOpTurnStateStore(),
        command_store=NoOpRuntimeCommandStore(),
    )
    state = TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=services, state=state)

    assert runtime.services.turn_store is not None
    assert runtime.state.identity.turn_id == "t1"


def test_require_runtime_state_returns_expected_type() -> None:
    state = TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)

    assert require_runtime_state(runtime, TurnStateBase) is state
```

Add `tests/unit/runtime/test_concurrent_turns.py`:

```python
from __future__ import annotations

import pytest

from framework.agents.react.state import ReActRuntimeStateCodec
from framework.runtime.codec import RuntimeStateCodecRegistry
from framework.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from framework.runtime.models import ResumePoint, TurnIdentity, TurnSnapshot
from framework.runtime.store import ActiveTurnConflictError, JsonFileTurnStateStore


async def test_store_rejects_second_active_turn_for_same_agent_session(tmp_path) -> None:
    registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    store = JsonFileTurnStateStore(tmp_path, registry)

    first = TurnSnapshot(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        reason=SnapshotReason.LLM_COMPLETED,
        resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING),
        message_delta=[],
        state_payload={"current_node": "llm", "iteration": 1},
    )
    second = TurnSnapshot(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t2"),
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
```

- [ ] **Step 4: Run tests and verify failure**

Run:

```bash
python -m pytest tests/unit/runtime/test_codec.py tests/unit/runtime/test_codec_registry.py tests/unit/runtime/test_file_store.py tests/unit/runtime/test_command_store.py tests/unit/runtime/test_runtime_services.py tests/unit/runtime/test_concurrent_turns.py -v
```

Expected: fail because codec, services, store, and ReAct codec do not exist.

- [ ] **Step 5: Implement `framework/runtime/services.py`**

Implement:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeVar

from framework.core.llm_error import RuntimeSafetyPolicy

from .models import TurnStateBase

TState = TypeVar("TState", bound=TurnStateBase)


@dataclass
class AgentRuntimeServices:
    hooks: object | None = None
    interceptors: object | None = None
    control: object | None = None
    approval: object | None = None
    governance: object | None = None
    turn_store: object | None = None
    command_store: object | None = None
    pending_input_queue: object | None = None
    safety: RuntimeSafetyPolicy = field(default_factory=RuntimeSafetyPolicy)


@dataclass
class AgentRuntime:
    services: AgentRuntimeServices
    state: TurnStateBase


def require_runtime_state(runtime: AgentRuntime, state_type: type[TState]) -> TState:
    if isinstance(runtime.state, state_type):
        return runtime.state
    raise TypeError(
        f"runtime state must be {state_type.__name__}, got {type(runtime.state).__name__}"
    )
```

- [ ] **Step 6: Implement policy, codec, and store**

Implement `framework/runtime/policy.py`, `framework/runtime/codec.py`, and `framework/runtime/store.py` using the test APIs above. Requirements:

- `RuntimeStateCodecRegistry.get()` raises `UnsupportedAgentKindError`.
- `RuntimeStateCodecConfig.max_provider_payload_keys` defaults to `10`.
- `JsonFileTurnStateStore` uses nested directories and a collision-safe sanitized segment. Prefer `<safe>--<8-char sha256>` for all segments so collisions are impossible and deterministic.
- Active turn conflict applies to phases `RUNNING` and `SUSPENDED`.
- `NoOpTurnStateStore.load_turn()` returns `None`; `list_active_turns()` returns `[]`.
- `InMemoryRuntimeCommandStore.mark_command_applied()` updates `OperationStatus.COMPLETED` and `applied_at`.

- [ ] **Step 7: Export runtime services and stores**

Update `framework/runtime/__init__.py` to export `AgentRuntime`, `AgentRuntimeServices`, codec types, policy types, and store types.

- [ ] **Step 8: Run tests**

Run:

```bash
python -m pytest tests/unit/runtime -v
```

Expected: runtime tests pass.

- [ ] **Step 9: Commit**

```bash
git add framework/runtime tests/unit/runtime
git commit -m "feat: add runtime state stores and codecs"
```

---

## Task 3: ReAct Typed Turn State and Codec

**Files:**
- Modify: `framework/agents/react/state.py`
- Modify: `framework/agents/react/__init__.py`
- Test: `tests/unit/agents/react/test_turn_state.py`
- Test: `tests/unit/runtime/test_codec.py`

- [ ] **Step 1: Write ReAct state tests**

Add `tests/unit/agents/react/test_turn_state.py`:

```python
from __future__ import annotations

from framework.agents.react.constants import ReActNode
from framework.agents.react.state import ReActSnapshotPolicy, ReActTurnState
from framework.runtime.enums import AgentKind, OperationKind, OperationStatus, SnapshotReason, ToolBatchStatus, TurnPhase
from framework.runtime.models import ToolArguments, ToolCallState, TurnIdentity


def test_react_turn_state_creates_operation_for_tool_batch() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    batch = state.create_tool_batch(
        iteration=1,
        calls=[
            ToolCallState(
                call_id="call-1",
                tool_name="read_file",
                arguments=ToolArguments(values={"path": "README.md"}),
            )
        ],
    )

    assert batch.status is ToolBatchStatus.CREATED
    assert batch.operation_id is not None
    assert state.tool_batches == [batch]
    assert state.operations[0].kind is OperationKind.TOOL_BATCH
    assert state.operations[0].subject_id == batch.batch_id


def test_react_snapshot_policy_captures_minimal_resume_point() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.SUSPENDED,
        current_node=ReActNode.TOOL,
        iteration=2,
    )
    snapshot = ReActSnapshotPolicy().capture(
        state,
        SnapshotReason.TOOL_APPROVAL_REQUIRED,
    )

    assert snapshot.resume_point.agent_kind is AgentKind.REACT
    assert snapshot.resume_point.phase is TurnPhase.SUSPENDED
    assert snapshot.state_payload["current_node"] == ReActNode.TOOL.value
    assert snapshot.state_payload["iteration"] == 2


def test_react_operation_update_marks_batch_completed() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    batch = state.create_tool_batch(iteration=1, calls=[])
    state.update_operation(batch.operation_id, OperationStatus.COMPLETED)

    assert state.operations[0].status is OperationStatus.COMPLETED
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m pytest tests/unit/agents/react/test_turn_state.py -v
```

Expected: fail because `ReActTurnState` and helpers are not implemented.

- [ ] **Step 3: Replace `framework/agents/react/state.py`**

Delete old resume-store classes in this file and implement:

- `ReActTurnState(TurnStateBase)`
- `create_tool_batch()`
- `active_tool_batch()`
- `completed_tool_calls()`
- `ReActSnapshotPolicy`
- `ReActRuntimeStateCodec`

The implementation must import runtime models from `framework.runtime`. It must not import `framework.control.checkpoint`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m pytest tests/unit/agents/react/test_turn_state.py tests/unit/runtime/test_codec.py tests/unit/runtime/test_codec_registry.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/state.py framework/agents/react/__init__.py tests/unit/agents/react/test_turn_state.py tests/unit/runtime/test_codec.py tests/unit/runtime/test_codec_registry.py
git commit -m "feat: add react turn state snapshots"
```

---

## Task 4: Core AgentContext and Runtime Service Wiring

**Files:**
- Modify: `framework/core/agent.py`
- Modify: `framework/agents/react/runtime.py`
- Modify: `framework/agents/react/assembler.py`
- Modify: `framework/agents/react/builder.py`
- Test: `tests/unit/core/test_agent_context.py`
- Test: `tests/unit/runtime/test_clean_mode.py`
- Test: `tests/unit/agents/react/test_runtime.py`
- Test: `tests/unit/agents/react/test_assembler.py`

- [ ] **Step 1: Write context/runtime tests**

Add `tests/unit/runtime/test_clean_mode.py`:

```python
from __future__ import annotations

from framework.agents.react.state import ReActTurnState
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
from framework.runtime.store import NoOpTurnStateStore


def test_clean_mode_still_has_typed_react_state() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    runtime = AgentRuntime(
        services=AgentRuntimeServices(turn_store=NoOpTurnStateStore()),
        state=state,
    )

    assert isinstance(runtime.state, ReActTurnState)
    assert runtime.services.turn_store is not None
```

Update `tests/unit/core/test_agent_context.py` with:

```python
from __future__ import annotations

from framework.core.agent import AgentContext
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


def test_agent_context_uses_identity_and_runtime_without_metadata_bag() -> None:
    identity = TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1")
    state = TurnStateBase(identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.CREATED)
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    ctx = AgentContext(
        identity=identity,
        system_prompt="system",
        history=None,
        tool_manager=None,
        runtime=runtime,
    )

    assert ctx.identity == identity
    assert ctx.runtime.state is state
    assert not hasattr(ctx, "metadata")
    assert not hasattr(ctx, "extensions")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m pytest tests/unit/core/test_agent_context.py tests/unit/runtime/test_clean_mode.py -v
```

Expected: fail while `AgentContext` still has generic runtime, `metadata`, and `extensions`.

- [ ] **Step 3: Update `AgentContext`**

Modify `framework/core/agent.py`:

- Remove `Generic[R]`, `R`, `extensions`, `metadata`, and loose `runtime: R | None`.
- Add `identity: TurnIdentity`.
- Add `runtime: AgentRuntime`.
- Keep `session_id` only if existing adapters still need it during migration; if kept temporarily, make it a property that returns `identity.session_id`.
- Keep `to_messages()` and `get_tool_descriptions()`.

Implementation shape:

```python
@dataclass
class AgentContext:
    identity: TurnIdentity
    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager
    runtime: AgentRuntime
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)
    emitter: ContentEmitter | None = None

    @property
    def session_id(self) -> str:
        return self.identity.session_id
```

- [ ] **Step 4: Replace `ctx_ext` usage**

Search:

```bash
rg "ctx_ext|ctx\\.extensions|ctx\\.metadata|context\\.metadata|context\\.extensions" framework tests examples
```

For this task, update only construction and service wiring call sites needed to make core context tests pass. Do not leave runtime state in metadata.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python -m pytest tests/unit/core/test_agent_context.py tests/unit/runtime/test_clean_mode.py tests/unit/agents/react/test_runtime.py tests/unit/agents/react/test_assembler.py -v
```

Expected: pass or fail only on later ReAct node metadata migrations. If failures reference removed metadata keys in ReAct nodes, leave those for Task 5.

- [ ] **Step 6: Commit**

```bash
git add framework/core/agent.py framework/agents/react/runtime.py framework/agents/react/assembler.py framework/agents/react/builder.py tests/unit/core/test_agent_context.py tests/unit/runtime/test_clean_mode.py tests/unit/agents/react/test_runtime.py tests/unit/agents/react/test_assembler.py
git commit -m "refactor: wire typed agent runtime context"
```

---

## Task 5: ReAct Nodes Use Turn State Instead of Metadata

**Files:**
- Modify: `framework/agents/react/agent.py`
- Modify: `framework/agents/react/constants.py`
- Modify: `framework/agents/react/nodes/start.py`
- Modify: `framework/agents/react/nodes/llm.py`
- Modify: `framework/agents/react/nodes/tool.py`
- Modify: `framework/agents/react/nodes/end.py`
- Test: `tests/unit/agents/react/test_nodes.py`
- Test: `tests/unit/agents/react/test_agent.py`
- Test: `tests/unit/agents/react/test_turn_state.py`

- [ ] **Step 1: Write metadata cleanup regression test**

Add to `tests/unit/agents/react/test_turn_state.py`:

```python
def test_react_state_replaces_iteration_metadata() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )

    state.iteration = 3
    state.current_node = ReActNode.LLM

    assert state.iteration == 3
    assert state.current_node is ReActNode.LLM
```

- [ ] **Step 2: Run ReAct node tests and capture current failures**

Run:

```bash
python -m pytest tests/unit/agents/react/test_nodes.py tests/unit/agents/react/test_agent.py tests/unit/agents/react/test_turn_state.py -v
```

Expected: failures mention metadata usage, old `ReActRuntime`, old checkpoints, or missing typed context fields.

- [ ] **Step 3: Update ReAct state access pattern**

Add a helper in `framework/agents/react/state.py`:

```python
def require_react_state(ctx: AgentContext) -> ReActTurnState:
    state = ctx.runtime.state
    if isinstance(state, ReActTurnState):
        return state
    raise TypeError(f"ReAct requires ReActTurnState, got {type(state).__name__}")
```

Use this helper in ReAct agent and nodes.

- [ ] **Step 4: Update StartNode**

Make `StartNode.execute()` set:

- `state.phase = TurnPhase.RUNNING`
- `state.current_node = ReActNode.START`
- `state.iteration = 0`

Do not write `_iteration`, `ITERATION`, or `CURRENT_NODE` into metadata.

- [ ] **Step 5: Update LLMNode**

Make `LLMNode.execute()`:

- read and increment `state.iteration`,
- set `state.current_node = ReActNode.LLM`,
- store `state.llm_response`,
- append assistant `MessageDelta`,
- create an `OperationState` with `OperationKind.LLM_CALL`,
- use `ctx.runtime.services.governance` for model-visible governance,
- avoid writing `ReActMetaKey.LLM_RESPONSE` and `ReActMetaKey.ITERATION_MSGS`.

- [ ] **Step 6: Update ToolNode**

Make `ToolNode.execute()`:

- read `state.llm_response`,
- create `ToolBatchState` through `state.create_tool_batch()`,
- classify each tool into `ToolCallState.decision`,
- if any pending approval exists, create `ApprovalTransaction`, set `state.approval`, mark phase suspended, snapshot through `ctx.runtime.services.turn_store`, and raise or return the existing interrupt signal,
- if no pending approval exists, execute the batch,
- append tool `MessageDelta` entries,
- snapshot with `SnapshotReason.TOOL_BATCH_PROGRESS` after each tool call when a store exists.

- [ ] **Step 7: Update EndNode**

Make `EndNode.execute()`:

- set `state.phase = TurnPhase.COMPLETING`,
- build `AgentResult` from `state.message_delta`,
- commit `MessageDelta.message` values to memory/history only once,
- call `turn_store.delete_turn(state.identity)` on successful completion,
- set `state.phase = TurnPhase.COMPLETED`.

- [ ] **Step 8: Remove ReAct runtime checkpoint helpers**

Delete `_save_checkpoint()`, `_save_denial_checkpoint()`, and `_clear_checkpoint()` from `framework/agents/react/agent.py` after no callers remain.

- [ ] **Step 9: Run focused tests**

Run:

```bash
python -m pytest tests/unit/agents/react/test_nodes.py tests/unit/agents/react/test_agent.py tests/unit/agents/react/test_turn_state.py -v
```

Expected: pass.

- [ ] **Step 10: Commit**

```bash
git add framework/agents/react tests/unit/agents/react
git commit -m "refactor: move react execution state into turn state"
```

---

## Task 6: Approval Transaction Suspend and Resume

**Files:**
- Modify: `framework/agents/react/approval.py`
- Modify: `framework/agents/react/nodes/tool.py`
- Modify: `framework/pipeline/approval_renderer.py`
- Modify: `framework/pipeline/pipeline.py`
- Modify: `framework/approval/types.py`
- Delete later: `framework/approval/state.py`
- Delete later: `framework/approval/store.py`
- Test: `tests/unit/approval/test_transaction_state.py`
- Test: `tests/unit/approval/test_batch_atomicity.py`
- Test: `tests/unit/pipeline/test_runtime_resume.py`
- Test: `tests/unit/pipeline/test_approval_renderer_edge.py`

- [ ] **Step 1: Write transaction tests**

Create `tests/unit/approval/test_transaction_state.py`:

```python
from __future__ import annotations

from framework.approval.constants import ApprovalDecision, ApprovalStatus, ApprovalTier
from framework.runtime.enums import ApprovalSubjectType
from framework.runtime.models import ApprovalRequestState, ApprovalTransaction, ToolArguments


def test_denial_preempts_unresolved_requests() -> None:
    tx = ApprovalTransaction(
        approval_id="ap-1",
        turn_id="t1",
        subject_type=ApprovalSubjectType.TOOL_BATCH,
        subject_ids=["batch-1"],
        requests=[
            ApprovalRequestState(
                request_id="r1",
                approval_id="ap-1",
                tool_call_id="call-1",
                tool_name="write_file",
                arguments=ToolArguments(values={"path": "a.txt"}),
                tier=ApprovalTier.DANGEROUS,
                iteration=1,
            ),
            ApprovalRequestState(
                request_id="r2",
                approval_id="ap-1",
                tool_call_id="call-2",
                tool_name="delete_file",
                arguments=ToolArguments(values={"path": "b.txt"}),
                tier=ApprovalTier.DANGEROUS,
                iteration=1,
            ),
        ],
    )

    tx.apply_decision("call-1", ApprovalDecision.DENIED, reason="not allowed")

    assert tx.status is ApprovalStatus.DENIED
    assert tx.decisions["call-1"] == ApprovalDecision.DENIED
    assert tx.decisions["call-2"] == ApprovalDecision.PREEMPTED
    assert tx.deny_reason == "not allowed"
```

If `ApprovalTransaction.apply_decision()` is not in Task 1, add it now to `framework/runtime/models.py`.

- [ ] **Step 2: Write pipeline resume test**

Add to `tests/unit/pipeline/test_runtime_resume.py`:

```python
from __future__ import annotations

from framework.approval.constants import ApprovalDecision
from framework.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from framework.runtime.models import ResumePoint, StateQueryScope, TurnIdentity, TurnSnapshot


async def test_pipeline_loads_suspended_turn_for_approval_command(fake_pipeline, turn_store) -> None:
    identity = TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1")
    await turn_store.save_turn(
        TurnSnapshot(
            identity=identity,
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
            resume_point=ResumePoint(agent_kind=AgentKind.REACT, phase=TurnPhase.SUSPENDED),
            message_delta=[],
            state_payload={
                "current_node": "tool",
                "iteration": 1,
                "approval": {
                    "approval_id": "ap-1",
                    "subject_ids": ["batch-1"],
                    "requests": [],
                    "decisions": {},
                    "status": "pending",
                },
            },
        )
    )

    loaded = await turn_store.list_active_turns(
        StateQueryScope(agent_id="bot", session_id="s1", phase=TurnPhase.SUSPENDED)
    )

    assert loaded[0].identity == identity
```

Use the project’s existing pipeline fixtures if available. If no fixture exists, create local fake pipeline/store fixtures in this test file with concrete classes.

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m pytest tests/unit/approval/test_transaction_state.py tests/unit/approval/test_batch_atomicity.py tests/unit/pipeline/test_runtime_resume.py -v
```

Expected: fail while approval still depends on `ApprovalState`, `ApprovalStateStore`, `TurnResumeState`, and `suspend_strategy`.

- [ ] **Step 4: Implement `ApprovalTransaction.apply_decision()`**

Rules:

- `ALLOWED` updates one request.
- `DENIED` marks that call denied and unresolved calls preempted.
- all decided and no denial sets `ApprovalStatus.APPROVED`.
- some decided sets `ApprovalStatus.PARTIAL`.
- decisions stay keyed by `tool_call_id`.

- [ ] **Step 5: Replace `ApprovalRuntime`**

Modify `framework/agents/react/approval.py`:

- keep `ApprovalClassifier`,
- keep `TieredToolApprovalClassifier`,
- make `classify()` return `ApprovalTier` enum values or existing `ApprovalTier` constants consistently,
- define `ApprovalRuntime(classifier, default_deny_policy)`,
- remove `suspend_strategy`,
- remove `deny_as_cancel`.

- [ ] **Step 6: Replace approval suspend in ToolNode**

ToolNode must:

- create transaction in `ReActTurnState`,
- save a `TurnSnapshot` through `turn_store`,
- never write an approval file,
- never write a turn-resume file,
- suspend before executing any newly generated call when at least one pending approval exists.

- [ ] **Step 7: Replace approval resume in Pipeline**

Pipeline must:

- parse approval command using existing `parse_approval_action()`,
- query `turn_store.list_active_turns(StateQueryScope(session_id=input_msg.session_id, phase=TurnPhase.SUSPENDED))`,
- load the matching snapshot,
- decode ReAct state through codec registry,
- apply approval decision,
- save partial snapshots,
- resume complete approval from ReAct tool node.

- [ ] **Step 8: Crash-before-render behavior**

If a non-approval input arrives while a suspended approval exists:

- do not auto-deny,
- re-render the approval prompt from `ApprovalTransaction`,
- queue or ignore unrelated input according to `BusyInputMode`, but do not mutate approval decisions.

- [ ] **Step 9: Run approval tests**

Run:

```bash
python -m pytest tests/unit/approval tests/unit/pipeline/test_runtime_resume.py tests/unit/pipeline/test_approval_renderer_edge.py -v
```

Expected: approval and pipeline tests pass or fail only where old tests assert deleted stores. Update old tests to assert new behavior instead of preserving old APIs.

- [ ] **Step 10: Commit**

```bash
git add framework/agents/react/approval.py framework/agents/react/nodes/tool.py framework/pipeline/approval_renderer.py framework/pipeline/pipeline.py framework/approval/types.py tests/unit/approval tests/unit/pipeline/test_runtime_resume.py tests/unit/pipeline/test_approval_renderer_edge.py
git commit -m "refactor: manage approval as turn transaction"
```

---

## Task 7: Pipeline, AgentSession, DreamEngine, and Memory Commit Boundaries

**Files:**
- Modify: `framework/pipeline/pipeline.py`
- Modify: `framework/pipeline/context_assembler.py`
- Modify: `framework/session/agent_session.py`
- Modify: `framework/memory/consolidation/dream_engine.py` only if it reads uncommitted runtime state
- Test: `tests/unit/runtime/test_turn_lifecycle.py`
- Test: `tests/unit/pipeline/test_pipeline_cleanup.py`
- Test: `tests/unit/pipeline/test_pipeline_interrupt.py`
- Test: `tests/unit/session/test_agent_session.py`
- Test: `tests/unit/memory/consolidation/test_dream_engine_registry.py`

- [ ] **Step 1: Write turn lifecycle tests**

Create `tests/unit/runtime/test_turn_lifecycle.py`:

```python
from __future__ import annotations

from framework.agents.react.state import ReActTurnState
from framework.runtime.enums import AgentKind, MessageDeltaSource, TurnPhase
from framework.runtime.models import MessageDelta, TurnIdentity
from framework.memory.core.message import ChatMessage


def test_message_delta_is_current_turn_only() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    state.message_delta.append(
        MessageDelta(
            message=ChatMessage(role="assistant", content="new message"),
            source=MessageDeltaSource.ASSISTANT,
        )
    )

    assert [delta.message.content for delta in state.message_delta] == ["new message"]


def test_completed_turn_can_clear_message_delta_after_commit() -> None:
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.COMPLETING,
    )
    state.mark_completed()

    assert state.phase is TurnPhase.COMPLETED
```

- [ ] **Step 2: Run current tests**

Run:

```bash
python -m pytest tests/unit/runtime/test_turn_lifecycle.py tests/unit/pipeline/test_pipeline_cleanup.py tests/unit/session/test_agent_session.py -v
```

Expected: fail until Pipeline and AgentSession create typed runtime state.

- [ ] **Step 3: Generate identity in Pipeline**

In `AgentPipeline._process_message_locked()` or the equivalent per-message boundary:

```python
identity = TurnIdentity(
    agent_id=getattr(self.agent, "name", "agent"),
    session_id=input_msg.session_id,
    turn_id=uuid.uuid4().hex,
    conversation_id=input_msg.session_id,
)
```

Do not construct checkpoint IDs.

- [ ] **Step 4: Create runtime services in Pipeline**

Pipeline should create:

```python
services = AgentRuntimeServices(
    hooks=self.hook_runner,
    interceptors=self.interceptor_chain,
    control=self._control_runtime,
    approval=self._approval_runtime,
    governance=self.governance,
    turn_store=self.turn_store,
    command_store=self.command_store,
    pending_input_queue=self._injection_queues[input_msg.session_id],
    safety=self.safety,
)
runtime = AgentRuntime(services=services, state=react_state)
```

Use actual field names introduced in earlier tasks.

- [ ] **Step 5: Enforce one active turn**

Before starting a new normal input, query suspended/running snapshots. Apply `BusyInputMode`:

- `QUEUE`: queue new input when a turn is active.
- `INTERRUPT`: create a `ControlCommandState` cancellation command.
- `REJECT`: send a busy output message.

Do not mutate approval state for unrelated input.

- [ ] **Step 6: Update AgentSession**

Make `AgentSession` create the same `TurnIdentity`, `AgentRuntimeServices`, and `ReActTurnState` as Pipeline. Clean mode uses `NoOpTurnStateStore`, not missing state.

- [ ] **Step 7: Confirm DreamEngine isolation**

Search:

```bash
rg "message_delta|TurnState|runtime\\.state" framework/memory framework/session framework/pipeline
```

DreamEngine may read memory layers. It must not read `runtime.state.message_delta`. If it does, replace that read with committed memory access through existing memory APIs.

- [ ] **Step 8: Run lifecycle tests**

Run:

```bash
python -m pytest tests/unit/runtime/test_turn_lifecycle.py tests/unit/pipeline/test_pipeline_cleanup.py tests/unit/pipeline/test_pipeline_interrupt.py tests/unit/session/test_agent_session.py tests/unit/memory/consolidation/test_dream_engine_registry.py -v
```

Expected: pass.

- [ ] **Step 9: Commit**

```bash
git add framework/pipeline framework/session framework/memory/consolidation tests/unit/runtime/test_turn_lifecycle.py tests/unit/pipeline tests/unit/session tests/unit/memory/consolidation
git commit -m "refactor: create typed runtime per turn"
```

---

## Task 8: Hook, Interceptor, and Control Runtime Migration

**Files:**
- Modify: `framework/interceptor/abc.py`
- Modify: `framework/interceptor/chain.py`
- Modify: `framework/interceptor/builtin/*.py`
- Modify: `framework/hook/abc.py`
- Modify: `framework/hook/runner.py`
- Modify: `framework/hook/builtin/*.py`
- Modify: `framework/control/runtime.py`
- Modify: `framework/control/types.py`
- Modify: `framework/control/store.py`
- Test: `tests/unit/interceptor/test_runtime_state_interceptors.py`
- Test: `tests/unit/hook/test_runtime_state_hooks.py`
- Test: `tests/unit/control/test_control_runtime.py`
- Test: `tests/unit/test_interceptor_chain.py`
- Test: `tests/unit/test_hooks.py`

- [ ] **Step 1: Write interceptor runtime-state tests**

Create `tests/unit/interceptor/test_runtime_state_interceptors.py`:

```python
from __future__ import annotations

from framework.interceptor.abc import IterationContext, LLMCallContext, LLMRequest, LLMStreamContext, ToolCallContext, TurnContext
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import ToolArguments, TurnIdentity, TurnStateBase


def _state() -> TurnStateBase:
    return TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )


def test_all_interceptor_contexts_carry_turn_state() -> None:
    state = _state()
    request = LLMRequest(messages=[], model="fake", stream=False)

    assert TurnContext(turn_state=state, prompt="p", turn_id="t1", max_iterations=3).turn_state is state
    assert IterationContext(turn_state=state, iteration=1).turn_state is state
    assert LLMCallContext(turn_state=state, request=request).turn_state is state
    assert LLMStreamContext(turn_state=state, request=request).turn_state is state
    assert ToolCallContext(turn_state=state, tool_name="read", arguments=ToolArguments(values={})).turn_state is state
```

- [ ] **Step 2: Write hook runtime-state test**

Create `tests/unit/hook/test_runtime_state_hooks.py`:

```python
from __future__ import annotations

from framework.hook.runner import HookRunner
from framework.runtime.enums import AgentKind, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices


async def test_hook_receives_context_with_runtime_state(agent_context_factory) -> None:
    seen = {}

    async def hook(ctx, payload=None):
        seen["turn_id"] = ctx.runtime.state.identity.turn_id

    state = TurnStateBase(
        identity=TurnIdentity(agent_id="bot", session_id="s1", turn_id="t1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    ctx = agent_context_factory(runtime=AgentRuntime(services=AgentRuntimeServices(), state=state))
    runner = HookRunner([hook])

    await runner.dispatch("before_turn", ctx)

    assert seen == {"turn_id": "t1"}
```

If `agent_context_factory` does not exist, add a local fixture in this test file using the current `AgentContext` constructor.

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m pytest tests/unit/interceptor/test_runtime_state_interceptors.py tests/unit/hook/test_runtime_state_hooks.py tests/unit/control/test_control_runtime.py -v
```

Expected: fail until context models and control runtime are migrated.

- [ ] **Step 4: Update interceptor ABCs and chain**

Add `turn_state` to all five context dataclasses. Replace `messages: Sequence[dict[str, Any]]` with `LLMRequest` where practical. Keep provider conversion at the edge.

- [ ] **Step 5: Update builtin interceptors**

For each builtin interceptor:

- `control_drain.py`: emit or handle `ControlMutation`; do not write cancellation flags to metadata.
- `tool_approval.py`: remove final state ownership and rely on ReAct `ApprovalTransaction`.
- `steer_inject.py`: use `AgentRuntimeServices.pending_input_queue`.
- `llm_stream_watch.py`: use `LLMStreamContext.turn_state`.
- `turn_timeout.py`: write `CancellationState` through a returned mutation or runtime state helper.

- [ ] **Step 6: Update hooks**

For each builtin hook:

- `runtime_context.py`: remove or replace with typed state/session model.
- `peer_auto_send.py`: read completed tool calls from `ReActTurnState.completed_tool_calls()`.
- `subagent_cleanup.py`: keep memory cleanup behavior, avoid runtime metadata.
- `dynamic_tool_filter.py`: use typed runtime services or explicit hook payload.

- [ ] **Step 7: Update control runtime**

`ControlRuntime` should:

- keep channel drain behavior,
- persist offline commands through `RuntimeCommandStore`,
- return `ControlMutation` from handlers,
- apply mutations to `TurnStateBase`.

- [ ] **Step 8: Run tests**

Run:

```bash
python -m pytest tests/unit/interceptor/test_runtime_state_interceptors.py tests/unit/hook/test_runtime_state_hooks.py tests/unit/control/test_control_runtime.py tests/unit/test_interceptor_chain.py tests/unit/test_hooks.py -v
```

Expected: pass.

- [ ] **Step 9: Commit**

```bash
git add framework/interceptor framework/hook framework/control tests/unit/interceptor tests/unit/hook tests/unit/control tests/unit/test_interceptor_chain.py tests/unit/test_hooks.py
git commit -m "refactor: expose typed turn state to hooks and interceptors"
```

---

## Task 9: Bot Project Runtime Wiring

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `examples/bot_project/tests/test_approval_flow.py`
- Modify: `examples/bot_project/tests/test_runtime_defaults.py`
- Modify: `tests/unit/bot_project/test_bot_project_runtime_wiring.py`
- Modify: `examples/bot_project/docs/memory-system.md`

- [ ] **Step 1: Write bot runtime wiring test**

Update `tests/unit/bot_project/test_bot_project_runtime_wiring.py`:

```python
from __future__ import annotations

from framework.runtime.store import JsonFileRuntimeCommandStore, JsonFileTurnStateStore


def test_bot_project_uses_runtime_turn_store(bot_service) -> None:
    assert isinstance(bot_service.pipeline.turn_store, JsonFileTurnStateStore)
    assert isinstance(bot_service.pipeline.command_store, JsonFileRuntimeCommandStore)
    assert not hasattr(bot_service.pipeline, "_approval_workspace")
```

Use the existing bot service fixture. If the fixture name differs, inspect the current test file and use the existing factory.

- [ ] **Step 2: Run bot tests and verify failure**

Run:

```bash
PYTHONPATH=. python -m pytest examples/bot_project/tests/test_approval_flow.py examples/bot_project/tests/test_runtime_defaults.py tests/unit/bot_project/test_bot_project_runtime_wiring.py -v
```

Expected: fail because bot still uses approval workspace or old checkpoint store.

- [ ] **Step 3: Wire bot stores**

In `examples/bot_project/bot/service/core.py`:

- create `RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})`,
- create `JsonFileTurnStateStore(data_dir / "runtime_state", registry)`,
- create `JsonFileRuntimeCommandStore(data_dir / "runtime_commands")`,
- pass both into Pipeline or runtime services,
- remove `approval_workspace` and old approval-store arguments.

- [ ] **Step 4: Update bot approval test assertions**

In `examples/bot_project/tests/test_approval_flow.py`, assert:

- a suspended approval creates one turn snapshot,
- snapshot payload has approval/tool batch data,
- snapshot does not contain full session history,
- approval completion deletes snapshot.

- [ ] **Step 5: Update docs**

In `examples/bot_project/docs/memory-system.md`, add a short section:

```markdown
## Runtime State vs Memory

Runtime state stores in-progress turn snapshots for suspend/resume and crash recovery.
Conversation memory stores committed conversation messages and remains the only input to compression and governance persistence.
Approval transactions live in runtime state while a turn is suspended and are deleted with the turn snapshot after completion.
```

- [ ] **Step 6: Run bot tests**

Run:

```bash
PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v
python -m pytest tests/unit/bot_project -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add examples/bot_project tests/unit/bot_project
git commit -m "refactor: wire bot project to runtime turn store"
```

---

## Task 10: Full Legacy Cleanup

**Files:**
- Delete: `framework/control/checkpoint.py`
- Delete: `framework/approval/store.py`
- Delete: `framework/approval/state.py`
- Delete or rewrite: tests that only validate old deleted APIs
- Modify: imports across `framework/`, `tests/`, and `examples/`
- Modify: `framework/approval/__init__.py`
- Modify: `framework/control/__init__.py`
- Modify: `framework/agents/react/constants.py`
- Test: `tests/unit/runtime/test_legacy_cleanup.py`

- [ ] **Step 1: Write cleanup test**

Create `tests/unit/runtime/test_legacy_cleanup.py`:

```python
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_legacy_runtime_state_modules_are_removed() -> None:
    assert not (ROOT / "framework" / "control" / "checkpoint.py").exists()
    assert not (ROOT / "framework" / "approval" / "store.py").exists()
    assert not (ROOT / "framework" / "approval" / "state.py").exists()


def test_no_legacy_runtime_state_symbols_remain() -> None:
    forbidden = [
        "TurnResumeState",
        "TurnResumeStateStore",
        "ApprovalStateStore",
        "LocalFileApprovalStateStore",
        "InMemoryApprovalStateStore",
        "StateStoreTurnResumeStateStore",
        "_current_resume",
        "checkpoint_store",
        "suspend_strategy",
        "deny_as_cancel",
        "ctx.metadata",
        "ctx.extensions",
        "context.metadata",
        "context.extensions",
    ]
    searched_roots = [ROOT / "framework", ROOT / "examples" / "bot_project"]
    offenders: list[str] = []
    for base in searched_roots:
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)} contains {needle}")

    assert offenders == []
```

- [ ] **Step 2: Run cleanup test and verify failure**

Run:

```bash
python -m pytest tests/unit/runtime/test_legacy_cleanup.py -v
```

Expected: fail while old modules and symbols still exist.

- [ ] **Step 3: Remove old modules**

Delete:

- `framework/control/checkpoint.py`
- `framework/approval/store.py`
- `framework/approval/state.py`

Use `rg` to find imports and replace them with runtime models/stores:

```bash
rg "control\\.checkpoint|approval\\.store|approval\\.state|TurnResumeState|ApprovalStateStore|checkpoint_store|suspend_strategy|deny_as_cancel|_current_resume" framework tests examples
```

- [ ] **Step 4: Remove old tests or rewrite assertions**

Rewrite old tests that asserted old APIs into new behavior tests. Delete tests only when their sole purpose is old API compatibility.

Expected old-test dispositions:

- `tests/unit/control/test_checkpoint_save.py`: delete or replace with `tests/unit/runtime/test_file_store.py`.
- `tests/unit/test_checkpoint_v2.py`: delete or replace with runtime snapshot tests.
- `tests/unit/approval/test_store.py`: delete; store no longer exists.
- `tests/unit/approval/test_state.py`: rewrite to `ApprovalTransaction`.
- `tests/unit/agents/react/test_strategy_storage.py`: delete old strategy-storage assertions.
- `tests/unit/agents/react/test_tool_node_no_strategy.py`: rewrite to assert no approval service means all calls execute normally.

- [ ] **Step 5: Remove metadata and extension runtime keys**

Delete from `framework/agents/react/constants.py` any enum members that only exist for runtime state. Keep only real graph node/reason enums and event constants.

- [ ] **Step 6: Run cleanup scans**

Run:

```bash
rg "TurnResumeState|ApprovalStateStore|LocalFileApprovalStateStore|InMemoryApprovalStateStore|StateStoreTurnResumeStateStore|_current_resume|checkpoint_store|suspend_strategy|deny_as_cancel|ctx\\.metadata|ctx\\.extensions|context\\.metadata|context\\.extensions" framework tests examples
```

Expected: no output. If output is in docs describing old removed behavior, move that text to historical notes outside implementation docs or remove it.

- [ ] **Step 7: Run cleanup test**

Run:

```bash
python -m pytest tests/unit/runtime/test_legacy_cleanup.py -v
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add -A framework tests examples
git commit -m "refactor: remove legacy runtime state paths"
```

---

## Task 11: Verification Matrix and Combination Scenarios

**Files:**
- Create: `tests/integration/test_runtime_state_governance.py`
- Create or update: `examples/bot_project/tests/test_runtime_state_governance_e2e.py`
- Modify tests as needed when failures expose real integration defects.

- [ ] **Step 1: Add integration scenario tests**

Create `tests/integration/test_runtime_state_governance.py` with scenarios that exercise multiple components together:

```python
from __future__ import annotations

from framework.runtime.enums import TurnPhase
from framework.runtime.models import StateQueryScope


async def test_react_approval_resume_does_not_copy_session_history(runtime_pipeline_factory) -> None:
    pipeline, turn_store = runtime_pipeline_factory()

    await pipeline.process_text("s1", "please write a file")
    active = await turn_store.list_active_turns(
        StateQueryScope(session_id="s1", phase=TurnPhase.SUSPENDED)
    )

    assert len(active) == 1
    assert active[0].message_delta != []
    assert "full_history" not in active[0].state_payload


async def test_unrelated_input_during_suspended_approval_rerenders_prompt(runtime_pipeline_factory) -> None:
    pipeline, turn_store = runtime_pipeline_factory()

    await pipeline.process_text("s1", "please write a file")
    before = await turn_store.list_active_turns(
        StateQueryScope(session_id="s1", phase=TurnPhase.SUSPENDED)
    )

    await pipeline.process_text("s1", "new unrelated question")
    after = await turn_store.list_active_turns(
        StateQueryScope(session_id="s1", phase=TurnPhase.SUSPENDED)
    )

    assert before[0].identity == after[0].identity
    assert after[0].phase.value == "suspended"


async def test_dream_engine_reads_committed_memory_not_message_delta(runtime_pipeline_factory, dream_engine) -> None:
    pipeline, turn_store = runtime_pipeline_factory(dream_engine=dream_engine)

    await pipeline.process_text("s1", "please use a pending tool")
    active = await turn_store.list_active_turns(
        StateQueryScope(session_id="s1", phase=TurnPhase.SUSPENDED)
    )

    assert active[0].message_delta != []
    assert await dream_engine.collect_candidate_messages("s1") == []
```

If current fixture names differ, create concrete fixtures in this test file using the project’s existing fake provider, in-memory memory, and tool manager test helpers.

- [ ] **Step 2: Run focused integration tests and verify failure**

Run:

```bash
python -m pytest tests/integration/test_runtime_state_governance.py -v
```

Expected: fail until helper fixtures and integration behavior are complete.

- [ ] **Step 3: Implement missing fixtures and fix behavior**

Create local fixtures that provide:

- fake LLM provider returning one tool call,
- fake dangerous approval classifier,
- in-memory `TurnStateStore`,
- pipeline with fake input/output adapters,
- fake tool manager returning deterministic `ToolResult`.

Do not add network calls or real bot dependencies.

- [ ] **Step 4: Run full unit test groups**

Run:

```bash
python -m pytest tests/unit/runtime tests/unit/agents/react tests/unit/approval tests/unit/pipeline tests/unit/interceptor tests/unit/hook tests/unit/control tests/unit/session tests/unit/multi_agent tests/unit/memory -v
```

Expected: pass.

- [ ] **Step 5: Run bot project tests**

Run:

```bash
PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v
```

Expected: pass.

- [ ] **Step 6: Run integration and e2e tests impacted by runtime state**

Run:

```bash
python -m pytest tests/integration tests/e2e -v
```

Expected: pass, or only skip tests that already require unavailable external services. Do not skip runtime-state tests.

- [ ] **Step 7: Run static checks**

Run:

```bash
ruff check framework tests examples/bot_project
mypy framework
```

Expected: pass.

- [ ] **Step 8: Run final legacy scans**

Run:

```bash
rg "TurnResumeState|ApprovalStateStore|LocalFileApprovalStateStore|InMemoryApprovalStateStore|StateStoreTurnResumeStateStore|_current_resume|checkpoint_store|suspend_strategy|deny_as_cancel|ctx\\.metadata|ctx\\.extensions|context\\.metadata|context\\.extensions" framework tests examples
rg "framework\\.control\\.checkpoint|framework\\.approval\\.store|framework\\.approval\\.state" framework tests examples
```

Expected: no output.

- [ ] **Step 9: Run final git checks**

Run:

```bash
git diff --check
git status --short
```

Expected:

- `git diff --check` has no output.
- `git status --short` shows only intentional implementation changes.

- [ ] **Step 10: Commit verification tests**

```bash
git add tests/integration/test_runtime_state_governance.py examples/bot_project/tests/test_runtime_state_governance_e2e.py
git commit -m "test: verify runtime state governance integration"
```

---

## Final Verification Checklist

Run all commands after Task 11:

```bash
python -m pytest tests/unit/runtime -v
python -m pytest tests/unit/agents/react -v
python -m pytest tests/unit/approval -v
python -m pytest tests/unit/pipeline -v
python -m pytest tests/unit/interceptor tests/unit/hook tests/unit/control -v
python -m pytest tests/unit/session tests/unit/multi_agent tests/unit/memory -v
PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v
python -m pytest tests/integration tests/e2e -v
ruff check framework tests examples/bot_project
mypy framework
git diff --check
rg "TurnResumeState|ApprovalStateStore|LocalFileApprovalStateStore|InMemoryApprovalStateStore|StateStoreTurnResumeStateStore|_current_resume|checkpoint_store|suspend_strategy|deny_as_cancel|ctx\\.metadata|ctx\\.extensions|context\\.metadata|context\\.extensions" framework tests examples
rg "framework\\.control\\.checkpoint|framework\\.approval\\.store|framework\\.approval\\.state" framework tests examples
```

Expected:

- All pytest groups pass or skip only pre-existing external-service e2e tests.
- `ruff` passes.
- `mypy framework` passes.
- `git diff --check` passes.
- Both `rg` cleanup commands return no output.
- No approval snapshot contains full session history.
- Suspended approval survives crash/reload by loading `TurnSnapshot`.
- Unrelated input during suspended approval does not auto-deny.
- Clean mode still creates `ReActTurnState`.
- Bot project uses `JsonFileTurnStateStore`.

## Self-Review Notes

Spec coverage:

- Runtime models, enums, scopes, and stores: Tasks 1 and 2.
- ReAct-specific typed state and future-mode boundary: Task 3.
- Non-generic `AgentContext` and service/state separation: Task 4.
- Metadata removal from ReAct execution: Task 5.
- Approval transaction, suspend/resume, crash-before-render, and no full history snapshots: Task 6.
- Pipeline, AgentSession, concurrency, memory commit, and DreamEngine isolation: Task 7.
- Hook, interceptor, and control integration: Task 8.
- Bot project reference wiring: Task 9.
- Full legacy cleanup and no backward compatibility: Task 10.
- Single-feature and combination verification: Task 11.

Placeholder scan:

- This plan intentionally avoids preserving old compatibility APIs.
- Any step that introduces a temporary migration helper must delete it in Task 10.
- No task is complete until its focused tests and commit step are done.

Type consistency:

- Store name is consistently `TurnStateStore`.
- Runtime command store name is consistently `RuntimeCommandStore`.
- Runtime services field is consistently `turn_store`.
- ReAct-specific codec name is consistently `ReActRuntimeStateCodec`.
