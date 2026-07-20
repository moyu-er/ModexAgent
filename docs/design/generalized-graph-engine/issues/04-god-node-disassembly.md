# 04 — God node disassembly + AOP migration to `ReactGraphRuntime`

**What to build:** Disassemble the ReAct god nodes (`LLMNode` / `ToolNode` / `EndNode` / `StartNode`) by moving all AOP code (hook dispatch, control drain, governance, interceptor around, snapshot capture, event emission) from inline node logic into `ctx.runtime.*` calls that route through `ReactGraphRuntime`. After this ticket, node bodies contain only node-specific business logic (LLM call, tool execution, result assembly). Iteration-level hooks (`BEFORE_ITERATION` / `AFTER_ITERATION`) remain as explicit `ctx.runtime.dispatch_hook(...)` calls in `LLMNode` at the same code points as today — they are NOT engine-auto-invoked (the `GraphRuntime` ABC has no `before_iteration`/`after_iteration` methods), so hook timing is preserved by construction and no parity test is needed. ReAct still uses the old `core/graph/` engine; only node code is refactored.

**Blocked by:** 03 — `ReActTurnState` must be a `GraphState` with working checkpoint, and `ReactGraphRuntime` must exist (from ticket 02) ready to be wired in.

**Status:** ready-for-agent

## Acceptance criteria

- [ ] `LLMNode.execute(ctx)` no longer directly calls `runtime.hooks.dispatch(HookPoint.BEFORE_ITERATION, ctx)` / `AFTER_ITERATION` / `AFTER_LLM_RESPONSE` — these are now all node-explicit calls via `ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx)` / `AFTER_ITERATION` / `AFTER_LLM_RESPONSE` (iteration hooks are NOT engine-auto-invoked; they remain node-controlled at the same code points as today)
- [ ] `LLMNode.execute(ctx)` no longer directly calls `drain_control_channel(...)` — replaced with `ctx.runtime.drain_control(ctx)`
- [ ] `LLMNode.execute(ctx)` no longer directly calls `governance.apply(messages)` — replaced with `ctx.runtime.apply_governance(messages, ctx)`
- [ ] `LLMNode.execute(ctx)` no longer directly calls `runtime.interceptors.around_iteration(...)` — replaced with `ctx.runtime.around(ReActScope.ITERATION, ctx, body)`
- [ ] `LLMNode.execute(ctx)` no longer directly calls `ctx.emitter.emit(ReActEvent.X, ...)` — replaced with `ctx.runtime.emit(ReActEvent.X, ..., ctx)`
- [ ] `ToolNode.execute(ctx)` AOP code (drain_control, BEFORE_TOOL_EXECUTION / AFTER_TOOL_EXECUTION dispatch, emit TOOL_CALL_START / TOOL_CALL_END) all moved to `ctx.runtime.*` calls
- [ ] `ToolNode.execute(ctx)` approval suspend changes from `raise GraphInterrupt(snapshot)` to `ctx.runtime.capture_snapshot(ctx, "approval_suspend"); ctx.interrupt(tx)` (where `tx` is the `ApprovalTransaction`); the old explicit snapshot construction moves into `ReactGraphRuntime.capture_snapshot`. **`ApprovalTransaction` is mutable (not frozen)** — the external `apply_decision` path mutates `tx.decisions[call_id]` from `PENDING` to `ALLOWED`/`DENIED`, and `_normalize_batch_decisions` may rewrite `ALLOWED` to `PREEMPTED` for atomicity (ADR-0011). This mutability is preserved.
- [ ] `ToolNode.execute(ctx)` resume path reads `ctx.state.approval` (mutable `ApprovalTransaction`, updated by external approval decision via `apply_decision` → `replace_approval`) instead of querying `ctx.runtime.state.approval` — `ctx.state.approval.decisions` dict is read directly, same as today
- [ ] `EndNode.execute(ctx)` event emission moved to `ctx.runtime.emit(ReActEvent.FINAL_OUTPUT, ..., ctx)` / `ctx.runtime.emit(ReActEvent.ERROR, ..., ctx)`
- [ ] `StartNode.execute(ctx)` event emission moved to `ctx.runtime.emit(ReActEvent.START, ctx)`
- [ ] All ~30 sites of `ctx.runtime.state.x = y` changed to `ctx.state.x = y` (mechanical rename)
- [ ] All `ctx.runtime.state` reads changed to `ctx.state` reads
- [ ] `ctx.runtime.state.custom[KEY]` accesses changed to `ctx.state.custom[KEY]` (the `custom` dict is still part of `TurnStateBase` / `GraphState`)
- [ ] `before_iteration` / `after_iteration` hook dispatch remains in `LLMNode.execute` as explicit `ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx)` / `AFTER_ITERATION` calls at the same code points as today — **NOT engine-auto-invoked** (the `GraphRuntime` ABC has no `before_iteration`/`after_iteration` methods). This preserves hook timing exactly by construction; no hook timing parity test is needed because the dispatch sites are unchanged.
- [ ] All existing ReAct unit tests pass unchanged (hook timing is preserved by construction since dispatch sites are node-controlled)
- [ ] All existing ReAct integration tests pass unchanged
- [ ] Approval suspend/resume full cycle test passes: tool call triggers approval → `ctx.interrupt(tx)` raises `GraphInterrupt` → external decision applied → resume re-enters → pre-approved tools execute, denied tools return error
- [ ] `max_iterations` exit path works: LLMNode returns `transition=ReActReason.MAX_ITERATIONS` → static edge to END → `EndNode` assembles result
- [ ] `turn_cancelled` exit path works: ToolNode returns `transition=ReActReason.TURN_CANCELLED` → static edge to END
- [ ] `llm_error` exit path works: LLMNode returns `transition=ReActReason.LLM_ERROR` → static edge to END
- [ ] ReAct still uses old `src/modex_agent/core/graph/` engine for execution — node code is refactored but engine is unchanged; this ticket prepares nodes for the engine switch in ticket 05
- [ ] **`ReActGraphContext(GraphContext[ReActTurnState])` subclass defined** in `modex_agent/agents/react/context.py` with type-safe accessors: `agent_ctx` (returns `AgentContext` from `user_data`), `tool_manager`, `context_manager`, and other commonly-accessed `AgentContext` fields. Nodes use `ctx.tool_manager` instead of `cast(AgentContext, ctx.user_data).runtime.services.tool_manager`.
- [ ] `ReActAgent.run()` constructs `ReactGraphRuntime` and passes it through `AgentContext` so nodes can access `ctx.runtime.*` (still via the old engine's `AgentContext.runtime` indirection until ticket 05 switches to `ReActGraphContext`)
