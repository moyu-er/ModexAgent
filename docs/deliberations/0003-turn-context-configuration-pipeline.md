# 0003: Turn Context Configuration Pipeline + Graph Agent Node Lifecycle

> ADR: `docs/adr/0039-turn-context-configuration-pipeline.md`
> Glossary terms: `CONTEXT.md` — Turn Context Configuration, Graph-aware message
> Related: ADR-0038 (graph node agent context injection), `docs/design/static-graph-scheduling/todo.md` P0-6

## Objective

Unify per-turn runtime configuration across all agent turn contexts (normal-session main, normal-session subagent, graph-node-direct, graph-scheduling-subagent-via-inbox, external) into a single convergent path, resolving P0-6 (graph-scheduling subagent via inbox lacks graph-specific configuration) without pausing the graph and without rebuilding agent objects per session.

## User requirements (explicit, collected across the design conversation)

### R1: No per-session agent rebuild

Agent objects are pool-level singletons, reused across sessions and graph instances. `ReActAgent` is stateless (verified: 6 stateless attributes in `__init__`). `build_runtime_and_context` already produces fresh per-turn `AgentContext`. Rebuilding buys no isolation, costs MCP/memory/tool re-wiring complexity.

### R2: Pipeline-style configuration (like SystemPromptPipeline)

All per-turn runtime configuration flows through an ordered pipeline of configurators (`TurnContextConfigPipeline`), invoked at the end of `build_runtime_and_context`. This avoids configuration divergence across scenarios and prevents redundant/special-cased config paths for the same agent singleton used in different contexts.

### R3: Graph metadata propagation through inbox

Inbox messages must carry graph context metadata (`graph_instance_id`, `source_node_id`) in `envelope.metadata` (a `dict[str, Any]` with `extra="allow"` — no schema migration needed). Downstream consumers (subagent, parent re-activation) parse this metadata to resolve graph context from the registry.

**Two propagation paths, both required**:
- **parent→subagent (TASK_REQUEST)**: `SubagentDispatchStrategy.build_envelope` appends metadata when `req.context.graph_context is not None`
- **subagent→parent (AGENT_RESULT)**: `SubagentAutoSendHook` appends metadata when `state.custom[GRAPH_INSTANCE_ID]` is set

**Transparent passthrough**: subagent dispatching its own subagent must propagate graph metadata. Multi-level subagent chains preserve graph context end-to-end.

### R4: Subagent is NOT a graph node

Subagent is an agentNode **internal capability** — the agent calls task tool, dispatches a subagent that runs via the normal session inbox mechanism. Subagent is NOT registered as a graph node, NOT in the graph schedule, NOT driven by the graph engine.

### R5: Cannot pause the entire graph for internal subagent waiting

A node waiting for an internal subagent must NOT pause the graph. `GraphInterrupt` pauses the **entire graph instance** (verified: ParallelScheduler D13 cancels all siblings; LinearScheduler aborts loop; `GraphOrchestrator` sets instance to PAUSED). This is unacceptable for a node's internal async wait.

### R6: Node lifecycle must encompass subagent async execution

`BotAgentNode.execute` must not return until the agent's work is truly complete (deliver + normal turn end). If agent dispatches subagent and turn A ends without deliver, `execute` must wait for turn B (driven by InboxPoller) where agent receives subagent result and delivers. `node.run` completes only when `execute` returns with delivers.

### R7: Correct node completion signal

Node is considered complete when: **deliver was called AND turn ended normally** (FinallyGraphHook fired with non-error stop_reason). If turn ends without deliver → agent may be waiting for subagent → continue waiting. If deliver called but turn didn't end normally → error → auto-deliver incomplete result.

### R8: InboxFlushHook must be purely mode-agnostic

InboxFlushHook only pulls messages and adds to context history. It does NOT:
- Distinguish graph vs normal mode
- Do mode-specific configuration
- Read `graph_ref` / `graph_instance_id` from metadata
- Stamp any registry
- Set `graph_context`

