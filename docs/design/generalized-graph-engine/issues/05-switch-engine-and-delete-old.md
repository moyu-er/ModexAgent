# 05 — ReAct switches to `modex_graph` engine + delete old `core/graph/`

**What to build:** Switch ReAct's runtime from the old `src/modex_agent/core/graph/` engine to the new `modex_graph` package. `ReActAgent.run()` constructs a new `Graph` (via `build_react_graph().compile()`), a new `GraphEngine`, and a new `GraphContext` carrying `ReactGraphRuntime` + `user_data=agent_ctx`. The old `src/modex_agent/core/graph/` directory is deleted. An architecture guard test enforces no `modex_agent` file imports `modex_agent.core.graph` after the deletion. ReAct full regression must be green.

**Blocked by:** 04 — node code must use `ctx.runtime.*` AOP and `ctx.state.x` access patterns, ready for the new `GraphContext` to provide them.

**Status:** completed (commit 8045db1b)

## Acceptance criteria

- [ ] `build_react_graph()` function exists in `modex_agent/agents/react/graph.py`, returns a `Graph[ReActTurnState]` with 4 nodes (`StartNode` / `LLMNode` / `ToolNode` / `EndNode`) and the 7 static edges from the existing ReAct topology (NORMAL_START / RESUME_TOOLS / HAS_TOOLS / NO_TOOLS / MAX_ITERATIONS / LLM_ERROR / TOOLS_DONE / TURN_CANCELLED)
- [ ] Node names use `ReActNode` StrEnum values (START / LLM / TOOL / END); transition reasons use `ReActReason` StrEnum values
- [ ] `Graph.compile(max_iterations=...)` is called with the configured max_iterations value
- [ ] `ReActAgent.run()` constructs: `runtime = ReactGraphRuntime(hooks, interceptors, governance, control_channel, snapshot_policy, turn_state_store, emitter)`; `graph = build_react_graph().compile(max_iterations=...)` (engine-level safety net, larger than business max); `engine = GraphEngine(graph)`; `graph_ctx = ReActGraphContext(state=ReActTurnState(...), runtime=runtime, user_data=agent_ctx)`
- [ ] `ReActAgent.run()` calls `await engine.run_async(graph_ctx)` (replacing the old `engine.run(ctx)` call)
- [ ] `ReActAgent.run()` reads `state.result` from the returned state (typed `AgentResult | None`)
- [ ] **`run_async` re-entry semantics**: `engine.run_async(ctx)` always starts from the entry node. On resume after `GraphInterrupt`, `ReActAgent.run()` re-enters `engine.run_async(ctx)` with the updated state (approval decisions applied externally). `StartNode` detects suspended state (`ctx.state.approval is not None` or `ctx.state.current_node != START`) and routes to TOOL via `ReActReason.RESUME_TOOLS`. The engine itself is stateless across `run_async` calls — no internal "resume context".
- [ ] **`max_iterations` two layers preserved**: (1) engine-level `compile(max_iterations=N)` raises `GraphRecursionError` if exceeded (abnormal exit, safety net); (2) node-level `LLMNode` checks `ctx.state.iteration >= business_max` and returns `transition=ReActReason.MAX_ITERATIONS` → static edge to END → `EndNode` assembles `AgentResult(stop_reason=MAX_ITERATIONS)` (graceful exit, business logic). `compile(max_iterations=N)` should be set larger than business max.
- [ ] Turn-level hooks (`BEFORE_TURN` / `AFTER_TURN` / `FINALLY_TURN`) remain in `ReActAgent.run()` — they are NOT engine-auto-invoked (they are turn-level, not iteration-level)
- [ ] `GraphInterrupt` propagation: when `ctx.interrupt(tx)` raises inside a node, `GraphEngine.run_async` propagates it (does not swallow); `ReActAgent.run()` catches it for approval persistence + resume (same external behavior as before)
- [ ] Resume path: external approval decision path works end-to-end — `ApprovalRenderer` detects suspend → `apply_decision` mutates `ApprovalTransaction.decisions` dict (mutable, not frozen) from `PENDING` to `ALLOWED`/`DENIED` → `_normalize_batch_decisions` rewrites `ALLOWED` to `PREEMPTED` for atomicity (ADR-0011) → `replace_approval` persists updated transaction into snapshot via `state.checkpoint()` re-checkpoint → `ApprovalResumer` re-enters `engine.run_async(ctx)` with updated state → `StartNode` detects resume → routes to TOOL → `ToolNode._resume_suspended_batch` reads `ctx.state.approval.decisions` (mutated externally) → executes pre-approved tools, returns errors for denied tools. **Full approval state machine preserved.**
- [ ] Old `src/modex_agent/core/graph/` directory deleted entirely (engine.py / graph.py / node.py / constants.py / interrupt.py / __init__.py / AGENTS.md)
- [ ] Architecture guard test enforces: no file under `src/modex_agent/` imports `modex_agent.core.graph` (grep-based check)
- [ ] All ReAct unit tests pass (with mocks updated to construct new `GraphContext` / `ReactGraphRuntime` instead of old `AgentContext.runtime` patterns)
- [ ] All ReAct integration tests pass
- [ ] Approval suspend/resume full cycle works end-to-end on the new engine
- [ ] `max_iterations` / `turn_cancelled` / `llm_error` exit paths work on the new engine
- [ ] Snapshot serialize/deserialize round-trip works on the new engine (using the simplified `ReActSnapshotPolicy` from ticket 03)
- [ ] Loop detection hook (ADR-0016) still fires correctly on the new engine — `LoopDetectionHook` is registered on `HookRunner`, dispatched via `ReactGraphRuntime.dispatch_hook(ReActHookPoint.AFTER_LLM_RESPONSE, ctx)`; the hook raises `LoopDetectedError` which propagates through `ReActAgent.run()`'s `except AgentControlError` block unchanged
- [ ] `AgentPipeline` / `ReActTurnRunner` / `ExecutionStrategy` (ADR-0025) interfaces unchanged — the engine switch is internal to `ReActAgent.run()`
- [ ] `ExternalCodingAgent` is NOT migrated — it remains a subprocess streaming harness using `ExternalTurnRunner` directly
- [ ] No behavior change visible to `AgentPipeline` or any external ReAct consumer — the engine switch is fully encapsulated
