# 02 — `ReactGraphRuntime` adapter + ReAct state types → Pydantic

**What to build:** The ReAct-side adapter layer that bridges `modex_graph`'s `GraphRuntime` ABC to `modex_agent`'s existing AOP services (`HookRunner` / `InterceptorChain` / `ControlChannel` / `SnapshotPolicy` / `TurnStateStore` / `ContentEmitter`). Plus the Pydantic migration of five ReAct state types that currently block the universal channel codec. After this ticket, the adapter and codec exist but are not yet wired into ReAct's runtime — ReAct still uses the old `core/graph/` engine. The new code is unreferenced but available for Stage 03 to consume.

**Blocked by:** 01 — `modex_graph` package must exist with `GraphRuntime` ABC + `register_codec` API + `Codec` type.

**Status:** completed (commit ab1543b2)

## Acceptance criteria

- [ ] `ReactGraphRuntime(GraphRuntime)` class exists in `modex_agent/agents/react/runtime.py`
- [ ] `ReactGraphRuntime` maps business `StrEnum` values to `modex_agent` enums: `ReActHookPoint` → `HookPoint`, `ReActScope` → `InterceptorScope`, `ReActEvent` → existing `ReActEvent` enum
- [ ] `ReactGraphRuntime` implements all 8 `GraphRuntime` methods (2 engine-auto: `before_node`/`after_node` + 6 node-explicit: `dispatch_hook`/`around`/`apply_governance`/`drain_control`/`capture_snapshot`/`emit`); constructor accepts `hook_runner`, `interceptor_chain`, `governance`, `control_channel`, `snapshot_policy`, `turn_state_store`, `emitter`
- [ ] **`ReactGraphRuntime` does NOT implement `before_iteration`/`after_iteration`** — these are NOT on `GraphRuntime` ABC. ReAct nodes dispatch `BEFORE_ITERATION`/`AFTER_ITERATION` explicitly via `ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx)` at the same code points as today. This preserves hook timing exactly (eliminates the highest migration risk)
- [ ] **`ReactGraphRuntime` bridges `GraphContext` to `AgentContext` for underlying services**: all methods receive `GraphContext` but extract `ctx.user_data` (which holds `AgentContext`) and pass it to `hook_runner.dispatch` / `interceptor_chain.around_*` / `governance.apply` / `drain_control_channel`. Hook implementations receive `AgentContext` unchanged — they are completely unaware of the migration
- [ ] `ReactGraphRuntime.around(scope, ctx, body)` maps `scope` string to the correct interceptor method (`around_iteration` / `around_llm_call` / `around_llm_stream` / `around_tool_call`) and constructs the typed interceptor context (`IterationContext` / `LLMCallContext` / `LLMStreamContext` / `ToolCallContext`) from `ctx.user_data` (AgentContext) internally; `body` is a zero-arg awaitable closure
- [ ] `ReactGraphRuntime.dispatch_hook(hook_point, ctx, data)` wraps `data: dict` into `HookPayload(data=data)` when calling `hook_runner.dispatch` — preserves existing hook payload contract
- [ ] `ReactGraphRuntime.capture_snapshot(ctx, reason)` calls `SnapshotPolicy.capture(state, SnapshotReason(reason))` + `TurnStateStore.save_turn(snapshot)`
- [ ] `ReactGraphRuntime.drain_control(ctx)` calls existing `drain_control_channel` helper with `ctx.user_data` (AgentContext)
- [ ] `ReactGraphRuntime.emit(event_type, data, ctx)` maps event_type string to `ReActEvent` enum and calls `emitter.emit`
- [ ] New `ReActHookPoint`, `ReActScope`, `ReActEvent` `StrEnum` classes defined in `modex_agent/agents/react/constants.py` (alongside existing `ReActNode` / `ReActReason`)
- [ ] `ReActHookPoint` includes: `BEFORE_ITERATION`, `AFTER_ITERATION`, `AFTER_LLM_RESPONSE`, `BEFORE_TOOL_EXECUTION`, `AFTER_TOOL_EXECUTION`, `FINALIZE_CONTENT` (NOT `BEFORE_TURN`/`AFTER_TURN`/`FINALLY_TURN` — those are turn-level, dispatched in `ReActAgent.run()` directly)
- [ ] `ReActScope` includes: `ITERATION`, `LLM_CALL`, `LLM_STREAM`, `TOOL_CALL` (NOT `TURN` — `around_turn` is dispatched in `ReActAgent.run()` directly)
- [ ] `ReActEvent` includes all existing ReAct event values: `START`, `MAX_ITERATIONS`, `MODEL_OUTPUT`, `TOOL_CALL_START`, `TOOL_CALL_END`, `ITERATION_END`, `PROGRESS`, `FINAL_OUTPUT`, `ERROR`
- [ ] **`ApprovalTransaction` migrated from `@dataclass` to Pydantic `BaseModel` (NOT frozen)** — the approval state machine mutates `decisions` dict externally (`apply_decision` updates `approval.decisions[call_id]` from `PENDING` to `ALLOWED`/`DENIED`; `_normalize_batch_decisions` may rewrite `ALLOWED` to `PREEMPTED` for atomicity per ADR-0011). Frozen would break the state machine.
- [ ] `ApprovalRequestState` migrated to `BaseModel` (NOT frozen, for consistency with `ApprovalTransaction`)
- [ ] `ToolBatchState` migrated to `BaseModel` (NOT frozen — `status` transitions `WAITING`→`COMPLETED`/`FAILED`/`CANCELLED` during execution)
- [ ] `ToolCallState` migrated to `BaseModel` (NOT frozen — `decision` transitions `PENDING`→`ALLOWED`/`DENIED`/`PREEMPTED`; `status` transitions during execution; `result` set after tool execution)
- [ ] `ToolArguments` migrated to `BaseModel(frozen=True)` (truly immutable leaf value-object — just a typed wrapper around tool call arguments, never mutated)
- [ ] Channel codec registrations in `modex_agent/agents/react/codec.py` use `register_codec(Type, Codec(encode=lambda v: v.model_dump(mode="json"), decode=lambda d: Type.model_validate(d)))` for each of the 5 migrated types
- [ ] All existing ReAct tests pass unchanged (state type migration is behavior-preserving — same fields, same construction, same access patterns; mutable types remain mutable)
- [ ] All existing snapshot tests pass unchanged (the 5 migrated types still serialize correctly through the old `ReActSnapshotPolicy` path; Pydantic `model_dump()` output is JSON-compatible with the existing payload structure)
- [ ] Approval state machine tests pass: `apply_decision` can mutate `ApprovalTransaction.decisions` dict; `_normalize_batch_decisions` can rewrite `ALLOWED` to `PREEMPTED`; `ToolCallState.decision`/`status` can be updated during execution
- [ ] ReAct still uses old `src/modex_agent/core/graph/` engine — `ReactGraphRuntime` and codec registrations exist but are not referenced by ReAct runtime code yet