All configuration depends on "what mode is currently running", determined by the `TurnContextConfigPipeline` configurators (which read from the registry, not from InboxFlushHook).

### R9: Graph vs session mode isolation — same agent, same session, concurrent modes

**Critical scenario (previously misrecorded)**: the SAME agent (pool singleton), SAME main agent, SAME session (CACHED session_id) can be in BOTH:
- Graph scheduling mode (graph engine drives a turn via `BotAgentNode.execute`)
- Normal session mode (user sends a WebUI message → `InputPipeline` → `InboxPoller` → `pipeline.process_message`)

These two modes can coexist or interleave on the same session. The isolation requirement is:
- A graph-scheduling turn must NOT leak graph config (deliver tool, approval=None, MAX_TURNS=3, topology) into a subsequent normal-session turn on the same session
- A normal-session turn must NOT affect graph scheduling state (registry stamp, event_queue, _deliver_received)
- The `TurnContextConfigPipeline` configurators must correctly determine mode **per-turn**, not per-session

**Current design's per-turn isolation**: `build_runtime_and_context` constructs a fresh `AgentContext` each turn. Configurators read from `desc.graph_context` (resolved from registry per-turn). A normal-session turn has no graph metadata in `input_metadata` → no registry stamp → `desc.graph_context = None` → configurators skip. A graph turn has graph metadata → stamp → `desc.graph_context` set → configurators apply. This provides per-turn mode isolation.

**But there is a residual concern (N16)**: the `BotAgentNode.execute` event loop holds `_deliver_received` flag and the temporary hook on `hook_runner`. If a user sends a WebUI message while `BotAgentNode.execute` is in `await event_queue.get()` (waiting for subagent), the WebUI message triggers InboxPoller → `pipeline.process_message` → a normal-session turn on the **same session**. This turn's `FinallyGraphHook` fires → `_NodeLifecycleEventCollector` catches it (session_id matches!) → pushes "turn_completed" to event_queue. `BotAgentNode.execute` wakes up, checks `_deliver_received` → False (the WebUI turn didn't deliver) → continues listening. This is **correct behavior** — the WebUI turn doesn't falsely complete the node. But it means the event loop correctly tolerates interleaving normal-session turns.

**However**: the normal-session turn on the same session might install tools (pool default), set approval (enabled), etc. — all on the fresh per-turn `AgentContext`. This doesn't leak to the next graph turn (fresh `AgentContext` again). But it does mean the session history accumulates both graph turns and normal turns interleaved. This is a semantic concern (not a technical isolation failure) — the agent's history has mixed graph and normal context. Future improvement: separate history scopes or session-level mode tracking (see OQ4).

**Current design satisfies basic isolation** (per-turn AgentContext fresh, configurators per-turn). Advanced isolation (history scope separation) is deferred (OQ4). The design must preserve extensibility for this future improvement.

