# Runtime State Governance Design

Date: 2026-05-09

## Purpose

ModexAgent currently keeps runtime data in several overlapping places:

- `AgentContext.metadata`
- `AgentContext.extensions`
- `ReActRuntime`
- memory checkpoints
- runtime checkpoints
- approval state files
- turn-resume state files
- control command stores

This makes it hard to answer basic questions:

- Which component owns this state?
- Is it process-local, session-level, turn-level, or operation-level?
- Should it be persisted?
- When should it be cleared?
- Is this state specific to ReAct, or should future agent modes reuse it?

The goal is a breaking redesign. The framework is not yet used externally, so
we should not preserve old runtime-state APIs. Temporary compatibility may be
used during implementation, but the final result must remove historical state
paths and duplicate stores.

The end state should be simple enough for contributors who understand the
project architecture, but have not studied the current runtime internals, to
extend safely.

## Design Goals

1. Every runtime datum has an explicit owner, scope, lifecycle, and storage rule.
2. Runtime services and runtime state are separate.
3. Approval is modeled as one kind of runtime transaction, not a special side
   store with its own recovery model.
4. ReAct-specific state is isolated from generic agent runtime state.
5. Future agent modes, such as Plan-and-Execute, can define their own state
   without polluting ReAct or `AgentContext`.
6. Runtime persistence is backend-agnostic. The default implementation is local
   JSON files, but Redis, PostgreSQL, SQLite, or encrypted file storage should
   fit behind the same interface.
7. Protocol values use enums and typed structures, not ad hoc string keys.
8. No full session history is copied into approval state or resume state.

## Non-Goals

- Preserving old imports, old `ctx.metadata` keys, old approval stores, or old
  checkpoint IDs.
- Building Redis or PostgreSQL storage in the first implementation.
- Designing a full event-sourcing system now. The design keeps a path open for
  journal-style state later, but starts with snapshots.
- Replacing the memory system. Conversation memory remains a separate subsystem.

## Current Problems

### Mixed Lifecycles

The same containers hold data with very different lifetimes:

- `ctx.extensions` carries services such as hooks, interceptors, governance, and
  checkpoint stores.
- `ctx.metadata` carries ReAct control state, approval flags, cancellation
  reasons, dynamic hook data, and graph result data.
- approval uses both `ApprovalStateStore` and `TurnResumeStateStore`.
- runtime checkpoint and memory checkpoint both preserve turn messages.

This means a short-lived tool decision can sit beside a session-level runtime
context manager, and a persistent approval state can duplicate recovery data
from another persistent state file.

### Duplicate Recovery State

Approval suspend/resume currently saves:

- approval requests and decisions in approval state,
- resume node and turn messages in turn-resume state,
- new turn messages in runtime checkpoint,
- message checkpoint in memory.

The same turn has multiple partial truths. Recovery depends on coordinating all
of them correctly.

### Approval Is Too Special

Approval is currently a side path controlled by `ApprovalRenderer`,
`ApprovalStateStore`, `TurnResumeStateStore`, and a context variable. It should
instead be a transaction inside turn state. Tool calls, approval requests,
approval decisions, and post-approval tool execution belong to one state tree.

### Weak Abstraction for Future Agent Modes

The current state shape is ReAct-centric. Future modes such as Plan-and-Execute
will need plan IDs, step state, step retry state, and step-level approval. If the
framework keeps using `metadata` as a generic bag, each new mode will add more
private keys.

## State Scope Model

All runtime state and services must declare a scope.

```python
class StateScope(StrEnum):
    PROCESS = "process"
    AGENT = "agent"
    SESSION = "session"
    TURN = "turn"
    OPERATION = "operation"
```

### Process Scope

Process-scope values are long-lived services and are not persisted as state.

Examples:

- LLM provider
- hook runner
- interceptor chain
- control channel
- runtime state store instance
- safety policy
- governance service

### Agent Scope

Agent-scope values describe one agent instance or resident agent.

Examples:

- agent ID and role
- agent kind
- tool policy
- skill policy
- configured execution strategy

### Session Scope

Session-scope values survive across turns in one conversation.

Examples:

- conversation memory
- inbox messages
- session locks
- session-level runtime context records, if needed

