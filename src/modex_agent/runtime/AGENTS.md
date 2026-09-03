<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 -->

# runtime

## Purpose

Runtime state governance — typed state models, enums, persistence, codecs, and services. Unifies formerly scattered runtime data (approval, resume, checkpoint, metadata) into `TurnSnapshot` + `TurnStateStore`. Provides `AgentRuntimeServices` (process-scope services) and `AgentRuntime` (services + per-turn state composition). Also owns the per-turn `RuntimeContext` container subsystem (moved from `core/`, plan §15 B2).

## Key Files

| File | Description |
|------|-------------|
| `services.py` | `AgentRuntimeServices` — process-scope services container (hooks, interceptors, control, approval, governance, stores); `AgentRuntime` — composes services + per-turn state |
| `context.py` | `ToolCallRecord`, `RuntimeContext` ABC, `InMemoryRuntimeContext`, and `RuntimeContextManager` for per-session runtime contexts. |
| `hooks.py` | `RuntimeContextHook` (moved from `hook/builtin/runtime_context.py`, plan §15 B2) — manages the per-turn RuntimeContext lifecycle: resolves/caches the session context at `start_node_turn`, records `ToolCallRecord`s around tool execution. Production-dormant: registered explicitly by business code, never auto-injected |
| `process_identity.py` | `ProcessIdentity` — lazily generates and logs the process-level snowflake used for graph instance ownership |
| `process_registry.py` | `ProcessRegistry` ABC + `SingletonProcessRegistry` zero-infrastructure liveness implementation; replace the implementation for multi-instance discovery |
| `constants.py` | `EXECUTOR_PROCESS_ID_KEY` — typed `GraphMetadata.attrs` key for executor ownership |
| `store.py` | `TurnStateStore` ABC; `NoOpTurnStateStore`, `InMemoryTurnStateStore`, and `JsonFileTurnStateStore`; `ActiveTurnConflictError`. |
| `todo.py` | Per-session `TodoStatus`, `TodoItem`, `TodoStore`, and `JsonFileTodoStore`; SQLite implementation lives in `persistence/adapters/todo_store.py`. |
| `dispatch.py` | `DispatchDeadline` — the unified watchdog deadline (module docstring carries the phase-budget protocol table). The pool watchdog is the sole termination mechanism (provider-level timeouts default to `None`). Phases owning an inner deadline declare their full budget at entry (tool: `tool_timeout + margin`; hooks: `hook_timeout×n + margin`; turn tail: flush+hook+margin); LLM calls re-assert `dispatch_timeout` at call entry; activity signals (react stream chunks, external provider events) renew by `chunk_renew_seconds`. `DeadlinePolicy` (`core/llm_struct.py`) holds the knobs (`chunk_renew_seconds` / `max_ahead_seconds` / `watchdog_poll_seconds`; phase margin = 2×poll). The sliding ceiling (`max_ahead_seconds`, default 1200s) is a panic fuse validated at startup against every phase budget. `current_dispatch_deadline` ContextVar + `renew_dispatch_deadline()` helper |
| `models.py` | Core data models: `TurnIdentity`, `ToolArguments`, `ApprovalRequestState`, `ApprovalTransaction`, `ToolBatchState`, `TurnStateBase`, `TurnSnapshot`, `TurnSummary`, `StateQueryScope`, and `MessageDelta`. |
| `enums.py` | Enumerations — `StateScope`, `AgentKind`, `TurnPhase`, `OperationKind`, `ToolBatchStatus`, `ToolCallStatus`, `ApprovalDenyPolicy`, `ApprovalSubjectType`, `OperationStatus`, `CancellationSource`, `SnapshotReason`, `MessageDeltaSource`, `TurnCustomKey` |
| `policy.py` | `SnapshotPolicy` ABC — defines when/how snapshots are taken during agent execution |
| `codec.py` | `RuntimeStateCodec` ABC + `RuntimeStateCodecRegistry` — serialization extensibility for runtime state |
| `approval_decision.py` | `ApprovalDecisionCoordinator` ABC + typed approval audit entry/decision models for atomic snapshot-and-audit persistence |
| `dream_locks.py` | Dream lock primitives — coordination locks for the dream engine's concurrent scan phases |

## Timeout Architecture

One watchdog, phase-budget declarations (see `dispatch.py` module docstring for the authoritative protocol table):

1. **Provider HTTP timeout** (`request_timeout` / `stream_idle_timeout`) — defaults to `None` (no provider-level timeout). The provider waits indefinitely for the LLM.
2. **Phase-budget declarations** — every phase owning an inner deadline declares its full budget into the `DispatchDeadline` at entry (`renew(own_budget + margin)`; floor semantics, never shortens): tool calls (`ToolTimeoutInterceptor` entry), hook dispatches (`HookRunner.dispatch` entry), turn tail (`turn_runner` finally: flush + session-end + margin). The LLM call re-asserts `dispatch_timeout` at call entry; react stream chunks and external provider events renew by `chunk_renew_seconds` as activity signals.
3. **Pool watchdog** (polls `DispatchDeadline.is_expired` at `DeadlinePolicy.watchdog_poll_seconds`) — the **sole termination mechanism**. Kills the turn only on genuine no-progress; it never races an inner deadline (phase margin = 2×poll interval guarantees the inner graceful path fires first).

`DeadlinePolicy` (on `RuntimeSafetyPolicy.deadline`) holds `chunk_renew_seconds` / `max_ahead_seconds` / `watchdog_poll_seconds`; a startup model validator enforces `max_ahead_seconds >= every phase budget + margin`, so the sliding ceiling is a panic fuse, not a behaviour knob.

**Design intent**: as long as there is ongoing activity (chunks arriving, phases declaring budgets), the turn never times out. The watchdog only fires when nothing has renewed the deadline for longer than its remaining budget.

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
- `modex_agent.core.session_id` — `SessionInfo` identity used by runtime state
- `modex_agent.core.tool_manager` — `ToolResult` and tool execution values
- `modex_agent.hook` — `HookRunner` for lifecycle hooks (injected via `AgentRuntimeServices`)
- `modex_agent.interceptor` — `InterceptorChain` for AOP interception (injected via `AgentRuntimeServices`)
- `modex_agent.control` — `InMemoryControlChannel` + `ControlCommand`/`ControlScope` (live control-plane types)
