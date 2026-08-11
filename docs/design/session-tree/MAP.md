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
- [R01: Can modex_graph support dynamic nodes?](issues/00-research-modex-graph-dynamic-nodes.md) — Research: modex_graph persistence decoupled, schedulers coupled. Full findings informed T01.

## Frontier (open, unblocked)

All specifiable decisions resolved and validated. Implementation can proceed.

## Abandoned tickets (fully determined by upstream decisions)

- [T06: Persistence design](issues/06-persistence-design.md) — Three stores defined by T01. No independent decision.
- [T07: Crash recovery](issues/07-crash-recovery.md) — Recovery procedure defined by T03. No independent decision.
- [T08: Graph node integration](issues/08-graph-node-integration.md) — Flow defined by T05 + T11. No independent decision.
- [T10: External agent integration](issues/10-external-agent-integration.md) — Determined by T01 + T08 + T09. No independent decision. External agent is not a special type — it's an orthogonal dimension (agent implementation type), tree treats all agent types uniformly by message type.
- [T12: Communication tool convergence](issues/12-communication-tool-convergence.md) — Convergence point defined by T01 + T04 + T05. No independent decision.

## Not yet specified

- **TurnContextConfigPipeline integration**: After Tree core is delivered, extract `BotAgentNode.execute`'s 70-line post-build mutation (deliver tool, approval=None, MAX_TURNS=3, topology, knowledge keys) into a `TurnContextConfigPipeline` with `TurnContextDescriptor` + 6 Graph*Configurators, modeled on `SystemPromptPipeline`. Peer communication closure (T09) migrates from simple `ctx.graph_context` check to a `PeerCommunicationConfigurator`. Configurator reads `graph_instance_id` from envelope metadata → resolves `GraphContext` via `GraphOrchestrator.get_graph_context(gid)` (new method + `_active_contexts: dict[int, GraphContext]`). This replaces ADR-0039 Pillar 2 (GraphContextRegistry) and Pillar 3 (Event loop) — both superseded by Tree. ADR-0039 Pillar 1 (ConfigPipeline) is retained. **Do NOT reference `docs/handoff/turn-context-configuration-pipeline.md` — it is not git-tracked. This record is self-contained.**
- **wait_quiesce vs ADR-0039 BotAgentNode.execute event loop interaction**: ADR-0039 specifies BotAgentNode.execute as an event loop with `_NodeLifecycleEventCollector` + `event_queue`. T05 shows `graph node → tree.wait_quiesce`. Are these the same loop or different? This interaction must be clarified before Phase 3 implementation. Options: (a) wait_quiesce replaces ADR-0039's event loop, (b) BotAgentNode.execute calls wait_quiesce after its own loop, (c) wait_quiesce called by a different component.
- **Memory/context system interaction**: How does tree node versioning interact with session memory (ScopedMessageHistory, archive, core memory)? Does a new node version reset memory, or carry forward? Depends on T03 (resolved: node version = one dispatch cycle, memory carries forward via session) and graph integration (T08).
- **WebUI session tree display**: Current WebUI builds session tree from `parent_session_id`. Tree adds version + pending-state dimensions. How does WebUI render versions and pending tracks? Depends on T03 (resolved: tree has no version, node has version) and T04 (resolved: message_id linkage).
- **Multi-level subagent nesting depth limits**: How deep can subagent chains go? Current opencode limits to `subagent_depth` (default 1). What's our limit?
- **Task tool API changes**: Does `task` tool need new parameters (e.g. `background` mode like opencode)? Or does tree make fire-and-forget work naturally? Depends on T08 (graph integration) and T12 (comm tool convergence).
- **Experience review hook + tree**: ExperienceReviewHook fires after turn. With tree, "turn" = one dispatch cycle = one node version. Does review fire per-node-version or per-tree-quiesce?
- **Session GC interaction**: Existing session GC (ADR-0018) evicts stale sessions. How does tree interact with GC? Old node versions — when are they cleanable?

## Out of scope

- **Peer agent tree**: Peer agents are separate trees (peer = another main agent). Only subagent lifecycle is managed. **Peer completely unrelated to sender's tree** — no track, no quiesce impact, no tree status change. Graph context disables peer communication entirely.
- **ParallelScheduler per-node pause (NodeOnlyPolicy)**: User confirmed not needed — current requirement is "async subagent → wait → continue", event loop / tree quiesce handles this.
- **Full history scope separation for same-session graph/normal interleaving (OQ4)**: Semantic concern, deferred. Tree versioning may partially address this, but full history scope separation is out of scope.