Session-scope data should not be copied into approval state or runtime
snapshots. Runtime snapshots may reference a session by ID.

### Turn Scope

Turn-scope values exist for one user input and its resulting agent execution.

Examples:

- current graph node
- current phase
- ReAct iteration
- message delta created by this turn
- active approval transaction
- active tool batch
- cancellation state

Turn-scope state is the main target of runtime snapshots.

### Operation Scope

Operation-scope values describe one LLM call, tool call, approval request, or
control command.

Examples:

- one tool call and its arguments
- one approval request and decision
- one LLM call result envelope
- one control command processing result

Operation state is usually nested under turn state.

## Core Enums

Runtime protocols must avoid hard-coded strings. New runtime values should use
enums or typed constants.

```python
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


class SnapshotReason(StrEnum):
    LLM_COMPLETED = "llm_completed"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    TOOL_BATCH_PROGRESS = "tool_batch_progress"
    TURN_INTERRUPTED = "turn_interrupted"
    ERROR_RECOVERY = "error_recovery"
```

ReAct should continue using typed `ReActNode` and `ReActReason`, but these values
must live in structured state fields rather than `ctx.metadata` keys.

## Core Runtime Structure

### TurnIdentity

Every turn has a stable identity.

```python
@dataclass(frozen=True)
class TurnIdentity:
    agent_id: str
    session_id: str
    turn_id: str
    conversation_id: str | None = None
```

Stores use this identity. Pipeline and agents must not hand-build checkpoint
IDs.

### Persistable Values

Persistent runtime payloads should use a small JSON value type alias instead of
open-ended `object` dictionaries.

```python
JsonPrimitive = str | int | float | bool | None
JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class ToolArguments:
    values: Mapping[str, JsonValue]
```

`ToolArguments` is a value object because tool arguments are part of approval,
audit, and recovery. Tool code may convert it to provider-specific call formats
at the edge, but state models should keep the typed wrapper.

### AgentContext

`AgentContext` should be an execution entry object, not a general state store.
It should not be generic. A generic context looks precise at first, but it
spreads mode-specific type parameters through hooks, interceptors, emitters, and
pipeline code that should stay mode-neutral.

```python
@dataclass
class AgentContext:
    identity: TurnIdentity
    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager
    runtime: AgentRuntime
    emitter: ContentEmitter | None = None
    attachments: list[str] = field(default_factory=list)
```

Final ReAct code should not depend on `metadata` or `extensions`.

Mode-specific code validates or narrows the state at the boundary:

```python
def require_react_state(ctx: AgentContext) -> ReActTurnState:
    if not isinstance(ctx.runtime.state, ReActTurnState):
        raise TypeError("ReAct agent requires ReActTurnState")
    return ctx.runtime.state
```

Only agent implementations and their tests should know the concrete state
subclass. Shared services should use `TurnStateBase`.

### AgentRuntime

Runtime separates process-local services from turn-local state.

```python
@dataclass
class AgentRuntime:
    services: AgentRuntimeServices
    state: TurnStateBase
```

### AgentRuntimeServices

Services are not serialized into snapshots.

```python
@dataclass
class AgentRuntimeServices:
    hooks: HookRunner | None = None
    interceptors: InterceptorChain | None = None
    control: ControlRuntime | None = None
    governance: ContextGovernance | None = None
    state_store: RuntimeStateStore | None = None
    pending_input_queue: asyncio.Queue[str] | None = None
    safety: RuntimeSafetyPolicy = field(default_factory=RuntimeSafetyPolicy)
```

## Generic Turn State

All agent modes share a base state shape.

```python
@dataclass
class MessageDelta:
    message: ChatMessage
    source: MessageDeltaSource
    provider_payload: Mapping[str, JsonValue] | None = None


@dataclass
class TurnStateBase:
    identity: TurnIdentity
    agent_kind: AgentKind
    phase: TurnPhase
    created_at: float
    updated_at: float
    message_delta: list[MessageDelta] = field(default_factory=list)
    operations: list[OperationState] = field(default_factory=list)
    cancellation: CancellationState | None = None
```

