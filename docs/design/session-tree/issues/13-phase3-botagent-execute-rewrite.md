# T13: Phase 3 — BotAgentNode.execute rewrite (thin shell)

> Type: `wayfinder:ticket`
> Status: **Active — design complete**
> Depends on: T01 (SessionTree core), T05 (InboxPoller integration), T11 (modex_graph semantic change — delivered in Phase 0)

## Question

How does `BotAgentNode.execute` use SessionTree to drive graph node turns? Specifically: how does execute wait for async subagent results without returning early, and how does it hand off per-turn configuration to the unified `build_runtime_and_context` path?

## Background — the two live bugs

Current `BotAgentNode.execute` (examples/bot_project/bot/graph/agent_node.py:99-259) has two problems:

1. **P0-6 — subagent has no graph context**: execute directly calls `runner.execute_turn` after 70 lines of inline mutation (L168-239). The subagent dispatched during this turn goes through `InboxPoller → _process_locked_inner → build_runtime_and_context` — a path with no graph awareness. Subagent's `GraphWorkflowProvider` is empty, `approval` may deadlock, `SubagentAutoSendHook` can't propagate graph metadata.

2. **Node lifecycle — execute returns before subagent result**: execute calls `execute_turn` once (turn A). If agent dispatches subagent (async) and turn A ends without deliver, execute auto-delivers (L248-255) and returns → `node.run` completes → graph advances → but subagent result hasn't arrived yet.

## Resolution

**BotAgentNode.execute becomes a thin shell: pre-build artifacts → tree.deliver → tree.wait_quiesce → return.**

Turns are driven by InboxPoller (not by execute directly). This is the "ultimate convergence" — graph node turn = normal turn, all going through `_process_locked_inner → build_runtime_and_context + configurators` (Phase 4).

### New execute flow

```
BotAgentNode.execute(ctx, integrated_input):
  1. Ensure session (CACHED, existing logic)
  2. Pre-build graph artifacts (deliver_tool, topology_section, node_description, knowledge_config)
  3. Store artifacts on ModexGraphContext: ctx.set_node_artifacts(self.name, artifacts)
  4. Build graph input envelope (metadata: graph_instance_id, graph_node_name, is_node_execution=True)
  5. await tree.deliver(session_id, envelope, track_consume=True)
     → inbox receives message
     → EXTERNAL_INPUT track created (DISPATCHED)
     → InboxPoller wakes, dispatches turn via _process_locked_inner
     → build_runtime_and_context + configurators install graph config (Phase 4)
     → agent ReAct loop runs, may dispatch subagents, may call deliver
  6. await tree.wait_quiesce(tree_id)
     → waits until: no DISPATCHED tracks + no running nodes
     → subagent AGENT_RESULT consumed → track closed → eventually quiesces
  7. return (no deliver check, no auto-deliver)
```

### Key decisions

#### D1 — ModexGraphContext inherits GraphContext (artifacts propagation)

Deliver tool is a Python object (references `BotAgentNode._graph_ref` + `_pending_delivers`), cannot serialize into envelope metadata. Solution: `ModexGraphContext(GraphContext)` subclass in modex_agent business layer.

```python
class ModexGraphContext(GraphContext[ModexGraphState]):
    """Business-layer GraphContext extension. modex_graph base stays pure."""
    _node_artifacts: dict[str, GraphTurnArtifacts]  # keyed by node_name

    def set_node_artifacts(self, node_name: str, artifacts: GraphTurnArtifacts) -> None: ...
    def get_node_artifacts(self, node_name: str) -> GraphTurnArtifacts | None: ...
```

- `ModexGraphContext` defined in `src/modex_agent/orchestration/` (business layer).
- `modex_graph/context.py` base class unchanged (ADR-0033 D5.1 — regular class, subclassable).
- `GraphOrchestrator` gains `_active_contexts: dict[int, GraphContext]` — stores context at `run_instance`, clears at `finalize`. This is the GraphContext lifecycle owner. **run_instance creates `ModexGraphContext`** (not base `GraphContext`) so `set_node_artifacts` is available.
- `TurnContextBuilder` gains `graph_context_resolver: Callable[[int], GraphContext | None]` (post-construction setter, closure binding to `orchestrator.get_graph_context(gid)`). Wiring by pool/workspace builder post-construction.
- Configurator calls resolver(gid) → `ModexGraphContext` → `get_node_artifacts(node_name)` → installs. Configurator does NOT touch `ModexGraphContext` type — resolver returns `GraphContext` (framework type), configurator reads artifacts from descriptor (pre-resolved by `_process_locked_inner`).

