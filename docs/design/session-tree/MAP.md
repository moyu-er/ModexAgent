# Wayfinder Map: SessionTree

> Label: `wayfinder:map`
> Status: **Active — validated + corrected (2026-08-11)**

## Destination

A design spec for **SessionTree**: a unified, persistent message-dispatch/consume lifecycle tree that replaces the current fragmented inbox/poller/subagent-tracking model. Tree ID = main agent session ID (isolated by workspace+pool). All communication tools (task / send_to_agent / modexctl send) converge on `tree.deliver`. The spec covers: tree data model, node version chain, persistence, inbox linkage, integration points, and the modex_graph reuse decision. Implementation order: tree first → bot integration (all comm tools) → graph integration last.

## Notes

- **Domain**: multi-agent framework (modex_agent) + standalone graph engine (modex_graph). Reference: `.references/opencode/` for TurnCompletionWaiter / session tree patterns.
- **Skills every session should consult**: `/domain-modeling` (tree is a domain concept needing canonical terms), `/grilling` (every ticket is a decision, not a build slice).
- **Standing preferences**:
  - Tree manages subagent lifecycle only — peer agents are separate trees (peer = another main agent, out of scope; **peer completely unrelated to sender's tree**).
  - Graph context disables peer communication; normal chat enables it.
  - Tree does NOT handle internal pipeline — only node lifecycle + dispatch/consume state + dispatch lifecycle hooks.
  - Tree node = sessionId, 1:1 binding. InboxFlushHook is the ONLY consumer; it does not create tree nodes.
  - External agent (opencode) has its own TurnCompletionWaiter — tree tracks external subagent completion via SubagentAutoSendHook, not via internal waiting.
- **Tree binds to inbox dispatch mechanism (InboxPoller), NOT to hooks/react**. Lifecycle callbacks (`on_dispatch_start`/`on_dispatch_end`) in InboxPoller's `_dispatch_batch` + `_end_dispatch`. No success parameter — any complete dispatch exit = COMPLETED. Covers all agent types (ReAct + external) since all go through InboxPoller.
- **tree status must be maintained** (ACTIVE/COMPLETED/CANCELLED at tree level). Tree does NOT track all node states (may be provided later).
- **quiesce = no DISPATCHED tracks + no running nodes** (SQL + in-memory, NOT inbox.count).
  - **Two orthogonal dimensions**: agent implementation type (native/external) × message/topology type (TASK_REQUEST/AGENT_RESULT/AGENT_MESSAGE/EXTERNAL_INPUT). Tree only cares about message type.
- **Predecessor docs**: ADR-0039 (Turn Context Configuration Pipeline — partially superseded by this effort), Deliberation 0003, P0-6 (`docs/design/static-graph-scheduling/todo.md`), ADR-0038 (graph node agent context injection).

## Validation (2026-08-11)

Code audit via 6 parallel explore subagents validated all design decisions against the actual codebase. Seven corrections were identified and merged into the affected issue docs:

1. **`message_id` already preserved** (T04 corrected): `InboxProducer.send` (producer.py:84) already passes `message_id=envelope.message_id`. No fix needed.
2. **`on_consumed` belongs in `InboxConsumer`** (T05 corrected): `InboxFlushHook._flush` bypasses `pool.consume_inbox` and calls `InboxConsumer.consume` directly. Placing `on_consumed` in `pool.consume_inbox` would miss fold-in consumes → track deadlock.
3. **Quiesce does NOT check `inbox.count`** (T01/T02 corrected): peer messages in inbox cause false non-quiesce. Correct: no DISPATCHED tracks + no running nodes (SQL + in-memory).
4. **TASK_REQUEST tracks NOT closed on consume** (T01/T05 corrected): closing on consume creates a brief quiesced window before AGENT_RESULT arrives. Correct: close on AGENT_RESULT deliver (by invocation_id) or on_dispatch_end fallback.
5. **Dispatch lifecycle callbacks in InboxPoller, NOT hooks** (T01/T05 corrected): `on_dispatch_start`/`on_dispatch_end` in InboxPoller's `_dispatch_batch` entry + `_end_dispatch` shared cleanup. Tree binds to inbox dispatch mechanism, NOT ReAct hooks. No success parameter — error exit = COMPLETED, tree stays ACTIVE. fold-in doesn't trigger these (not through _dispatch_batch).
6. **Convergence scope is 4 call sites, not 4 strategy files** (T01 corrected): `SubagentDispatchStrategy` and `ParentReplyStrategy` both inherit `base.py:_deliver`. Modifying `base.py` covers both.
7. **T11 break + END-check must ship atomically** (T11 corrected): `break` alone changes dead-end graphs from CRASHED to COMPLETED (GraphOrchestrator line 341 is unconditional COMPLETED). Both changes required together.

## Decisions so far

<!-- the index — one line per closed ticket: enough to judge relevance, then zoom the link for the detail the ticket holds -->

- [T01: SessionTree data model](issues/01-modex-graph-reuse-decision.md) — Separate implementation in modex_agent, not modex_graph reuse. Message tracking model (MessageTrack) + running set, not node execution state. Three stores (SessionTreeStore / TreeNodeStore / MessageTrackStore). Quiesce = no DISPATCHED tracks + no running (corrected: does NOT check inbox.count). session_id + message_id already unified (corrected: InboxProducer already preserves). Track closing rules: TASK_REQUEST not closed on consume (corrected). 8-method SessionTreeManager (corrected: added on_dispatch_start/on_dispatch_end). Write path convergence: 4 call sites, not 4 strategy files (corrected: base.py covers SubagentDispatch + ParentReply).
- [T02: Tree node state machine](issues/02-tree-node-state-machine.md) — No node completion state machine. State lives on MessageTrack (DISPATCHED → CONSUMED / CANCELLED) + in-memory running set. Quiesce corrected: track + running, not inbox.count. T03 adds NodeVersionStatus (version lifecycle, different level). Track closing rules corrected: TASK_REQUEST not closed on consume.
- [T03: Version chain design](issues/03-version-chain-design.md) — Tree has NO version chain (persistent container). Only nodes have versions. Node version = one inbox dispatch cycle. HITL suspend = version COMPLETED. InboxFlushHook fold-in = no new version. Track is version-agnostic. Tree status reversible (COMPLETED → ACTIVE). Version lifecycle managed by on_dispatch_start/on_dispatch_end (bound to InboxPoller, NOT ReAct hooks). No success parameter — error exit = COMPLETED.
- [T04: Inbox-tree linkage](issues/04-inbox-tree-linkage.md) — `message_id` is the linkage key, already unified end-to-end. **No InboxProducer fix needed** (corrected: already preserves message_id). on_consumed injected into InboxConsumer (corrected: not in InboxFlushHook or pool.consume_inbox).
- [T05: InboxPoller integration](issues/05-inboxpoller-integration.md) — Tree is a shell over bus.send. Poller core logic unchanged (only _dispatch_batch +2 lines, _end_dispatch +5 lines, two finallys -1 line each). **Four** integration points: (1) tree.deliver replaces bus.send, (2) on_consumed in InboxConsumer (corrected from pool.consume_inbox), (3) on_dispatch_start/on_dispatch_end in InboxPoller _dispatch_batch + _end_dispatch (bound to inbox dispatch, NOT hooks), (4) wait_quiesce via idempotent signal_wakeup. Quiesce = track + running, not inbox.count (corrected). Track closing rules corrected.
- [T09: Peer communication closure](issues/09-peer-communication-closure.md) — Per-turn `ctx.graph_context` check in TaskDispatchTool. No pool-level mutation. Peer messages = receiving tree's external input (isomorphic to user input, no track, new root node version). **Peer completely unrelated to sender's tree** (corrected: no quiesce impact, no track, no tree status change on sender side). Graph context peer closure is tool-level, not tree-level.
- [T11: modex_graph semantic change](issues/11-modex-graph-semantic-change.md) — Graph scheduling exits with no executable node + END not reached → FAILED. **Two changes required atomically** (corrected: break + END-check must ship together). LinearScheduler `break` + GraphOrchestrator END-check. `load_latest_completed` already exists (no new method). No Node.run changes, no ParallelScheduler changes.
- [T13: Phase 3 — BotAgentNode.execute rewrite](issues/13-phase3-botagent-execute-rewrite.md) — execute becomes thin shell: pre-build artifacts → `tree.deliver(envelope, track_consume=True)` → `tree.wait_quiesce(tree_id)` → return. Turns driven by InboxPoller (ultimate convergence: graph turn = normal turn). `ModexGraphContext(GraphContext)` subclass stores per-node artifacts. Delete 70-line inline mutation (L168-239), delete auto-deliver fallback (L248-255), delete `isinstance(runner, ReActTurnRunner)` assert. Graph COMPLETED/FAILED judged by `ctx.reached_end` (Phase 0 T11, already implemented). Crash recovery NOT designed in execute — relies on bootstrap + persistence (native graph capability). **D10: retry 行为澄清** — T15-3 删除 retry loop 后,dead-end 由 scheduler 原生检测(no dispatches → FAILED)。**"不做的设计"清单**: wait_quiesce lost-wakeup fix / cancel_tree / crash_count guard / wait_quiesce timeout / emitter configurator / current_input 字段设置 — 均否决,详见 T13。
- [T14: Phase 4 — TurnContextConfigPipeline](issues/14-phase4-turn-context-config-pipeline.md) — `TurnContextDescriptor` (Pydantic frozen) + `TurnContextConfigPipeline` with 6 configurators at END of `build_runtime_and_context`. `agent_kind` from `_is_subagent()` / `agent_descriptor.comm_kind` (NOT from envelope). `_process_locked_inner` is single descriptor construction site for ReAct turns. **Unified graph_instance_id data flow (§6)**: 4 injection sites — **Site 1 = SendStrategy.execute template method** (base.py:79-80, covers ALL 3 strategies) / Site 2 = SubagentAutoSendHook / Site 3 = ExternalTurnRunner / Site 4 = modexctl SendRequest. **`AgentContext.graph_instance_id`** (new first-class `int | None` field) replaces `_LightGraphContext` — external/CLI 设 gid 直接到字段,不创建 dummy GraphContext 子类。`GraphContextBindingConfigurator` applies() = `graph_instance_id is not None`,always sets `ctx.graph_instance_id`,conditionally sets `ctx.graph_context`(resolver 成功时)。Environment variable bridge (MODEX_TASK_ID) for SSE child discovery. PeerNormalStrategy excluded. `GraphOrchestrator._active_contexts` + `get_graph_context(gid)` + resolver closure on TurnContextBuilder. DeliverRetryHook checks deliver tool existence. **§11 并发安全**: 6 risk points 全部 SAFE 或 FIXED(T15-2)。**"不做的设计"清单**: 同 T13,详见 T14。
- [T15: Technical debt cleanup](issues/15-technical-debt-cleanup.md) — Phase 3-4 prerequisite. **T15-1**: Delete `GraphContext.fork()` (zero production call sites, 9 test-only calls — dead code). **T15-2**: Remove `ctx.current_invocation` field (node.py:209 write is redundant — ContextVar already set at L211-222; only production read at context.py:336 has fallback; 8 test reads in 1 file). **T15-3**: Delete `UndeliveredError` class + Node.run retry loop + `max_retry` attribute — scheduler native dead-end detection covers it (ParallelScheduler: no dispatches → loop exits → FAILED; LinearScheduler: `else` branch 改为 `reached_end=False; break`)。**"不做的设计"清单**: crash_count guard / Coupling A fix(不再需要,UndeliveredError 删除)/ _run_existing_instance setup redundancy cleanup — 均否决,详见 T15。
- [R01: Can modex_graph support dynamic nodes?](issues/00-research-modex-graph-dynamic-nodes.md) — Research: modex_graph persistence decoupled, schedulers coupled. Full findings informed T01.

## Frontier (open, unblocked)

Phase 0-4 design complete. All specifiable decisions resolved and validated. Technical debt cleanup (T15) added as prerequisite.

**Implementation status**:
- Phase 0 (T11): **delivered** — `graph_orchestrator.py:340-345` has `ctx.reached_end` check.
- Phase 1 (T01-T04, T06): **delivered** — SessionTree core (`multi_agent/session_tree/`).
- Phase 2 (T05, T09, T12): **delivered** — InboxPoller integration, peer closure, comm tool convergence. `SubagentAutoSendHook` uses `tree.deliver` at L478.
- Phase 3 (T13): **designed, not implemented** — execute rewrite (thin shell) + D10 (retry 行为澄清)。
- Phase 4 (T14): **designed, not implemented** — TurnContextConfigPipeline + configurators + §11 并发安全。
- T15 (technical debt): **designed, not implemented** — prerequisite for Phase 3-4。

**Implementation batches** (T15 first as prerequisite):

| Batch | Content | Depends on | Testable independently? |
|-------|---------|------------|--------------------------|
| 0 | T15: fork deletion + current_invocation removal + UndeliveredError/retry loop deletion | none | ✅ (existing tests verify no regression) |
| 1 | Phase 4 infrastructure: descriptor + configurators + `build_runtime_and_context` param + resolver wiring | Batch 0 | ✅ (`turn_descriptor=None` short-circuits) |
| 2 | graph_instance_id propagation: 4 injection sites + `AgentContext.graph_instance_id` field + DeliverRetryHook fix | Batch 1 | ✅ (unit test each injection site) |
| 3 | GraphOrchestrator + SessionTreeManager extensions: `_active_contexts` + `get_graph_context` + `ModexGraphContext` + `tree_id_for_session` + `track_consume` | Batch 0 | ✅ (unit test each method) |
| 4 | pipeline_wiring binding: `graph_context_resolver` post-construction setter | Batch 1 + 3 | ✅ (integration test resolver closure) |
| 5 | Phase 3 execute rewrite: thin shell + delete inline mutation + delete auto-deliver | Batch 1-4 (all) | ✅ (integration test full graph flow) |

Phase 3 and 4 must ship together (or Phase 4 slightly before Phase 3). See T14 "Implementation order".

## Abandoned tickets (fully determined by upstream decisions)

- [T06: Persistence design](issues/06-persistence-design.md) — Three stores defined by T01. No independent decision.
- [T07: Crash recovery](issues/07-crash-recovery.md) — Recovery procedure defined by T03. No independent decision.
- [T08: Graph node integration](issues/08-graph-node-integration.md) — **Superseded by T13 + T14.** Original flow (direct execute_turn) replaced by tree.deliver + wait_quiesce (T13) + configurator pipeline (T14). T08's abandoned status is now moot — the integration is fully designed in T13/T14.
- [T10: External agent integration](issues/10-external-agent-integration.md) — Determined by T01 + T08 + T09. No independent decision. External agent is not a special type — it's an orthogonal dimension (agent implementation type), tree treats all agent types uniformly by message type.
- [T12: Communication tool convergence](issues/12-communication-tool-convergence.md) — Convergence point defined by T01 + T04 + T05. No independent decision.

## Not yet specified (deferred, not blocking Phase 3-4 implementation)

- **`tree.wait_quiesce` 阻塞语义**: 无限阻塞等待,不设 timeout。Stuck agent 后续通过心跳检测处理(不在 Phase 3-4 范围)。T13 D10 修复 lost-wakeup 排序 bug。
- **Memory/context system interaction**: How does tree node versioning interact with session memory (ScopedMessageHistory, archive, core memory)? Does a new node version reset memory, or carry forward? Depends on T03 (resolved: node version = one dispatch cycle, memory carries forward via session) and graph integration (T13).
- **WebUI session tree display**: Current WebUI builds session tree from `parent_session_id`. Tree adds version + pending-state dimensions. How does WebUI render versions and pending tracks? Depends on T03 (resolved: tree has no version, node has version) and T04 (resolved: message_id linkage).
- **Multi-level subagent nesting**: Native subagents cannot dispatch their own subagents (task tool only registered for main agents, TopologyPolicy rejects subagent→subagent). External subagents (opencode) may fork internally via SSE child discovery — graph_instance_id inherited via MODEX_TASK_ID env var (T14 §6.5). Nesting depth controlled by opencode's `subagent_depth` setting.
- **Experience review hook + tree**: ExperienceReviewHook fires after turn. With tree, "turn" = one dispatch cycle = one node version. Does review fire per-node-version or per-tree-quiesce?
- **Session GC interaction**: Existing session GC (ADR-0018) evicts stale sessions. How does tree interact with GC? Old node versions — when are they cleanable?
- **Streaming output observation**: `CompositeEmitter` (existing) wrapping for graph-level streaming deltas. Deferred from ADR-0039 §12 (future). Not covered by Phase 3-4.

## ADR-0039 disposition

ADR-0039 (`docs/adr/0039-turn-context-configuration-pipeline.md`) has been cleaned up:
- **Retained sections** (§2 ConfigPipeline, §3 Descriptor, §5 propagation, §6 stamp sites, §7 configurator matrix, §8 mutation extraction, §9 single-switch, §10 DeliverRetryHook fix, Context constraints) — these ARE the Phase 4 design. Refined in T14: agent_kind sourcing changed, graph_artifacts consolidated, session/workspace/pool_data removed from descriptor.
- **Deleted sections** (§1 event loop, §4 GraphContextRegistry, §11 _NodeLifecycleEventCollector, related Consequences/Considered Options) — superseded by SessionTree Phase 3 (T13: `tree.deliver` + `wait_quiesce` + `ModexGraphContext` + resolver closure).
- **Deferred** (§12 streaming output) — future work, not Phase 3-4.

## Out of scope

- **Peer agent communication in graph mode**: Graph scheduling shields peer agents — communication tools (task peer direction, send_to_agent to non-parent) are disabled in graph mode at the business layer. `PeerNormalStrategy` is NOT in the graph_instance_id propagation scope (T14 §6). This is a deliberate constraint: graph subagents must not communicate with peers, only with their parent (graph node agent). Native and external agents are both restricted.
- **ParallelScheduler per-node pause (NodeOnlyPolicy)**: User confirmed not needed — current requirement is "async subagent → wait → continue", event loop / tree quiesce handles this.
- **Full history scope separation for same-session graph/normal interleaving (OQ4)**: Semantic concern, deferred. Tree versioning may partially address this, but full history scope separation is out of scope.