`message_delta` contains only messages created during the current turn. It is
not full session history. `MessageDelta.message` is the normalized memory
message. `provider_payload` is optional and should only keep a small, explicit
provider envelope required for recovery, such as an assistant tool-call payload.
It is not a place to store raw provider clients, full request bodies, or
unbounded debug data.

### Operation State

Operations are the structured history of work performed inside a turn. They
replace scattered metadata flags and side stores.

```python
@dataclass
class RuntimeErrorState:
    error_type: str
    message: str
    retryable: bool


@dataclass
class OperationState:
    operation_id: str
    kind: OperationKind
    status: OperationStatus
    subject_id: str | None
    created_at: float
    updated_at: float
    error: RuntimeErrorState | None = None
```

`OperationState` is the common index record for lifecycle inspection. The
domain payload stays in typed models such as:

- `LLMCallState`: one model request and its recoverable response envelope.
- `ToolBatchState`: one ReAct tool batch produced by an assistant message.
- `ToolCallState`: one tool call, arguments, decision, result, and error.
- `ApprovalTransaction`: one approval transaction for tool calls or plan steps.
- `ControlCommandState`: one runtime command and its applied mutation.

The operation list is useful for generic inspection and audit. Agent-specific
state keeps typed fields, such as `ReActTurnState.tool_batches`, so nodes do not
need to scan the operation list repeatedly or downcast untyped payloads.

## ReAct Turn State

ReAct extends the generic turn state.

```python
@dataclass
class ReActTurnState(TurnStateBase):
    current_node: ReActNode = ReActNode.START
    iteration: int = 0
    llm_response: LLMResponse | None = None
    tool_batches: list[ToolBatchState] = field(default_factory=list)
    approval: ApprovalTransaction | None = None
```

ReAct graph nodes read and write this object directly:

- `StartNode` sets `current_node` and initializes iteration.
- `LLMNode` updates `iteration`, `llm_response`, and `message_delta`.
- `ToolNode` creates and updates `ToolBatchState`.
- `EndNode` builds `AgentResult` from state, commits memory, and clears the
  runtime snapshot.

## Future Plan-and-Execute State

Plan-and-Execute should reuse the same runtime infrastructure with a different
state payload.

```python
@dataclass
class PlanExecuteTurnState(TurnStateBase):
    plan_id: str
    steps: list[PlanStepState]
    active_step_index: int
    approval: ApprovalTransaction | None = None
```

This is the main reason the state system should be generic and agent-kind aware.

## Approval as a Runtime Transaction

Approval is not a standalone store. It is a transaction inside turn state.

```python
class ApprovalSubjectType(StrEnum):
    TOOL_CALL = "tool_call"
    TOOL_BATCH = "tool_batch"
    PLAN_STEP = "plan_step"


@dataclass
class ApprovalTransaction:
    approval_id: str
    turn_id: str
    subject_type: ApprovalSubjectType
    subject_ids: list[str]
    requests: list[ApprovalRequestState]
    decisions: dict[str, ApprovalDecision]
    status: ApprovalStatus
    deny_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
```

Tool approval data belongs with tool execution data.

```python
@dataclass
class ToolBatchState:
    batch_id: str
    iteration: int
    calls: list[ToolCallState]
    approval_id: str | None
    status: ToolBatchStatus


@dataclass
class ToolCallState:
    call_id: str
    tool_name: str
    arguments: ToolArguments
    approval_id: str | None
    decision: ApprovalDecision | None
    result: ToolResult | None
    status: ToolCallStatus
```

This lets the framework answer:

- which tools required approval,
- which arguments were approved,
- which approved tools executed,
- which tools were denied or preempted,
- which result was produced after approval.

## Runtime Snapshots

The store persists snapshots, not services and not full session history.

```python
@dataclass(frozen=True)
class ResumePoint:
    agent_kind: AgentKind
    phase: TurnPhase
    node: StrEnum | None = None
    iteration: int | None = None
    operation_id: str | None = None


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
```

`ResumePoint.node` is an enum value. ReAct uses `ReActNode`; future modes should
define their own node or phase enum rather than storing raw node strings.

Snapshots are created by a policy object so persistence decisions do not leak
into graph nodes.

