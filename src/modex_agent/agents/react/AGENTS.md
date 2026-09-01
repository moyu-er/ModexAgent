<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-31 -->

# react

## Purpose

Graph-based ReAct agent runtime. Owns the turn loop, LLM calls, tool execution,
approval suspend/resume, and integration points for hooks, interceptors, and control.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `ReActAgent(Agent[ReActEvent])` — event enum, turn context setup, constructs `Graph` via `build_react_graph().compile()`, wraps in `GraphEngine`, executes via `engine.run_async(ReActGraphContext(...))`. |
| `graph.py` | `build_react_graph()` — builds `Graph[ReActTurnState]` (from `modex_graph`) with 6 ReAct nodes + 11 edges (8 total with engine sentinels). |
| `context.py` | `ReActGraphContext(GraphContext[ReActTurnState])` — type-safe accessors (`agent_ctx`, `tool_manager`, `context_manager`). |
| `runtime.py` | `ReactGraphRuntime(GraphRuntime)` — AOP bridge mapping ReAct StrEnums to framework enums, bridging `GraphContext.user_data` → `AgentContext`. |
| `state.py` | `ReActTurnState(GraphState)`, `ReActSnapshotPolicy`, and `ReActRuntimeStateCodec`. |
| `builder.py` | `ReActAgentBuilder` -- `build_agent()` + `build_emitter_factory()` from `AgentDescriptor`. |
| `approval.py` | *(removed — migrated to `modex_agent.approval.runtime`)* |
| `llm_client.py` | `ReactLlmClient` — the ReAct LLM caller; single event loop (ADR-0046): `call()` drives one pass over `provider.stream(request)` for every provider — stream-native providers yield real event streams, `CallbackStreamProvider` implementations ride the callback→event bridge (LLMStreamEvents fed into an `EventAssembler`). The LLM_STREAM-scope interceptor chain wraps the event iterator (events in, events out). Emitter driving happens at the event dispatch point, gated on `emitter.wants_streaming()`. Mid-stream interrupt stashes `INTERRUPTED_PARTIAL` into turn state; a bridge-translated `Finish(CANCELLED)` re-enters the lifecycle as `CancelledError`. |
| `error_recovery.py` | Provider error recovery for `ReactLlmClient` — on context-length / payload-too-large errors applies emergency compaction (drop middle messages, keep system + recent tail) and retries with the trimmed list. |
| `media_injection.py` | `inject_multimodal(messages, ctx)` — copy-on-write resolution of persisted `media://` parts at the LLM boundary (input history never mutated). Per-part modality gate (`content_part_modality` — unsupported modalities drop with ERROR); two-pass budget (framework constants: 8 parts / 6MB decoded bytes, oldest-first offload to `[media offloaded: <aid>]` placeholders); `media://` resolution via `runtime.services.media_store` with a `(id(store), session_id, aid)` weakref-guarded cache; no store / missing / corrupt bytes degrade to `[media unavailable: <aid>]` placeholders with ERROR — a broken image never fails the LLM call. |
| `message_builder.py` | Pure helpers constructing `ChatMessage` structs (tool results, assistant content) — no sanitization logic; callers own thinking-chain cleanup. |
| `injection_drainer.py` | `InjectionDrainer` — consumes the per-turn injection queue into history (extracted from `ReActAgent._drain_injections`). |
| `tool_executor.py` | `ToolExecutor` — runs a tool call through the interceptor chain with the mandatory `ToolTimeoutInterceptor` composed innermost (per-invocation deadline on every ReAct path; the interceptor also declares `tool_timeout + margin` into the dispatch deadline at entry — watchdog phase-budget protocol, see `runtime/dispatch.py`). |
| `tool_dedup.py` | `ToolCallDeduplicator` tracks cross-step streaks with escalating `<system-reminder>`s; same-step duplicates are pruned during scheduling in `ToolNode`, and `check_same_step` has been removed. It skips at `_STREAK_SKIP=8`, force-cancels the turn at `_STREAK_STOP=12`, and is instantiated once per turn. |
| `constants.py` | `ReActNode`, `ReActHookPoint` (11 values: iteration-level + turn-attempt `BEFORE_TURN`/`AFTER_TURN` + node-level `START_NODE_TURN`/`END_NODE_TURN`), `ReActScope`, `ReActEvent`, `InterruptReason` (B1) StrEnums, plus the frozen `ToolCallEndPayload` event model. |
| `nodes/start.py` | `StartNode` -- routes to BEFORE (fresh) or TOOL (resume from approval). Dispatches `START_NODE_TURN` hook on fresh-turn path only (not on resume). |
| `nodes/before_turn.py` | `BeforeTurnNode` -- increments `turn_attempt`, resets `iteration = 0`, dispatches `BEFORE_TURN` hook, routes to LLM. |
| `nodes/llm.py` | `LLMNode` -- calls LLM (re-asserting `dispatch_timeout` into the dispatch deadline at call entry), enforces the business max-iterations gate (`state.iteration > agent_ctx.max_iterations` → AFTER, the sole iteration cap — ReAct sets no engine limit), dispatches hooks/interceptors via `ctx.runtime.*`, emits iteration events. `_build_messages()` applies governance, then coerces to `list[ChatMessage]` and returns `inject_multimodal(typed, ctx)` — media resolution happens after governance, before the provider. |
| `nodes/tool.py` | `ToolNode` classifies and suspends calls for approval, prunes same-step duplicates, then schedules approved calls as PARALLEL segments and EXCLUSIVE barriers. A bounded rolling pool settles through `completion_queue`; per-call state and `TOOL_CALL_END` follow completion order, while the commit cursor writes history, `message_delta`, and hook results in model order. Channel cancellation and streak STOP share synthesized cancellation and `on_cancel`; internal G3 failures drain started workers, mark FAILED, and rethrow without synthesized results. |
| `nodes/after_turn.py` | `AfterTurnNode` -- constructs `AgentResult`, writes `state.result`, dispatches `AFTER_TURN` hook (with `{"result": result}` payload), then consumes `CONTINUATION_REQUEST` + `CONTINUATION_RENEW_MAX_TURNS` one-shot flags to decide continuation. Routes to BEFORE/END. Watchdog: when RENEW is set and `turn_attempt >= MAX_TURNS`, gate increments `MAX_TURNS` by 1. Default `MAX_TURNS` is 3. No hardcoded deliver-reminder (migrated to `DeliverRetryHook`). |
| `nodes/end.py` | `EndNode` -- reads `state.result` (raises `RuntimeError` if None), emits completion events, dispatches `END_NODE_TURN` hook. |

