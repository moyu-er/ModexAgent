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

**Relationship to unified data flow (T14 §6)**: `ModexGraphContext` is the ReAct-side resolution target — when `_process_locked_inner` receives `graph_instance_id` from `envelope.metadata`, it calls `resolver(gid)` to get the full `ModexGraphContext` (with artifacts). This is the "resolution" half of the propagation/resolution separation. The "propagation" half (graph_instance_id in envelope.metadata) is handled by `SendStrategy.execute` + `SubagentAutoSendHook` + `ExternalTurnRunner` (T14 §6.2). External agents set `agent_context.graph_instance_id` directly (no GraphContext object, no _LightGraphContext) — see T14 §6.3.

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
- `GraphOrchestrator.recover_crashed()` + `bootstrap` (the sole recovery mechanism — queries store, derives seeds, restores state)
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
    await self._tree.wait_quiesce(tree_id)
    # return — no deliver check, no auto-deliver
    # graph COMPLETED/FAILED judged by ctx.reached_end (Phase 0 T11)
```

## Verification

- **T11 already delivered**: `graph_orchestrator.py:340-345` has `ctx.reached_end` check → COMPLETED/FAILED. Verified.
- **SessionTree already delivered**: `multi_agent/session_tree/` exists. `SubagentAutoSendHook` already calls `self._tree.deliver(...)` at L478. Verified.
- **`wait_quiesce` 需修改签名**: 当前 `manager.py:226` 签名 `wait_quiesce(self, tree_id: str, timeout: float) -> bool` — 有 timeout 参数,返回 bool。Phase 3 修改为 `wait_quiesce(self, tree_id: str) -> None` — 无限阻塞,无 timeout,无返回值。删除 deadline/remaining/wait_for 逻辑,改为 `await event.wait()` 无限等待。
- **`track_consume` NOT implemented**: zero matches — Phase 3 adds `*, track_consume: bool = False` to `tree.deliver`.
- **`tree_id_for_session` NOT implemented**: zero matches — Phase 3 adds public method `tree_id_for_session(session_id) -> str | None` (thin wrapper over `_node_store.get`, read-only).
- **`ModexGraphContext` NOT implemented**: zero matches — Phase 3 creates this class in `src/modex_agent/orchestration/`. **GraphOrchestrator.run_instance** (graph_orchestrator.py:330) currently creates `GraphContext(...)`. Phase 3 changes this to create `ModexGraphContext(...)`.
- **`GraphOrchestrator._active_contexts` / `get_graph_context` NOT implemented**: zero matches — Phase 3 adds these, parallel to `_active_instances`.
- **`TurnContextBuilder.graph_context_resolver` wiring**: post-construction setter in `pipeline_wiring.py`, NOT in `factory.py` directly (factory calls `_wire_main_pipeline` at L492 which is defined in `pipeline_wiring.py`).
- **fork() dead code**: T15-1 deletes `GraphContext.fork()` entirely — zero production call sites, only 9 test-only calls. ModexGraphContext does NOT need fork() override (nothing calls it).
- **`ctx.current_invocation` technical debt**: T15-2 removes the field. node.py:209 write is redundant (ContextVar already set at L211-222). Only production read (context.py:336) has ContextVar fallback. Phase 3-4 new code uses `get_execution()` exclusively.
- **UndeliveredError + retry loop**: T15-3 deletes both. Scheduler native dead-end detection covers it (详见"不做的设计"§B)。

## Closed items (previously open)

- **wait_quiesce 阻塞语义**: DECIDED — 无限阻塞等待,不设 timeout。`wait_quiesce` 阻塞直到 tree quiesce(no DISPATCHED tracks + no running + no pending_input)。Stuck agent(如 ReAct 死循环)场景后续通过心跳检测处理,不在 Phase 3-4 设计范围。
- **Message injection (L144-166) placement**: DECIDED — stays in execute as pre-deliver step. It's node-lifecycle logic (re-execution detection via `is_re_execution`), not per-turn configuration. Configurators handle per-turn config; execute handles node-lifecycle.

### D10 — Retry behavior clarification (UndeliveredError chain)

T13 D3 says "agent doesn't deliver → graph FAILED"。在 T15-3 删除 retry loop + UndeliveredError 后,链路简化为:

1. Agent doesn't deliver → `_collect_delivers` returns empty → `submit` dispatches nothing → `complete_invocation`
2. Scheduler 检测 dead-end:
   - **LinearScheduler**: `self._dispatches` 为空 → `ctx.reached_end = False` → `break` → 返回
   - **ParallelScheduler**: 无新 instance → `_ready` 空 + `running` 空 → 循环退出 → 返回
3. `ctx.reached_end` 保持 False → orchestrator 映射 FAILED

**Node 没 deliver = graph 没到 END = FAILED**。这是 graph 调度的原生 fail 机制,不需要 UndeliveredError。

### D11 — Technical debt cleanup (see T15)

Phase 3-4 implementation includes cleanup of identified technical debt. See `15-technical-debt-cleanup.md` for the full ticket:

- **T15-1**: Delete `GraphContext.fork()` (zero production call sites, 9 test-only calls)
- **T15-2**: Remove `ctx.current_invocation` field (write at node.py:209 is redundant — ContextVar already set at L211-222; only production read at context.py:336 has fallback; 8 test reads in 1 file)
- **T15-3**: Delete `UndeliveredError` class + Node.run retry loop + `max_retry` attribute. Scheduler native dead-end detection covers it. LinearScheduler `else: raise RoutingError` 改为 `ctx.reached_end = False; break`。6 个 retry 测试删除/重写。

## 不做的设计 (Explicitly Rejected)

以下设计在探索过程中被考虑过但最终否决,列出以避免后续理解产生错误。

### §A — wait_quiesce lost-wakeup fix (不做)

**考虑过**: 重排序 `event.clear()` 到 `is_quiesced()` 之前,防止 signal 在 `is_quiced` 的 await 期间被 clear 抹掉。

**否决理由**: asyncio 单线程下,`is_quiesced` 返回后到 `event.clear()` 之间全是同步操作(无 await 点)。`_signal(Event.set)` 只能在 await 点执行。如果 `_signal` 在 `is_quiced` 的 await 期间执行,说明对应的 `on_dispatch_end` 也执行了 `discard` + `close_tracks`,is_quiced 会读到更新后的状态。如果仍有其他 running,那些 running 完成时会再调 `_signal`。lost-wakeup 在当前 asyncio 单线程模型中不可触发。

### §B — cancel_tree (不做)

**考虑过**: Graph 终止时调 `tree_manager.cancel_tree(tree_id)`,关闭所有未关闭的 DISPATCHED tracks,防止 stale subagent delivers 导致 tree 永远 ACTIVE。

**否决理由**: 引入 `GraphOrchestrator → SessionTreeManager` 跨层依赖(graph 引擎知道 tree)。graph 的生命周期是 Python 对象生命周期管理(`run_instance` 的 ctx 创建/存储/清除)。tree 是 pool 级别对象,不随 graph 终止。Phase 0-2 已有 `recover_tree`(crash recovery 时清理 stale tracks)+ `on_session_evicted`(session GC 时清理)。stale delivers 不影响新 graph(新 graph = 新 graph_instance_id = 新 tree)。

### §C — crash_count guard (不做)

**考虑过**: `recover_crashed()` 新增 `crash_count` 字段 + `MAX_RECOVERY_ATTEMPTS = 3` 阈值,防止 consistently-crashing instance 在每次 restart 时无限重试。

**否决理由**: crash recovery 已经是收敛的原生机制(bootstrap + version chain + persistence),不是单独的 retry。`recover_crashed` 是 thin wrapper,委托给 `run_instance → bootstrap → 正常调度`。限制重试次数是业务策略,不是框架职责。如果用户想限制,可在调用层(`recover_crashed` 的 caller)实现,不需要框架 schema 变更。

### §D — wait_quiesce timeout (不做)

**考虑过**: `wait_quiesce` 加 timeout 参数,超时返回 False,execute 处理超时场景。

**否决理由**: Stuck agent 场景后续通过心跳检测处理(不在 Phase 3-4 范围)。timeout 引入额外的 stale-turn 处理复杂度。无限阻塞等待是正确语义 — tree quiesce 是唯一返回条件。

### §E — Graph-level emitter configurator (不做)

**考虑过**: Phase 4 加 `GraphEmitterConfigurator` 为 graph nodes 安装 graph-specific emitter。

**否决理由**: 当前 `BotAgentNode.execute` 不覆盖 emitter(用 pool 默认)。Phase 3-4 不涉及 streaming(ADR-0039 §12 deferred)。YAGNI。

### §F — current_input 字段设置 (不做)

**考虑过**: `build_runtime_and_context` 设置 `agent_context.current_input`(docstring 声称会设但实际未设)。

**否决理由**: ReAct agent 用 history,不用 current_input。external agent 集成时再决定。Phase 3-4 只修 docstring(T15)。