```python
class SnapshotPolicy(ABC):
    @abstractmethod
    def should_capture(
        self,
        state: TurnStateBase,
        reason: SnapshotReason,
    ) -> bool: ...

    @abstractmethod
    def capture(
        self,
        state: TurnStateBase,
        reason: SnapshotReason,
    ) -> TurnSnapshot: ...
```

ReAct should provide `ReActSnapshotPolicy`. Plan-and-Execute should provide its
own policy if its resume points differ.

For ReAct, `state_payload` contains only recoverable ReAct fields:

- current node,
- iteration,
- current or pending tool batch,
- approval transaction,
- cancellation state,
- minimal LLM tool-call envelope needed to resume tool execution.

It must not contain:

- full session history,
- LLM provider instances,
- hook runner instances,
- interceptor instances,
- control channel instances,
- memory system objects.

## Runtime Store Abstraction

The store API is semantic, not file-based.

```python
class RuntimeStateStore(ABC):
    @abstractmethod
    async def save_turn(self, snapshot: TurnSnapshot) -> None: ...

    @abstractmethod
    async def load_turn(self, identity: TurnIdentity) -> TurnSnapshot | None: ...

    @abstractmethod
    async def delete_turn(self, identity: TurnIdentity) -> None: ...

    @abstractmethod
    async def list_active_turns(
        self,
        scope: StateQueryScope,
    ) -> list[TurnSnapshot]: ...
```

`StateQueryScope` is a typed query object, not raw string filtering.

```python
@dataclass(frozen=True)
class StateQueryScope:
    agent_id: str | None = None
    session_id: str | None = None
    agent_kind: AgentKind | None = None
    phase: TurnPhase | None = None
    reason: SnapshotReason | None = None
    created_before: float | None = None
```

## Codec Layer

Models should not be dumped directly with `json.dumps(dataclass)`.

```python
class RuntimeStateCodec(ABC):
    @abstractmethod
    def encode_turn(self, snapshot: TurnSnapshot) -> Mapping[str, JsonValue]: ...

    @abstractmethod
    def decode_turn(self, payload: Mapping[str, JsonValue]) -> TurnSnapshot: ...
```

The codec owns:

- schema versioning,
- enum serialization,
- dataclass conversion,
- rejecting unserializable process objects,
- future payload migrations.

The first implementation does not need backward compatibility with historical
approval or resume files. It should support only the new schema. If migration
helpers are useful during implementation, delete them before the final cleanup
phase.

## Default File Backend

The first implementation only needs local JSON files.

```python
class JsonFileRuntimeStateStore(RuntimeStateStore):
    def __init__(
        self,
        workspace: Path,
        codec: RuntimeStateCodec | None = None,
    ) -> None: ...
```

Suggested layout:

```text
data/runtime_state/
  <agent_id>/
    <safe_session_id>/
      <turn_id>.json
```

This is an implementation detail. Callers never construct paths.

## RuntimeContext Disposition

The old `RuntimeContext` and `RuntimeContextManager` are not state governance
primitives. They should not continue as a generic session key-value store for
ReAct execution.

Final disposition:

- tool execution tracking moves into `ToolCallState` and `ToolBatchState`,
- approval and resume tracking moves into `ApprovalTransaction` and
  `TurnSnapshot`,
- cross-turn conversation data remains in memory or a dedicated session service,
- process-local service wiring remains in `AgentRuntimeServices`,
- hook-local per-turn data becomes typed turn or operation state.

If a future feature needs session-scope runtime data, it must introduce a typed
session model and store contract. It should not revive `metadata` or generic
context dictionaries.

## Hook Contract

Hooks receive the same `AgentContext` used by the runtime. They can read
`ctx.runtime.state` and call services through `ctx.runtime.services`.

Rules:

- hooks must not create private runtime state in shared instance attributes,
- hooks must not use `ctx.metadata` as a hidden state channel,
- hooks may mutate documented typed fields on `TurnStateBase` or a concrete
  mode state,
- hooks that need new state must add a typed model first,
- event-specific `HookPayload` objects may describe an event, but they are not
  the owner of persistent runtime state.

Existing hook behavior that reads completed tool calls should read
`ToolCallState` records or a helper such as `state.completed_tool_calls()`.

## Interceptor Contract