## Graph Edges

Edges are plain topology — nodes route at runtime via `deliver()`.

```
START → BEFORE
START → TOOL
BEFORE → LLM
LLM   → TOOL
LLM   → AFTER
TOOL  → LLM
TOOL  → AFTER
AFTER → END
AFTER → BEFORE
END   → GraphNode.END
```

## Data Flow (Deliver-ization)

The six ReAct nodes do not hand off data through shared `ctx.state`
fields. `ReActTurnState.llm_response` — formerly the sole inter-node
hand-off between `LLMNode` and `ToolNode`/`AfterTurnNode` — was deleted
(tasks 22+23). Data flows through three distinct channels:

- **`agent_ctx.history`** — the sole persistent context across nodes.
  `ToolNode` reads the last assistant message (and its `tool_calls`)
  from `await agent_ctx.history.to_list()` (async API — sync access
  raises in pool mode). `LLMNode` appends assistant messages here.
  History survives across the turn; nodes append, later nodes read.
- **`deliver()`** — a routing signal only (default content `None`).
  `LLMNode` delivers `None` to TOOL/AFTER; `AfterTurnNode` delivers
  `None` to BEFORE/END; receivers read `ctx.state` / history, not the
  payload. The one exception: LLM infrastructure errors arrive as a
  `{"error": text}` deliver payload to AFTER, which sets
  `state.phase = FAILED` and uses the error text.
- **`ctx.state`** — the per-turn lifecycle workspace only.
  `state.message_delta` holds the final assistant `ChatMessage`
  (content read by `AfterTurnNode`); `state.phase` marks the node's own
  lifecycle; `state.result` holds the assembled `AgentResult`. These
  are turn-local, not inter-node data channels.

`LLMNode` uses a `nonlocal response` closure variable (declared in
`execute()` scope) instead of `state.llm_response` to carry the LLM
output out of the `actual_iteration()` closure — no state read-back
needed. `reasoning_content` (with `reasoning_signature` /
`reasoning_item_id` / `reasoning_encrypted_content`) are declared
`ChatMessage` fields (ADR-0046), accessed via plain attribute reads.

This resolves the "ReAct shared-state communication" debt recorded in
ADR-0034 D7. Hook timing is unchanged — deliver-ization rewired data
flow, not dispatch points (see `tests/unit/agents/react/test_hook_timing.py`).

