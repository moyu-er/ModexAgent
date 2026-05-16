<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 -->

# runtime

## Purpose
Runtime state governance — typed state models, enums, persistence, codecs, and services. Unifies formerly scattered runtime data (approval, resume, checkpoint, metadata) into `TurnSnapshot` + `TurnStateStore`.

## Key Files
| File | Description |
|------|-------------|
| `enums.py` | 15+ runtime enum types: `StateScope`, `AgentKind`, `TurnPhase`, `ApprovalDenyPolicy`, `OperationKind`, `ToolBatchStatus`, `ToolCallStatus`, `CancellationSource`, `SnapshotReason`, etc. |
| `models.py` | `TurnStateBase`, `ApprovalTransaction`, `ToolBatchState`, `TurnSnapshot`, `TurnSummary`, `ControlCommandState` |
| `codec.py` | `RuntimeStateCodec` ABC + `RuntimeStateCodecRegistry` — serialization extensibility |
| `policy.py` | `SnapshotPolicy` ABC — when/how snapshots are taken |
| `services.py` | `AgentRuntimeServices` (process-scope services), `AgentRuntime` (composes services + state) |
| `store.py` | `TurnStateStore` + `RuntimeCommandStore` ABCs; `NoOp`/`InMemory`/`JsonFile` implementations |

## Design Rules
1. **TurnStateStore is the ONE persistence abstraction.** All runtime state flows through `TurnSnapshot` → `TurnStateStore.save_turn()`.
2. **One active turn per (agent_id, session_id).** `JsonFileTurnStateStore` enforces this via `ActiveTurnConflictError`.
3. **Services ≠ State.** `AgentRuntimeServices` holds process-scope services; `AgentRuntime.state` holds turn-local state.
4. **Typed enums only.** All protocol values use `StrEnum`. No ad hoc string keys in metadata.