Interceptors are allowed to inspect and govern runtime behavior, so their
contexts need typed access to the active turn state. Add `turn_state` to
interceptor context models instead of passing raw metadata.

```python
@dataclass
class ToolCallContext:
    turn_state: TurnStateBase
    tool_name: str
    arguments: ToolArguments


@dataclass
class IterationContext:
    turn_state: TurnStateBase
    iteration: int


@dataclass
class LLMStreamContext:
    turn_state: TurnStateBase
    request: LLMRequest
```

Interceptors may return typed decisions or mutations. They should not write
protocol flags into untyped dictionaries.

## ControlRuntime Integration

`ControlRuntime` remains the command plane and lives in
`AgentRuntimeServices.control`. It should not own a separate checkpoint model.

```python
@dataclass(frozen=True)
class ControlMutation:
    command_id: str
    operation_id: str
    target_phase: TurnPhase | None = None
    cancellation: CancellationState | None = None
    snapshot_reason: SnapshotReason | None = None
```

Command handling should follow this shape:

1. pipeline receives or polls a command,
2. `ControlRuntime` resolves the handler,
3. the handler receives `AgentContext`, the command, and the active
   `TurnStateBase`,
4. the handler returns a typed `ControlMutation`,
5. the runtime applies the mutation to turn state,
6. the snapshot policy decides whether the mutation requires persistence.

This keeps command routing as a service concern and command effects as state
concerns.

## AgentSession and Clean Runtime Mode

`AgentSession` should create the same runtime shape as the pipeline:
`TurnIdentity`, `AgentRuntimeServices`, and a mode-specific `TurnStateBase`.
Session APIs may choose a `NoOpRuntimeStateStore` when persistence is disabled,
but they should not bypass turn state.

Clean runtime mode means optional services are absent or no-op. It does not mean
state is absent. A clean ReAct turn still has `ReActTurnState`; it simply does
not persist snapshots unless a store is configured and a snapshot policy asks
for one.

## Background Work and Concurrency

Background engines, such as DreamEngine, must read committed memory and their
own explicit stores. They must not read active `TurnStateBase.message_delta`
while a turn is still running.

Runtime commit rules:

- each `(agent_id, session_id)` has at most one active mutable turn by default,
- `turn_id` is generated with `uuid4().hex`,
- the store rejects a second `RUNNING` or `SUSPENDED` turn for the same default
  concurrency scope unless an explicit policy allows it,
- `message_delta` is private until completion commits it to memory,
- background memory scans see the last committed conversation state, not
  uncommitted turn deltas.

If future modes support parallel steps, they should model that inside one turn
state with operation IDs and step IDs, not by creating competing hidden runtime
stores.

## Approval Suspend Flow

When a ReAct tool batch needs approval:

1. `ToolNode` creates `ToolBatchState`.
2. `ToolNode` creates `ApprovalTransaction`.
3. `state.approval` points to that transaction.
4. `state.phase = TurnPhase.SUSPENDED`.
5. `state.current_node = ReActNode.TOOL`.
6. `SnapshotPolicy.capture(state, SnapshotReason.TOOL_APPROVAL_REQUIRED)`
   creates a `TurnSnapshot`.
7. `RuntimeStateStore.save_turn(snapshot)` persists it.
8. The agent returns a suspend signal or raises `GraphInterrupt`.
9. The UI renders approval requests.

No separate approval state file is written.

Mixed approval batches are treated atomically. If a newly generated batch
contains any pending approval request, the runtime suspends before executing any
new tool call in that batch. Auto-allowed calls are retained in their original
order and execute after the approval transaction reaches a terminal decision.
This avoids partial side effects before the user has responded to the batch.
If policy produces a hard denial before suspension, denied calls are recorded as
denied results and no pending prompt is rendered for those calls.

## Approval Resume Flow

When the user sends `/approve` or `/deny`:

1. Pipeline asks `RuntimeStateStore` for active suspended turns in the session.
2. Pipeline loads the matching `TurnSnapshot`.
3. The codec restores `ReActTurnState`.
4. The approval decision updates `state.approval`.
5. Related `ToolCallState.decision` fields are updated.
6. If approval is partial, the same turn snapshot is saved again.
7. If approval is complete, `state.phase = TurnPhase.RUNNING` and execution
   resumes from `state.current_node`.

