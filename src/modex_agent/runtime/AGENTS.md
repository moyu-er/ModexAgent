<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-10 -->

# runtime

## Purpose

Runtime state governance — typed state models, enums, persistence, codecs, and services. Unifies formerly scattered runtime data (approval, resume, checkpoint, metadata) into `TurnSnapshot` + `TurnStateStore`. Provides `AgentRuntimeServices` (process-scope services) and `AgentRuntime` (services + per-turn state composition).

## Key Files

| File | Description |
|------|-------------|
| `services.py` | `AgentRuntimeServices` — process-scope services container (hooks, interceptors, control, approval, governance, stores); `AgentRuntime` — composes services + per-turn state |
| `store.py` | `TurnStateStore` ABC; `NoOpTurnStateStore` / `InMemoryTurnStateStore` / `JsonFileTurnStateStore` implementations; `ActiveTurnConflictError`. Also `TodoStore` ABC + `JsonFileTodoStore` + `TodoItem` (per-session task-list store, a separate concern from turn snapshots — injected into the todo tools, not part of turn-state governance) |
| `dispatch.py` | `DispatchDeadline` — renewable monotonic-clock deadline with a **sliding forward ceiling**. The pool watchdog is the sole LLM termination mechanism (provider-level timeouts default to `None`). Streaming chunk callbacks renew +3s per chunk; each LLM iteration renews by `agent_run_timeout`. The sliding ceiling (`max_ahead_seconds`, default 1200s) caps how far a single `renew()` can reach ahead, but slides forward with each renewal — so continuous activity keeps the turn alive indefinitely. `current_dispatch_deadline` ContextVar + `renew_dispatch_deadline()` helper |
| `models.py` | Core data models — `TurnIdentity`, `ToolArguments`, `ApprovalRequest`, `ApprovalTransaction`, `ToolCallRecord`, `ToolBatchState`, `TurnStateBase`, `TurnSnapshot`, `TurnSummary`, `StateQueryScope`, `MessageDelta` |
| `enums.py` | Enumerations — `StateScope`, `AgentKind`, `TurnPhase`, `OperationKind`, `ToolBatchStatus`, `ToolCallStatus`, `ApprovalDenyPolicy`, `ApprovalSubjectType`, `OperationStatus`, `CancellationSource`, `SnapshotReason`, `MessageDeltaSource`, `TurnCustomKey` |
| `policy.py` | `SnapshotPolicy` ABC — defines when/how snapshots are taken during agent execution |
| `codec.py` | `RuntimeStateCodec` ABC + `RuntimeStateCodecRegistry` — serialization extensibility for runtime state |
| `approval_decision.py` | `ApprovalDecisionCoordinator` ABC + typed approval audit entry/decision models for atomic snapshot-and-audit persistence |
| `dream_locks.py` | Dream lock primitives — coordination locks for the dream engine's concurrent scan phases |

## Timeout Architecture

The LLM call chain has three layers:

1. **Provider HTTP timeout** (`request_timeout` / `stream_idle_timeout`) — defaults to `None` (no provider-level timeout). The provider waits indefinitely for the LLM.
2. **Per-iteration renewal** (`agent_run_timeout`, called by `nodes/llm.py` after each LLM iteration) — renews the `DispatchDeadline` by a large amount to cover tool execution + next iteration.
3. **Pool watchdog** (polls `DispatchDeadline.is_expired`) — the **sole termination mechanism**. Kills the turn only when the deadline is truly past and no renewal has occurred.

`DispatchDeadline` uses a **sliding forward ceiling** (`max_ahead_seconds`, default 1200s):
- Each `renew(seconds)` sets `expires_at = max(old_expires, now + seconds)`, capped at `now + max_ahead_seconds`.
- The cap slides forward with each renewal — continuous activity (streaming chunks) keeps the turn alive indefinitely.
- The ceiling only prevents a single `renew(huge)` from making the watchdog ineffective if activity later stops.

**Design intent**: as long as the LLM is actively producing output (chunks arriving every <1200s), the turn never times out. The watchdog only fires when activity genuinely stops for longer than the current remaining deadline.

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

## turnId / trace_id Future Consideration

`turn_uuid` is generated in the pipeline layer (`TurnRunner`), and `trace_id` is generated in `TraceCollectorHook.before_graph()`. Both fire once per `actual_turn()` call, so approval resume (which re-enters `actual_turn()`) generates a new trace root. A future improvement could move `trace_id` generation to `START_NODE_TURN` so approval-resume continues the same trace. This is deferred because it involves `TraceCollectorHook` span lifecycle redesign.

## Dependencies

- `modex_agent.core.agent` — `Agent[E]` for turn execution
- `modex_agent.core.types` — base types used by models
- `modex_agent.hook` — `HookRunner` for lifecycle hooks (injected via `AgentRuntimeServices`)
- `modex_agent.interceptor` — `InterceptorChain` for AOP interception (injected via `AgentRuntimeServices`)
- `modex_agent.control` — `InMemoryControlChannel` + `ControlCommand`/`ControlScope` (live control-plane types)