## Runtime Modes

- **clean**: plain ReAct graph, no hooks/interceptors/approval/control/state-store.
- **full**: all services wired through `AgentRuntimeServices`. Runtime assembly lives in
  `AgentPipeline` / `TurnContextBuilder` (the old `RuntimeAssembler` was removed as dead
  code); the approval runtime is built by `ioc.factories.approval.build_approval_runtime`
  and injected via `AgentPipeline.runtime_services`.

## Approval Flow

```
ToolNode._classify_all() -> TieredToolApprovalClassifier
  -> PENDING: ctx.runtime.capture_snapshot() -> ctx.interrupt(tx) -> GraphInterrupt
  -> Pipeline: ApprovalRenderer.detect() -> apply_decision() (mutates ApprovalTransaction.decisions)
  -> replace_approval() persists updated transaction into snapshot
  -> StartNode detects SUSPENDED -> routes to TOOL -> _resume_suspended_batch()
  -> reads ctx.state.approval.decisions -> ALLOWED tools execute, DENIED return error
```

Deny policy: default `TOOL_RESULT_ONLY` (loop continues); override to `CANCEL_TURN` to abort.

## Key Invariants

- `AgentRuntime` / `AgentRuntimeServices` are assembled by `AgentPipeline` /
  `TurnContextBuilder` (per turn). The approval runtime is constructed by
  `ioc.factories.approval.build_approval_runtime` and wired onto the main pipeline via
  `AgentPipeline.runtime_services` (a mirror property) — see ADR-0008.
- ReAct runs on the `modex_graph` engine (ADR-0033). `ReActAgent.run()` constructs
  `Graph` + `GraphEngine` + `ReActGraphContext` per turn and calls `engine.run_async()`.
  Hooks follow a 4-level hierarchy: graph-level (`BEFORE_GRAPH`/`AFTER_GRAPH`/`FINALLY_GRAPH`)
  and `around_turn` interceptor remain in `ReActAgent.run()`'s `actual_turn()`; node-level
  (`START_NODE_TURN`/`END_NODE_TURN`) dispatched in `StartNode`/`EndNode`; turn-attempt
  (`BEFORE_TURN`/`AFTER_TURN`) dispatched in `BeforeTurnNode`/`AfterTurnNode`; iteration-level
  dispatched in `LLMNode`/`ToolNode`. `BeforeTurnNode` dispatches `BEFORE_TURN` hook;
  `AfterTurnNode` dispatches `AFTER_TURN` hook and no longer injects deliver-reminder
  (migrated to `DeliverRetryHook`).
- **turnId / trace_id future consideration**: `turn_uuid` is generated in the pipeline layer
  (`TurnRunner`), and `trace_id` is generated in `TraceCollectorHook.before_graph()`. Both
  fire once per `actual_turn()` call, so approval resume (which re-enters `actual_turn()`)
  generates a new trace root. A future improvement could move `trace_id` generation to
  `START_NODE_TURN` so approval-resume continues the same trace. This is deferred because it
  involves `TraceCollectorHook` span lifecycle redesign.
- Node-level AOP (hooks, interceptors, governance, control drain, snapshot, emit) is
  routed through `ReactGraphRuntime` via `ctx.runtime.*`. Node-level, turn-attempt, and
  iteration-level hooks are all dispatched explicitly by nodes via
  `ctx.runtime.dispatch_hook(ReActHookPoint.X, ctx)`, NOT engine-auto-invoked
  (preserves hook timing exactly). Graph-level hooks bypass `HOOK_POINT_MAP` — they
  are dispatched in `actual_turn()` via `hook_runner.dispatch(HookPoint.X, agent_ctx)`
  directly.
- Per-turn state lives in `ctx.state` (`ReActTurnState`, a mutable `GraphState`).
  `ctx.state.result` holds the final
  `AgentResult` (replaces the old `custom[GRAPH_RESULT]` pattern).
- Approval does NOT go through interceptors; it is handled at the `ToolNode`/pipeline
  layer via `ctx.interrupt(tx)` → `GraphInterrupt` → `TurnSnapshot`.
- `ApprovalTransaction` / `ToolBatchState` / `ToolCallState` are mutable `BaseModel`
  (NOT frozen) — the approval state machine mutates `decisions` dict and `status`/
  `decision` fields at runtime (ADR-0033 D14).
- Control: `ctx.runtime.drain_control(ctx)` is called at safe points (LLMNode, ToolNode)
  to check for `CANCEL_TURN`.