There is no `_current_resume` context variable.

If the process crashes after saving a suspended snapshot but before rendering
the approval prompt, the next non-approval user input for that session should
not auto-deny the transaction. The pipeline should reload the suspended turn and
re-render the approval prompt, or buffer the unrelated input until the approval
transaction is resolved. Denial requires an explicit denial command or a policy
timeout.

## Tool Execution After Approval

`ToolNode` reads the latest `ToolBatchState`.

- `ALLOWED`: execute the tool and store the result in `ToolCallState.result`.
- `DENIED`: do not execute; record a structured denied result.
- `PREEMPTED`: do not execute; record a structured preempted result.

After each tool call, the runtime may snapshot with
`SnapshotReason.TOOL_BATCH_PROGRESS`. This gives crash recovery without saving
full session history.

After the batch completes:

- `ToolBatchState.status = ToolBatchStatus.COMPLETED`,
- `ApprovalTransaction.status = ApprovalStatus.COMPLETED`,
- active `state.approval` is cleared or moved to completed operation history,
- execution continues to the next LLM node.

## Turn Completion and Cleanup

Successful turn completion:

1. Commit `state.message_delta` to conversation memory.
2. Emit final result.
3. Delete the runtime snapshot with `state_store.delete_turn(identity)`.
4. Clear turn-local state.

Failed or interrupted turn:

- If the failure is recoverable, keep the snapshot.
- If the turn was explicitly cancelled and no recovery is needed, delete the
  snapshot.
- If audit is needed, write a lightweight `TurnSummary`; do not keep a full
  snapshot forever.

## Relationship to Memory

Memory and runtime state have different responsibilities.

Memory:

- stores long-lived conversation messages,
- applies compression and governance,
- owns session/archive/knowledge layers.

Runtime state:

- stores in-progress turn execution,
- supports suspend/resume and crash recovery,
- contains operation state such as tool calls and approval transactions.

Runtime snapshots may contain `message_delta` for recovery. Conversation memory
is updated after turn completion. This avoids copying full session history into
approval or runtime recovery data.

## Components to Remove or Merge

Remove:

- `TurnResumeState`
- `TurnResumeStateStore`
- `InMemoryTurnResumeStateStore`
- `StateStoreTurnResumeStateStore`
- `ApprovalStateStore`
- `InMemoryApprovalStateStore`
- `LocalFileApprovalStateStore`
- `_current_resume`
- ReAct control-state usage of `ctx.metadata`
- ReAct runtime-service usage of `ctx.extensions`

Merge or replace:

- `control.checkpoint.RuntimeStateStore` becomes the new
  `runtime.store.RuntimeStateStore`.
- runtime checkpoint, approval state, and resume state become one
  `TurnSnapshot`.
- memory execution checkpoint should be replaced by runtime `message_delta`
  recovery, then memory commit on turn completion.

Keep, but reposition:

- `ControlRuntime` remains the command plane.
- `ApprovalRuntime` can remain as classifier/policy service, but not as a state
  owner.
- `RuntimeAssembler` can remain as a service assembler, but should create
  `AgentRuntimeServices`, not hide state in `AgentContext.extensions`.

## Implementation Phases

### Phase 1: Runtime Models and Store

- Add `framework/runtime/`.
- Add enums, identities, state models, snapshot models, codec, in-memory store,
  and JSON file store.
- Add focused unit tests.

### Phase 2: ReAct State Migration

- Replace ReAct `ctx.metadata` usage with `ReActTurnState`.
- Update `StartNode`, `LLMNode`, `ToolNode`, and `EndNode`.
- Remove `ReActMetaKey` entries that represented control state.

### Phase 3: Approval Transaction Migration

- Replace `ApprovalState` persistence with `ApprovalTransaction`.
- Remove separate approval and resume stores.
- Make approval suspend/resume operate on `TurnSnapshot`.
- Remove `_current_resume`.

### Phase 4: Pipeline Runtime Integration

- Change Pipeline to create `TurnIdentity`, `AgentRuntimeServices`, and
  agent-specific turn state.
- Make approval command handling load suspended turns through
  `RuntimeStateStore`.
