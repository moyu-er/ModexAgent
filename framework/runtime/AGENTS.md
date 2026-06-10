<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# runtime

## Purpose
Runtime state governance — typed state models, enums, persistence, codecs, and services. Unifies formerly scattered runtime data (approval, resume, checkpoint, metadata) into `TurnSnapshot` + `TurnStateStore`.

## Key Files
| File | Description |
|------|-------------|
| `enums.py` | `StateScope`, `AgentKind`, `TurnPhase`, `OperationKind`, `ToolBatchStatus`, `ToolCallStatus`, `ApprovalDenyPolicy`, `ApprovalSubjectType`, `OperationStatus`, `CancellationSource`, `SnapshotReason`, `ControlCommandKind`, `MessageDeltaSource`, `TurnCustomKey` |
| `models.py` | `TurnIdentity`, `ToolArguments`, `ApprovalRequest`, `ApprovalTransaction`, `ToolCallRecord`, `ToolBatchState`, `TurnStateBase`, `TurnSnapshot`, `TurnSummary`, `ControlCommandState`, `StateQueryScope`, `MessageDelta` |
| `codec.py` | `RuntimeStateCodec` ABC + `RuntimeStateCodecRegistry` — serialization extensibility |
| `policy.py` | `SnapshotPolicy` ABC — when/how snapshots are taken |
| `services.py` | `AgentRuntimeServices` (process-scope services: hooks, interceptors, control, approval, governance, stores), `AgentRuntime[composes services + state)` |
| `store.py` | `TurnStateStore` + `RuntimeCommandStore` ABCs; `NoOp`/`InMemory`/`JsonFile` implementations; `ActiveTurnConflictError` |
| `dispatch.py` | Runtime dispatch utilities |

## Design Rules
1. **TurnStateStore is the ONE persistence abstraction.** All runtime state flows through `TurnSnapshot` → `TurnStateStore.save_turn()`.
2. **One active turn per (agent_id, session_id).** `JsonFileTurnStateStore` enforces this via `ActiveTurnConflictError`.
3. **Services ≠ State.** `AgentRuntimeServices` holds process-scope services; `AgentRuntime.state` holds turn-local state.
4. **Typed enums only.** All protocol values use `StrEnum`. No ad hoc string keys in metadata.
