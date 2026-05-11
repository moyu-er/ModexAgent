<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-11 -->

# runtime

## Purpose
Runtime state governance — typed state models, enums, persistence abstractions, and services. This package owns the unification of formerly scattered runtime data (approval state, resume state, checkpoint, metadata) into a single `TurnSnapshot` + `TurnStateStore` design.

## Key Files
| File | Description |
|------|-------------|
| `models.py` | `TurnIdentity`, `TurnSnapshot`, `TurnStateBase`, `ApprovalTransaction`, `ToolBatchState`, `ToolCallState`, `OperationState`, `CancellationState` |
| `enums.py` | `StateScope`, `AgentKind`, `TurnPhase`, `TurnCustomKey`, `ApprovalDenyPolicy`, `OperationKind`, `ToolBatchStatus`, `ToolCallStatus`, `CancellationSource`, `SnapshotReason` |
| `store.py` | `TurnStateStore` (ABC), `RuntimeCommandStore` (ABC), `InMemoryTurnStateStore`, `NoOpTurnStateStore`, `JsonFileTurnStateStore` |
| `services.py` | `AgentRuntime`, `AgentRuntimeServices` |
| `codec.py` | `RuntimeStateCodec` (ABC), `RuntimeStateCodecRegistry`, `RuntimeStateCodecConfig` |
| `policy.py` | `SnapshotPolicy` (ABC) |

## Core Design Rules

1. **TurnStateStore is the ONE persistence abstraction.** All runtime state (approval, tool batches, operations) is persisted through `TurnSnapshot` → `TurnStateStore.save_turn()`. No separate approval/resume stores.

2. **Active turn conflict prevention.** `JsonFileTurnStateStore` enforces one active turn per (agent_id, session_id). Attempting to save a second RUNNING or SUSPENDED turn raises `ActiveTurnConflictError`.

3. **Services ≠ State.** `AgentRuntimeServices` carries process-scope services (hooks, interceptors, control, approval, governance, stores). `AgentRuntime.state` carries one turn-local state object. Per turn, construct a new `TurnStateBase` or restore from `TurnSnapshot`.

4. **Typed enums only.** All protocol values use `StrEnum`. No ad hoc string keys in `ctx.metadata`.

## Approval State

- `ApprovalTransaction` is inside `ReActTurnState.approval`. It owns decisions, deny_reason, status.
- `deny_reason` records WHY a tool was denied (e.g. `unrelated input: "hello"`).
- `apply_decision()` cascades: one DENIED → all unresolved → PREEMPTED.
- `ApprovalDenyPolicy` controls whether the turn cancels on denial. Default: `TOOL_RESULT_ONLY`.

## TurnCustomKey Usage

Keys for `TurnStateBase.custom` used by hooks and interceptors:
- `PRE_APPROVED_TOOL_IDS` — set of call_ids that ToolNode has already resolved
- `APPROVAL_YOLO` — flag to skip sensitive-tier approval
- `POLICY_DENIED_TOOLS` — tools denied by policy
- `MAX_TOOLS_PER_TURN` — per-turn tool limit

## Testing Requirements
- `tests/unit/runtime/` — codec, store, models, services, command store
- `tests/unit/approval/` — approval transaction state, e2e resume
- Validate one-active-turn rule, phase transitions, snapshot lifecycle