- Stop constructing checkpoint IDs in Pipeline.
- Generate `turn_id` with `uuid4().hex`.
- Enforce the default one-active-turn-per-agent-session rule.

### Phase 5: Memory Checkpoint Cleanup

- Remove duplicated message checkpoint paths.
- Use runtime snapshot `message_delta` for in-progress turn recovery.
- Commit messages to memory only on successful turn completion or explicit
  terminal failure policy.

### Phase 6: Bot Project Rewire

- Configure a single `JsonFileRuntimeStateStore`.
- Inject it through runtime services.
- Remove bot-project approval workspace/store special cases.
- Update `BotService` startup to build `AgentRuntimeServices` once and pass it
  into every pipeline or session turn.
- Update bot approval command routing to query suspended turns through
  `RuntimeStateStore.list_active_turns(StateQueryScope(...))`.
- Replace bot-specific resume command payloads with typed approval decisions.
- Remove bot code that reads or writes old approval files, resume files, or
  runtime checkpoint IDs.
- Update bot tests so the reference project demonstrates the new default file
  backend and the no-history-copy approval model.

### Phase 7: Historical Cleanup

- Delete obsolete stores, state files, metadata keys, extension plumbing, and
  tests for removed behavior.
- Update AGENTS and runtime documentation.

## Testing Strategy

New tests should mirror the new boundaries.

- `tests/unit/runtime/test_models.py`
  Validate enums, identities, state defaults, and lifecycle transitions.

- `tests/unit/runtime/test_codec.py`
  Validate encode/decode, schema versioning, and rejection of unserializable
  process services.

- `tests/unit/runtime/test_file_store.py`
  Validate save/load/delete/list behavior for the default file backend.

- `tests/unit/runtime/test_runtime_services.py`
  Validate service assembly, no-op clean services, and absence of serialized
  process services.

- `tests/unit/runtime/test_turn_lifecycle.py`
  Validate phase transitions, snapshot decisions, completion cleanup, and
  recoverable failure retention.

- `tests/unit/runtime/test_concurrent_turns.py`
  Validate the one-active-turn-per-agent-session rule and explicit rejection of
  conflicting active turns.

- `tests/unit/runtime/test_clean_mode.py`
  Validate clean mode still creates typed turn state and skips persistence when
  no store is configured.

- `tests/unit/agents/react/test_turn_state.py`
  Validate ReAct nodes update `ReActTurnState` instead of `ctx.metadata`.

- `tests/unit/hook/test_runtime_state_hooks.py`
  Validate hooks receive typed runtime state and do not depend on metadata.

- `tests/unit/interceptor/test_runtime_state_interceptors.py`
  Validate interceptor contexts expose `turn_state` and return typed decisions.

- `tests/unit/approval/test_transaction_state.py`
  Validate batch atomicity, denial preemption, partial approval, and final
  decisions.

- `tests/unit/pipeline/test_runtime_resume.py`
  Validate suspended-turn loading and approval resume through the runtime store.

- `examples/bot_project/tests/test_approval_flow.py`
  Validate end-to-end approval in the reference bot wiring.

## New Rules for Contributors

1. Do not add ReAct control state to `ctx.metadata`.
2. Do not add runtime services to `ctx.extensions`.
3. Do not persist process services.
4. Do not persist full session history in approval or runtime snapshots.
5. Do not hand-build checkpoint IDs.
6. Use enums for protocol values.
7. Put approval state inside turn state.
8. Add a typed state model before adding a new runtime datum.
9. Add codec coverage when a state model becomes persistent.
10. Add lifecycle cleanup tests for every persistent runtime state.

## Open Design Decisions

The first implementation should make these decisions explicit:

- whether completed approval transactions are removed from turn state or kept as
  compact operation history until turn completion,
- whether failed turns retain snapshots by default or only for selected failure
  reasons,
- whether snapshot writes happen after every tool call or only at tool-batch
  boundaries.

Recommended defaults:

- keep completed approval transactions until turn completion, then delete with
  the snapshot;
- retain failed snapshots only for recoverable failures;
- store `message_delta` as `MessageDelta` records with normalized `ChatMessage`
  plus optional small provider payloads;
- snapshot after approval suspend and after each tool call completion.