- Normal-session subagent completing must NOT trigger any graph resume/event
- Graph-scheduling subagent completing MUST propagate graph metadata
- `SubagentAutoSendHook` checks `state.custom[GRAPH_INSTANCE_ID]` (NOT `ctx.graph_context`, which is always None on subagent's context — verified)

### R10: Subagent gets minimal graph config, but preserves extensibility

Subagent is an atomic capability provider. It gets:
- ✅ `graph_context` (for SubagentAutoSendHook to propagate metadata, for GraphWorkflowProvider to inject basic graph guidance)
- ✅ `approval=None` (no user approval in graph scheduling)
- ✅ `GRAPH_INSTANCE_ID` in per-turn state (for hook to read)

Subagent does NOT get:
- ❌ GraphDeliverTool (subagent notifies parent via SubagentAutoSendHook, not via deliver to downstream node)
- ❌ MAX_TURNS=3 (subagent uses pool default — it's atomic, not a graph node)
- ❌ Topology / node description (subagent is not a node)
- ❌ Knowledge keys (subagent doesn't read/write knowledge base)

**Extensibility preserved**: configurator `applies()` uses `is_node_execution and agent_kind == MAIN` for node-specific config. Adding subagent graph tools in the future = new configurator with `applies() → graph_context is not None and agent_kind == SUBAGENT`, without changing existing configurators.

### R11: Pydantic for all cross-module structured data

All new structured types use Pydantic `BaseModel` with `frozen=True, extra="forbid"`:
- `TurnContextDescriptor`
- `GraphContextStamp`
- `GraphInstanceRef` (if needed for serialization — currently metadata is a plain dict with `graph_instance_id`/`source_node_id` keys)

No `@dataclass(frozen=True)` for classes with behavior (AGENTS.md rule 11). No bare `Any`/`list`/`dict` in framework APIs.

### R12: Framework-layer separation

- `orchestration/` and `multi_agent/` are sibling framework modules — no framework-level cross-module dependency
- `GraphOrchestrator` gains read-only query methods (`is_instance_active`, `get_graph_context`) — pure functions, not lifecycle callbacks
- Business layer (`examples/bot_project/bot/workspace/wiring/`) injects closures (orchestrator methods) into framework objects (registry)
- `BotAgentNode` (business) bridges orchestrator and pool — existing pattern

### R13: Framework-level base class

If the design satisfies all requirements, `BotAgentNode`'s event-loop pattern should be generalized into a framework-level base class in `src/modex_agent/`, so business layers can subclass and customize. The base class provides: event loop, registry stamping, descriptor construction, temporary hook registration. Subclasses override: topology rendering, knowledge config, deliver tool construction, timeout.

## Design notes (critical findings from exploration)

### N1: Subagent's graph_context is always None

**Verified**: `SubagentAutoSendHook` runs on the **subagent's** `AgentContext`. Subagent builds its own context via its own `TurnContextBuilder.build_runtime_and_context`, which never sets `graph_context`. Only `BotAgentNode.execute:185` sets it. So `ctx.graph_context is not None` in SubagentAutoSendHook would **never fire** (false negative).

**Correct switch**: `state.custom[TurnCustomKey.GRAPH_INSTANCE_ID]`, set by configurator from envelope metadata. Normal-session subagents never get this key → hook skips graph metadata propagation.

### N2: GraphInterrupt pauses the entire graph

**Verified**: `GraphInterrupt` propagates node → scheduler → engine → orchestrator. ParallelScheduler D13 cancels ALL sibling tasks. LinearScheduler aborts the while loop. `GraphOrchestrator` sets the entire `GraphInstance` to PAUSED. `interrupt_policy.py` names `NodeOnlyPolicy` as a future subclass that does NOT exist.

**Implication**: Cannot use GraphInterrupt for internal subagent waiting. Must use event loop within `execute`.

### N3: node.run retry loop semantics

**Verified**: if `execute` returns without delivers, `node.run` injects "you forgot to deliver" error feedback and retries (up to 4 times, then RoutingError). `_pending_delivers` is reset each retry iteration.

**Implication**: `execute` must NOT return without delivers (unless timeout/error). The event loop ensures `execute` returns only after deliver + normal turn end.

### N4: InboxFlushHook is already mode-agnostic

**Verified**: current implementation (79 lines) only reads `reminder_kind` and `invocation_id` (message-formatting). No graph logic. ADR-0039 Phase 3's proposal to add registry stamp here is rejected — stamp moves to `turn_runner._process_locked_inner` (inbox-driven) and `BotAgentNode.execute` (direct).

### N5: GraphContextRegistry stamp/resolve split

**Stamp (side effect)**: in `turn_runner._process_locked_inner` (reads `input_metadata["graph_instance_id"]`, stamps via `context_resolver` closure) + `BotAgentNode.execute` (has `ctx` directly).

**Resolve (pure read)**: in `build_runtime_and_context` (`registry.resolve(session_id)` → fill `desc.graph_context`).

This keeps InboxFlushHook pure, `build_runtime_and_context` pure (resolve is a read), and puts the stamp side effect in orchestrators that already do side effects.

### N6: DeliverRetryHook needs deliver tool existence check

**Verified**: `DeliverRetryHook` checks `deliver_count` and `MAX_TURNS`, not `ctx.graph_context` or deliver tool existence. For subagent turns (no deliver tool, MAX_TURNS defaults to 1), it returns after first turn. But if pool default MAX_TURNS > 1, it would request continuation with deliver_count=0 → potential loop.

**Fix**: check deliver tool existence before enforcing.

### N7: GraphWorkflowProvider needs pre-built data

**Verified**: `GraphWorkflowProvider` reads `GRAPH_TOPOLOGY_CONTEXT`, `GRAPH_NODE_DESCRIPTION`, `GRAPH_KNOWLEDGE_DIR` from `ctx.runtime.state.custom`. These are currently built by `BotAgentNode` instance methods needing `_graph_ref` (CompiledGraph), `self.name`, pool queries.

**Fix**: `TurnContextDescriptor` carries pre-built `graph_topology_section` and `graph_node_description` strings. BotAgentNode constructs them at descriptor creation time. Configurators are thin installers — they set the custom keys from descriptor fields, don't construct.

### N8: GraphDeliverTool cache preserved

**Verified**: ADR-0038 caches `GraphDeliverTool` on `BotAgentNode._deliver_tool` (topology stable across runs of same spec). Configurator receives pre-built tool via `desc.graph_deliver_tool` — thin installer, not constructor. Cache preserved, no framework→business dependency.

### N9: Event loop concurrency safety

- Turn A: `BotAgentNode.execute` calls `runner.execute_turn` (synchronous) — holds session lock
- Between turns: `BotAgentNode.execute` in `await event_queue.get()` — doesn't hold session lock — InboxPoller can safely drive turn B
- Turn B: InboxPoller → `pipeline.process_message` → `turn_runner.process_locked` — acquires session lock, drives turn B
- Temporary hook: on shared `hook_runner`, filtered by `session_id` — only processes this node's turn events

### N10: Streaming output support

The event loop supports streaming observation via:
1. **Hooks**: `_NodeLifecycleEventCollector` can implement `AfterIterationHook`, `AfterToolExecutionHook`, `AfterLLMResponseHook` (fire correctly regardless of turn driver)
2. **Emitter**: `CompositeEmitter` (existing) supports multiple consumers for streaming deltas

Hooks cover structural events; emitter covers content deltas. Both work across multiple turns within the event loop.

### N11: Crash recovery is an edge case

If the process crashes mid-event-loop (between turn A and turn B):
- `node.run`'s `finally` calls `finalize_invocation` (orphan RUNNING)
- On recovery, `bootstrap` re-runs the node
- CACHED session retains turn A's history
- `execute` detects `is_re_execution` (history exists) → skips input dispatch → goes straight to event loop

This is a degraded but functional recovery path. The primary design targets normal operation (no crash).

### N12: Turn A 结束后 node 状态 — execute 不返回

**Verified**: agent dispatch subagent 后 turn A 结束 → `actual_turn()` 的 finally 块执行 `FINALLY_GRAPH` dispatch → `_NodeLifecycleEventCollector.finally_graph` 触发 → `event_queue.put("turn_completed")` → execute 收到事件 → `_has_pending_delivers()` 返回 False → **execute 不返回，继续 `await event_queue.get()`**。

node.run 在 `await node.run()` 处等待 execute 返回 — 不触发 retry loop（execute 没返回）。graph 实例保持 RUNNING。asyncio 事件循环自由 — InboxPoller 可以驱动其他 turn。

### N13: Turn B 事件流可被 event_queue 捕获

**Verified**: 
1. `HookRunner` 是 pool-level 共享（`turn_runner.py:179-180`）
2. `_hook_specs` 是 mutable list（`runner.py:217`），支持运行时 `add`/`remove`（`runner.py:224-234`）
3. `dispatch` 每次调用遍历最新 `_hook_specs`（`runner.py:270`）— 不是快照
4. `_NodeLifecycleEventCollector` 在 execute 开始时 `add` 注册，`finally` 块 `remove` 注销
5. Turn B 由 InboxPoller 驱动 → 走同一 pool 的 pipeline → 同一 hook_runner → 临时 hook 在 turn B 的 `FINALLY_GRAPH` 中触发 → `event_queue.put("turn_completed")`
6. execute 的 `await event_queue.get()` 收到事件 ✅

### N14: 严格完成判定 — 先 deliver 后 turn end

**Critical**: 不能只看 `_has_pending_delivers()` 和 turn end 同时满足 — deliver 后 turn 可能还没结束。

**正确设计**: 维护 `_deliver_received` 标志，只在收到 "turn_completed" 事件后才检查：

```python
# deliver 调用时 (GraphDeliverTool.deliver):
self._deliver_received = True

# 收到 turn_completed 事件后:
if self._deliver_received and self._has_pending_delivers():
    return  # ✅ deliver + turn end = node 完成
# turn 结束但没 deliver → 继续监听
```

**顺序保证**: `_deliver_received` 在 deliver 调用时设置（turn 执行中），但**只在 "turn_completed" 事件处理时检查**（turn 结束后）。这避免了"deliver 后但 turn 还没结束就判定完成"的问题。

**多个 deliver**: `_has_pending_delivers()` 检查 `_pending_delivers` 列表非空。多个 deliver 都写入同一列表。至少一个 deliver + turn end = 完成。

### N15: 事件监听的 session_id 隔离

**Verified**: 临时 hook 注册在 pool 的 hook_runner 上（共享）。但有两层过滤确保不收到无关事件：

**第一层 — session_id 过滤**: `_NodeLifecycleEventCollector.finally_graph` 检查 `ctx.session.session_id != self._node_session_id` → 跳过其他 session 的 turn 事件。

**第二层 — 临时注册生命周期**: 临时 hook 只在 `BotAgentNode.execute` 期间存在（add → finally remove）。普通会话的 turn 不经过 `BotAgentNode.execute` → 没有临时 hook 注册 → 普通会话不受影响。

**隔离验证**:
- Graph node 的 turn A/B: session_id 匹配 → 推入 event_queue ✅
- 普通会话的 turn（不同 session）: session_id 不匹配 → 跳过 ✅
- **普通会话的 turn（同一 session，WebUI 消息）**: session_id 匹配 → 推入 event_queue → execute 检查 `_deliver_received` → False（WebUI turn 没有 deliver）→ 继续监听 ✅ (见 N16)
- Subagent 的 turn（同一 pool）: session_id 不匹配 → 跳过 ✅
- 其他 graph node 的 turn（并行 graph）: session_id 不匹配 → 跳过 ✅
- 并行 BotAgentNode.execute: 每个注册自己的临时 hook，hook_runner 有多个临时 hook，每个只处理自己的 session_id ✅

### N16: 同一 agent 同一 session 的图模式与会话模式并发隔离（进阶）

**场景**: 同一个 main agent、同一个 CACHED session，可以同时：
- 在图调度中（graph engine 驱动 `BotAgentNode.execute`，event loop 在 `await event_queue.get()` 等待 subagent）
- 被用户 WebUI 手动发消息（InputPipeline → InboxPoller → `pipeline.process_message` → 普通会话 turn）

**两个隔离层面**:

**层面 1 — 配置隔离（当前设计已满足）**:
- 普通 WebUI turn 的 `input_metadata` 没有 `graph_instance_id` → `turn_runner` 不 stamp registry → `build_runtime_and_context` resolve 返回 None → `desc.graph_context = None` → 所有 graph configurator `applies()=False` → 跳过
- 普通 WebUI turn 的 `AgentContext` 是 fresh per-turn：approval=enabled（pool 默认）、无 deliver tool、MAX_TURNS=pool 默认、无 topology
- 下一个 graph turn（turn B，subagent 结果回来）的 `input_metadata` 有 `graph_instance_id` → stamp → resolve → `desc.graph_context` set → configurator apply
- **配置不泄漏** ✅

**层面 2 — event loop 容忍（当前设计已满足）**:
- 普通 WebUI turn 在同一 session 上执行 → `FinallyGraphHook` 触发 → `_NodeLifecycleEventCollector` 捕获（session_id 匹配！） → `event_queue.put("turn_completed")`
- `BotAgentNode.execute` 收到 "turn_completed" → 检查 `_deliver_received` → False（WebUI turn 没有 deliver）→ 继续监听 ✅
- **event loop 正确容忍交错** — 不会因 WebUI turn 而错误判定 node 完成

**层面 3 — history 混合（当前设计的残留问题，进阶改进）**:
- 普通 WebUI turn 和 graph turn 共享同一个 CACHED session 的 history
- history 中会交错出现 graph turn 的消息和 WebUI turn 的消息
- 这不是技术隔离失败（per-turn AgentContext 是 fresh 的），而是**语义混乱** — agent 的历史记录混合了两种模式的上下文
- **当前不解决** — 作为 OQ4 保留扩展

**扩展保留**:
- `TurnContextDescriptor` 已有 `is_node_execution` 字段 — 未来可用于 history scope 分离
- registry 已 per-turn stamp/resolve — 未来可扩展为 per-session mode tracking
- configurator pipeline 已支持按 `agent_kind`/`is_node_execution` 分流 — 未来可加 `HistoryScopeConfigurator`

## Convergence verification (10 dimensions × 5 contexts = 50 points)

| Dimension | Normal main | Normal subagent | Graph node direct | Graph subagent (inbox) | External | Converges? |
|---|---|---|---|---|---|---|
| System Prompt Pipeline | pool wiring; GraphWorkflowProvider=空 | pool wiring; GraphWorkflowProvider=空 | pool wiring; GraphWorkflowProvider=注入 | pool wiring; GraphWorkflowProvider=注入 | N/A | ✅ |
| Hook System (14 hooks) | pool hooks; graph hooks skip | pool hooks; graph hooks skip | pool hooks; graph hooks active | pool hooks; graph hooks active | N/A | ✅ |
| Interceptor Chain | pool interceptors | pool interceptors | pool interceptors | pool interceptors | N/A | ✅ |
| Tool System | base tools | base tools (preset) | base + graph tools (Configurator) | base tools (preset) | no tools | ✅ |
| Multi-level Memory | main memory config | subagent memory config | same as main | same as subagent | external | ✅ |
| Approval | enabled | None (template) | None (Configurator) | None (Configurator) | N/A | ✅ |
| Governance | CompositeGovernance | ToolChainRepair only | same as main/subagent | same as subagent | N/A | ✅ |
| MCP Connections | pool shared | pool shared | pool shared (copied ref) | pool shared (copied ref) | N/A | ✅ |
| Workspace | pool workspace | pool workspace | pool workspace | pool workspace | external ws | ✅ |
| Control Channel | pool channel | pool channel | pool channel (derived) | pool channel (derived) | N/A | ✅ |

**Single switch**: `agent_context.graph_context` — all graph-aware components auto-activate when set. All non-graph components unaffected.

**Only 2 additive extensions** (non-breaking, conditional additions):
1. `SubagentDispatchStrategy.build_envelope`: appends `graph_instance_id` when `req.context.graph_context is not None`
2. `turn_runner._process_locked_inner`: stamps registry when `input_metadata["graph_instance_id"]` present

## Design closure findings (resolved)

| # | Finding | Resolution |
|---|---|---|
| F1 | Registry clear() no caller | Liveness-guarded resolve() + eager clear in _evict_dynamic_session |
| F2 | 4 custom keys orphaned | Full coverage: GraphToolConfigurator (knowledge keys) + GraphTopologyConfigurator (node desc) |
| F3 | Registry storage/access unspecified | _config_pipeline on TurnContextBuilder, _graph_context_registry on AgentPool, post-construction setter |
| F4 | DeliverTool configurator vs cache | Descriptor carries pre-built tool; configurator installs, not constructs |
| F5 | ADR §D3 vs §D4 contradiction | Resolved: event loop design replaces GraphInterrupt approach entirely |
| F6 | Stamp upsert semantics | Upsert (overwrite by session_id). CACHED reuse safe. |
| F7 | Stale stamp liveness | Liveness guard in resolve() |
| F8 | Subagent graph_context always None | Propagate graph_instance_id via envelope metadata → per-turn state.custom; hook reads state.custom, not ctx.graph_context |
| F9 | GraphInterrupt pauses entire graph | Don't use GraphInterrupt. Event loop in execute. |
| F10 | node.run retry loop | execute doesn't return without delivers → retry never triggers |
| F11 | InboxFlushHook must stay pure | Stamp in turn_runner._process_locked_inner + BotAgentNode.execute, not InboxFlushHook |
| F12 | Turn A 结束后 node 状态 | execute 不返回, 在 await event_queue.get() — node.run 不触发 retry, graph 保持 RUNNING (N12) |
| F13 | Turn B 事件流捕获 | HookRunner 支持运行时 add/remove, dispatch 遍历最新列表, 临时 hook 在 turn B 中触发 (N13) |
| F14 | 严格完成判定 | _deliver_received 标志只在收到 turn_completed 事件后检查, 避免提前判定 (N14) |
| F15 | 事件监听 session_id 隔离 | 临时 hook 按 session_id 过滤 + 只在 execute 期间注册, 普通会话/subagent/其他 graph node 都被过滤 (N15) |
| F16 | 同一 session 图/会话模式并发 | 配置隔离 (per-turn fresh AgentContext) + event loop 容忍交错 (WebUI turn 无 deliver → 继续监听) 已满足; history 混合是残留问题 (N16, OQ4) |

## Open questions (for future exploration)

### OQ1: ParallelScheduler per-node pause (future)

If future graph topologies need parallel nodes with independent HITL approval suspension (not subagent waiting), `NodeOnlyPolicy` (defined in `interrupt_policy.py` as a future subclass) would need implementation. This is separate from subagent waiting (which uses the event loop, not GraphInterrupt).

### OQ2: Peer agent communication (future)

Current design supports subagent (parent→child). Peer agent communication (agent→agent, same level) is future work. The metadata propagation mechanism (envelope.metadata) supports peers — a peer agent receiving a graph-aware message would get graph_context via the same registry stamp/resolve path. Business layer may not support peers yet, but the framework design doesn't preclude them.

### OQ3: Framework-level GraphAgentNode base class

If the design satisfies all requirements (R1-R13), generalize `BotAgentNode`'s event-loop pattern into a framework-level base class in `src/modex_agent/`. Base class provides: event loop, registry stamping, descriptor construction, temporary hook registration, streaming observation hooks. Subclasses override: topology rendering, knowledge config, deliver tool construction, timeout, input dispatch.

### OQ4: History scope separation for same-session graph/normal interleaving (future)

**Scenario**: same agent, same CACHED session, both graph scheduling turns and normal WebUI turns interleave in the session's history. This is a **semantic** concern (not a technical isolation failure): the agent's history mixes two modes' context.

**Current design**: per-turn `AgentContext` is fresh (config doesn't leak). But `ScopedMessageHistory` is per-session — both modes' messages accumulate in the same history.

**Future improvement options**:
- Separate history scopes: graph turns write to a graph-scoped history segment, normal turns write to a normal-scoped segment. The agent sees both but with clear delimiters.
- Session-level mode tracking: track "current mode" on the session. Graph turns and normal turns have distinct mode markers in history.
- `HistoryScopeConfigurator`: a new configurator that sets a history scope tag based on `desc.is_node_execution`.

**Extensibility preserved**: `TurnContextDescriptor.is_node_execution` already carries the mode signal. `TurnContextConfigPipeline` supports adding new configurators without changing existing ones. Registry stamp/resolve is per-turn — future mode tracking can build on this.

## Assumptions

1. `ReActAgent` remains stateless (verified: `agent.py:161-177`).
2. Tool instances don't hold per-session mutable state (verified: TodoStore routes by session_id to disk; GraphDeliverTool/KnowledgeTool fresh per turn; MCPTool/SubprocessTool/SendToAgentTool stateless).
3. External agents are not graph nodes (`BotAgentNode.execute` asserts `isinstance(runner, ReActTurnRunner)`).
4. `liveness_check` + `context_resolver` predicates available at assembly time (business layer captures orchestrator methods as closures).
5. Configurator pipeline crash is contained (per-turn AgentContext is boundary; no rollback needed).
6. Current graph topology is LinearScheduler (sequential). Event loop design also works for ParallelScheduler (execute not returning doesn't block siblings).

## Flip conditions

1. If `ReActAgent` gains mutable per-turn state → singleton assumption breaks → per-session rebuild needed.
2. If a tool gains per-session mutable state → configurator must fresh-construct it per turn.
3. If parallel graph needs per-node GraphInterrupt pause → implement `NodeOnlyPolicy`.
4. If external agents become graph nodes → ExternalTurnRunner needs configure seam.
5. If event loop crash recovery proves insufficient → add checkpoint mechanism for consumer state (which turn ran, whether subagent dispatched).

## Implementation phases

### Phase 1 — Pure refactor (no behavior change)

1. Define `TurnContextConfigurator` ABC + `TurnContextConfigPipeline` + `TurnContextDescriptor` (Pydantic).
2. Extract 6 Graph*Configurator classes from `BotAgentNode.execute` lines 168–239 (full coverage, all 8 custom keys).
3. Add `turn_descriptor` optional parameter to `build_runtime_and_context`; call `pipeline.configure()` at end (short-circuit when None).
4. `BotAgentNode.execute` constructs descriptor (with `graph_context=ctx` directly + pre-built deliver_tool/topology/description), passes to builder; deletes post-build mutation block.
5. Register empty pipeline for normal pools; graph configurator pipeline for pools with graph capability.
6. **Verify**: existing tests pass; `BotAgentNode.execute` behavior unchanged.

### Phase 2 — GraphContextRegistry (direct node path)

1. Add `GraphContextRegistry` + `GraphContextStamp` to `multi_agent/`.
2. Add `is_instance_active(gid)` + `get_graph_context(gid)` to `GraphOrchestrator`.
3. Inject registry into `AgentPool` + `TurnContextBuilder` (post-construction setters, with `liveness_check` + `context_resolver` closures).
4. `BotAgentNode.execute` stamps registry at entry.
5. `build_runtime_and_context` resolves stamp → fills `desc.graph_context`.
6. `AgentPool._evict_dynamic_session` calls `registry.clear(session_id)`.
7. **Verify**: graph-node-direct turns get graph config; normal sessions skip; stale stamps cleaned by liveness guard.

### Phase 3 — Event loop + envelope metadata (subagent path — P0-6 fix)

1. Rewrite `BotAgentNode.execute` as event loop: `runner.execute_turn` (turn A) → `await event_queue.get()` → (InboxPoller drives turn B) → `FinallyGraphHook` → event → check deliver → return.
2. Add `_NodeLifecycleEventCollector` (FinallyGraphHook, session_id-filtered).
3. `turn_runner._process_locked_inner` stamps registry when `input_metadata["graph_instance_id"]` present.
4. `SubagentDispatchStrategy.build_envelope` appends `graph_instance_id`/`source_node_id` when `req.context.graph_context is not None`.
5. `SubagentAutoSendHook` reads `state.custom[GRAPH_INSTANCE_ID]` → appends graph metadata to reply envelope.
6. `DeliverRetryHook` checks deliver tool existence before enforcing.
7. **Verify**: graph-scheduling subagent via inbox gets graph_context + approval=None + GRAPH_INSTANCE_ID; parent re-activation (turn B) gets full graph config; subagent result flushed to history; agent delivers; node completes. Normal subagent turns unaffected. InboxFlushHook unchanged.
