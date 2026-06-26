<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# runtime

## Purpose

Runtime state governance — typed state models, enums, persistence, codecs, and services. Unifies formerly scattered runtime data (approval, resume, checkpoint, metadata) into `TurnSnapshot` + `TurnStateStore`. Provides `AgentRuntimeServices` (process-scope services) and `AgentRuntime` (services + per-turn state composition).

## Key Files

| File | Description |
|------|-------------|
| `services.py` | `AgentRuntimeServices` — process-scope services container (hooks, interceptors, control, approval, governance, stores); `AgentRuntime` — composes services + per-turn state |
| `store.py` | `TurnStateStore` ABC; `NoOpTurnStateStore` / `InMemoryTurnStateStore` / `JsonFileTurnStateStore` implementations; `ActiveTurnConflictError`. Also `TodoStore` ABC + `JsonFileTodoStore` + `TodoItem` (per-session task-list store, a separate concern from turn snapshots — injected into the todo tools, not part of turn-state governance) |
| `dispatch.py` | Runtime dispatch utilities |
| `models.py` | Core data models — `TurnIdentity`, `ToolArguments`, `ApprovalRequest`, `ApprovalTransaction`, `ToolCallRecord`, `ToolBatchState`, `TurnStateBase`, `TurnSnapshot`, `TurnSummary`, `StateQueryScope`, `MessageDelta` |
| `enums.py` | Enumerations — `StateScope`, `AgentKind`, `TurnPhase`, `OperationKind`, `ToolBatchStatus`, `ToolCallStatus`, `ApprovalDenyPolicy`, `ApprovalSubjectType`, `OperationStatus`, `CancellationSource`, `SnapshotReason`, `MessageDeltaSource`, `TurnCustomKey` |
| `policy.py` | `SnapshotPolicy` ABC — defines when/how snapshots are taken during agent execution |
| `codec.py` | `RuntimeStateCodec` ABC + `RuntimeStateCodecRegistry` — serialization extensibility for runtime state |
| `dream_locks.py` | Dream lock primitives — coordination locks for the dream engine's concurrent scan phases |

## Design Rules

1. **TurnStateStore is the ONE persistence abstraction.** All runtime state flows through `TurnSnapshot` → `TurnStateStore.save_turn()`.
2. **One active turn per (agent_id, session_id).** `JsonFileTurnStateStore` enforces this via `ActiveTurnConflictError`.
3. **Services ≠ State.** `AgentRuntimeServices` holds process-scope services; `AgentRuntime.state` holds turn-local state.
4. **Typed enums only.** All protocol values use `StrEnum`. No ad hoc string keys in metadata.

## For AI Agents

- Use `TurnStateStore` for all runtime persistence — do not bypass it with custom file I/O.
- `AgentRuntimeServices` is constructed once per process; `AgentRuntime` is constructed per turn.
- `TurnSnapshot` captures the full agent state for approval suspend/resume.
- `dream_locks.py` is specific to the dream engine's scan phase — not for general locking.

## Note on Control-Related Types

`AgentRuntimeServices.control_channel` is threaded through but not fed in the default runtime; the live cancellation path is `asyncio.Task.cancel()` in the pipeline (see `modex_agent/control/AGENTS.md`).

## Dependencies

- `modex_agent.core.agent` — `Agent[E]` for turn execution
- `modex_agent.core.types` — base types used by models
- `modex_agent.hook` — `HookRunner` for lifecycle hooks (injected via `AgentRuntimeServices`)
- `modex_agent.interceptor` — `InterceptorChain` for AOP interception (injected via `AgentRuntimeServices`)
- `modex_agent.control` — control channel types (vestigial)