**Relationship to unified data flow (T14 §6)**: `ModexGraphContext` is the ReAct-side resolution target — when `_process_locked_inner` receives `graph_instance_id` from `envelope.metadata`, it calls `resolver(gid)` to get the full `ModexGraphContext` (with artifacts). This is the "resolution" half of the propagation/resolution separation. The "propagation" half (graph_instance_id in envelope.metadata) is handled by `SendStrategy.execute` + `SubagentAutoSendHook` + `ExternalTurnRunner` (T14 §6.2). External agents use `_LightGraphContext` (gid only, no artifacts) instead of `ModexGraphContext` — see T14 §6.3.

#### D2 — tree.deliver gains `track_consume` parameter

**Problem**: graph input envelope is EXTERNAL_INPUT type (graph → main agent). Phase 0-2 rule: EXTERNAL_INPUT creates no MessageTrack. However, `SessionTreeManager.deliver` (manager.py:109-114) DOES add the session to `_pending_input` for EXTERNAL_INPUT/AGENT_MESSAGE types before returning, and `is_quiesced` (manager.py:211-217) checks `_pending_input`. So the premature-quiesce scenario does NOT actually occur — `_pending_input` already prevents it.

`track_consume` still provides value: (1) persisted tracking (MessageTrackStore survives crash, `_pending_input` is in-memory); (2) consistent closing mechanism (on_consumed or on_dispatch_end fallback, integrated with existing track lifecycle); (3) individual message tracking (per-message track vs per-session set).

**Solution**: `tree.deliver(session_id, envelope, *, track_consume: bool = False)`:
- `track_consume=False` (default): existing behavior, no track for EXTERNAL_INPUT (goes through `_pending_input` path).
- `track_consume=True`: creates a MessageTrack with `status=DISPATCHED` for this EXTERNAL_INPUT message (in addition to the `_pending_input` path).

**Closing rules** (same as AGENT_RESULT):
- on_consumed: track closed → CONSUMED (when InboxConsumer consumes the message).
- on_dispatch_end fallback: track closed (when dispatch cycle ends).

**Quiesce definition** (3 conditions, matching actual `is_quiesced` implementation): no DISPATCHED tracks + no running nodes + no `_pending_input`. Both `_pending_input` (in-memory) and `track_consume` track (persisted) prevent premature quiesce; `track_consume` adds persisted crash-recovery semantics.

#### D3 — Delete auto-deliver; graph FAILED judges completion

**Delete**: execute's auto-deliver fallback (current L248-255). Execute does NOT check `_has_pending_delivers()` after wait_quiesce. It just returns.

**Completion judgment**: Phase 0-2 T11 (already implemented in `graph_orchestrator.py:340-345`):
```python
final_state = await GraphEngine(compiled).run_async(ctx)
status = GraphInstanceStatus.COMPLETED if ctx.reached_end else GraphInstanceStatus.FAILED
```

**Chain**: agent delivers → `node.run` collects delivers → submits → downstream nodes have input → graph reaches END → `ctx.reached_end = True` → COMPLETED. Agent doesn't deliver → `node.run` gets empty delivers → submits empty → no downstream executable → graph doesn't reach END → `ctx.reached_end = False` → FAILED.

**Semantics**: agent主动 deliver = 有输出 = COMPLETED; agent不 deliver = 无输出 = FAILED. No false-positive auto-deliver.

#### D4 — External agent support (delete isinstance assert)

**Delete**: `isinstance(runner, ReActTurnRunner)` assert (current L211-215).

After T13 rewrite, execute doesn't call `runner.execute_turn` directly — turns go through InboxPoller → `_process_locked_inner` → `build_runtime_and_context` → runner (ReAct or External). Both runner types work.

**opencode TurnCompletionWaiter**: Tree is the outer layer, opencode's TurnCompletionWaiter is the inner black box. Serial chain, not double-waiting:
- opencode subagent completes internally → `SubagentAutoSendHook` fires → `tree.deliver(AGENT_RESULT)` → track DISPATCHED → InboxPoller drives parent turn → consume → track closed → tree quiesces.
- Tree tracks modex session_ids via MessageTrack; opencode tracks provider session_ids via TurnCompletionWaiter. Orthogonal.

#### D5 — Crash recovery: NOT designed in execute

**Decision**: execute does NOT contain crash recovery logic. Relies on modex_graph's existing recovery:
- `node.run` retry loop (max_retry, `modex_graph/node.py:136-303`)
- `GraphOrchestrator.recover_crashed()` + `bootstrap` re-runs crashed instances
- SessionTree's `recover_tree` (Phase 0-2 T07) cleans up残留 DISPATCHED tracks + RUNNING versions

**Normal flow assumption**: execute is designed for `deliver → wait_quiesce → return`. If the process crashes mid-wait, modex_graph's recovery takes over. CACHED session retains history. Tree's recover_tree handles track cleanup.

**Code comment**: "Crash recovery relies on modex_graph node.run retry + GraphOrchestrator bootstrap. execute内部不做恢复设计。"

#### D6 — Deliver回流链

Deliver tool is the same instance BotAgentNode pre-built (stored in `ModexGraphContext._node_artifacts`). Agent calls `deliver_tool.deliver(payload, target)` → writes to `BotAgentNode._pending_delivers` (instance attribute). After wait_quiesce, execute returns. `node.run`'s collect-delivers logic (existing, unchanged) reads `_pending_delivers`.

**Multiple delivers**: all writes to the same `_pending_delivers` list. `node.run` collects all. `track_consume` closes on first consume (first deliver message consumed ends the turn's inbox consumption cycle) — but `_pending_delivers` accumulates all delivers within that cycle. At least one deliver + turn end = node has output.

**`self.deliver()` is the single accumulation seam**: regardless of how delivers are produced (agent calls GraphDeliverTool, or any future mechanism), they must land in `self._pending_delivers` via `self.deliver()` for `Node.run._collect_delivers` to see them. Otherwise the undelivered-retry loop fires (`node.py:243-248`, `max_retry=3` → `UndeliveredError`). Phase 3's tree path converges on this seam — no parallel deliver paths (convergence rule 1).

#### D7 — modex_graph design division: execute is the business customization point

**Confirmed via code exploration** (`src/modex_graph/node.py`):

`Node.run` (node.py:136-303) is the framework-fixed lifecycle. `execute` is the abstract method subclasses implement — the sole business customization point. The three-layer split is documented at node.py:10-22:

- `run` (framework-fixed): orchestrate integrate → execute (with undelivered detection retry) → submit
- `_deliver` (framework-fixed): accumulate + persist
- `submit` (node-custom, overridable): actual dispatch logic

**execute's contract** (node.py:116-123):
- Input: read from `integrated_input` (upstream delivers) — already prepared by framework
- Output: call `self.deliver(content, next_node, ctx)` to send data downstream
- Working state: `ctx.state.node_scratch[self.node_id]`

**`_integrate_upstream` is framework logic** (node.py:305-350): collects upstream delivers via `coordinator.collect_consumable_delivers` → marks consumed → integrates via `input_integrator`. `AgentNode._integrate_upstream` (agent_node.py:93-141) adds agent-memory-aware filtering (CONSUMED_PENDING filtered to prevent duplicate SYSTEM_REMINDER injection). BotAgentNode inherits this — does NOT override it.

**Phase 3 compatibility**: execute receives already-integrated `IntegratedInput` (frozen Pydantic value object). Phase 3's thin shell transforms it into envelope content for `tree.deliver`. The framework's integrate mechanism is unchanged — it still collects upstream delivers and hands them to execute. What changes is what execute does with the input (deliver via tree instead of direct history.append + execute_turn).

#### D8 — IntegratedInput → tree.deliver: how input flows

Current flow (pre-Phase-3):
```
Node.run._integrate_upstream → IntegratedInput
  → BotAgentNode.execute: _format_integrated_input(integrated_input) → text string
  → wrap_system_reminder → agent_context.history.append(SYSTEM_REMINDER)
  → runner.execute_turn
```

Phase 3 flow:
```
Node.run._integrate_upstream → IntegratedInput (UNCHANGED)
  → BotAgentNode.execute: _format_integrated_input(integrated_input) → envelope content
  → tree.deliver(session_id, envelope, track_consume=True)
  → InboxPoller → _process_locked_inner → build_runtime_and_context → runner.execute_turn
```

**`_format_integrated_input` stays in execute** — it's business logic (formats upstream payloads as text). The framework hands execute an `IntegratedInput`; execute formats it into whatever the envelope needs. `Node.run`'s integrate mechanism is not aware of tree — it just prepares `IntegratedInput` as before.

**Message injection (Origin Request + SYSTEM_REMINDER, current L144-166)**: stays in execute as pre-deliver step. It's node-lifecycle logic (re-execution detection via `is_re_execution` — session history has messages → skip [Origin Request]), not per-turn configuration. Execute formats both the Origin Request and upstream input into the envelope content before `tree.deliver`.

#### D9 — execute accesses SessionTreeManager via pool resolution

**Current state**: `BotAgentNode` does NOT have a tree reference injected. `BotAgentNodeFactory.create` (agent_node_factory.py:35-42) injects only `workspace_resolver`.

**Phase 3 access**: execute resolves tree via `_resolve_pool().tree_manager` (pool_instance.py:51 exposes `tree_manager: SessionTreeManager`). This works today — `_resolve_pool()` (agent_node.py:79-84) already resolves the pool by name from the workspace. No factory change needed.

```python
# In Phase 3 execute:
tree = self._resolve_pool().tree_manager
```

**Alternative (factory injection)**: BotAgentNodeFactory receives tree and passes to BotAgentNode.__init__. But the factory is workspace-scoped while tree is per-pool — pool_name comes from the spec at create-time. Pool-time resolution is the natural path.

### What gets deleted from current execute

| Current code | Lines | Fate |
|---|---|---|
| 70-line post-build mutation (deliver_tool install, graph_context set, approval=None, MAX_TURNS, topology, knowledge keys) | L168-239 | **Deleted** — migrated to Phase 4 configurators |
| `isinstance(runner, ReActTurnRunner)` assert | L211-215 | **Deleted** — external agent supported |
| Direct `runner.execute_turn(...)` call | L240 | **Deleted** — turns driven by InboxPoller |
| Auto-deliver fallback (`_has_pending_delivers` → extract content → `self.deliver(...)`) | L248-255 | **Deleted** — graph FAILED judges completion |
| Message injection (Origin Request + upstream as SYSTEM_REMINDER) | L144-166 | **Retained** — node-lifecycle logic, not per-turn configuration. Stays in execute (or pre-deliver step). |

### New execute body (skeleton)

```python
async def execute(self, ctx: GraphContext[Any], integrated_input: Any) -> None:
    session = await self._ensure_session(ctx)
    artifacts = self._build_graph_artifacts(ctx)  # deliver_tool, topology, description, knowledge
    ctx.set_node_artifacts(self.name, artifacts)   # ModexGraphContext

    envelope = self._build_graph_input_envelope(ctx, integrated_input, session)
    tree_id = self._tree.tree_id_for_session(session.session_id)

    await self._tree.deliver(
        session.session_id, envelope, track_consume=True
    )
    await self._tree.wait_quiesce(tree_id, timeout=self._node_timeout)
    # return — no deliver check, no auto-deliver
    # graph COMPLETED/FAILED judged by ctx.reached_end (Phase 0 T11)
```

## Verification

- **T11 already delivered**: `graph_orchestrator.py:340-345` has `ctx.reached_end` check → COMPLETED/FAILED. Verified.
- **SessionTree already delivered**: `multi_agent/session_tree/` exists. `SubagentAutoSendHook` already calls `self._tree.deliver(...)` at L478. Verified.
- **`wait_quiesce` ALREADY implemented**: `manager.py:219-233`, signature `async def wait_quiesce(self, tree_id: str, timeout: float) -> bool`. Phase 3 uses it directly (no new method needed). The `timeout` parameter is required — execute skeleton should pass a timeout value.
- **`track_consume` NOT implemented**: zero matches — Phase 3 adds this param to `tree.deliver`.
- **`ModexGraphContext` NOT implemented**: zero matches — Phase 3 creates this class. **GraphOrchestrator.run_instance** (graph_orchestrator.py:330) currently creates `GraphContext(...)`. Phase 3 changes this to create `ModexGraphContext(...)` instead (business layer construction, stored in `_active_contexts`). The `_active_contexts` dict is new, parallel to existing `_active_instances`.
- **`GraphOrchestrator._active_contexts` / `get_graph_context` NOT implemented**: zero matches — Phase 3 adds these.
- **`TurnContextBuilder.graph_context_resolver` wiring**: post-construction setter, called by pool/workspace wiring code after both TurnContextBuilder and GraphOrchestrator are constructed. The closure captures `orchestrator.get_graph_context`.

## Open items (deferred, not blocking)

- **Timeout for wait_quiesce**: if agent stuck in ReAct loop, tree never quiesces. `wait_quiesce` should accept a timeout parameter (tree-level). Timeout → execute returns → graph FAILED via `reached_end=False`. Implementation detail, not design decision.
- **Message injection (L144-166) placement**: stays in execute as pre-deliver step, or moves to a configurator. Tentatively stays — it's node-lifecycle logic (re-execution detection), not per-turn configuration.
