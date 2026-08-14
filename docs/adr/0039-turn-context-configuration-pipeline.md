# Turn Context Configuration Pipeline + Graph Agent Node Lifecycle

> **SUPERSEDED**: The authoritative design is now tracked in `docs/design/session-tree/issues/14-phase4-turn-context-config-pipeline.md` (T14). T14 refines this ADR's retained sections (§2 ConfigPipeline, §3 Descriptor, §5 propagation, §6 stamp sites, §7 configurator matrix, §8 mutation extraction, §9 single-switch, §10 DeliverRetryHook fix) with verified code exploration and risk mitigation design. Key refinements: method name `configure()` not `apply()`; `graph_artifacts` consolidated (3 fields → 1); `graph_instance_id` propagation via `SendStrategy.execute` template method (not `build_envelope` per-strategy); `GraphApprovalConfigurator.applies()` = `graph_instance_id is not None` (resolver stale-reference degradation). See T14 §6, §4, §11 for details. This ADR is retained for historical context only — do not update it; update T14 instead.

Per-turn runtime configuration (graph context binding, graph tools, approval disable, turn limits, topology, knowledge keys) flows through an ordered pipeline of configurators invoked at the end of `build_runtime_and_context`, modeled on `SystemPromptPipeline`. The agent object stays a pool-level singleton — it is stateless, and `build_runtime_and_context` already constructs a fresh `AgentContext` per turn. `agent_context.graph_context` is the single switch for the entire graph system — all graph-aware components (GraphWorkflowProvider, KnowledgeHook, DeliverRetryHook, GraphDeliverTool) auto-activate when it is set.

> **Superseded sections removed**: The original §1 (BotAgentNode.execute event loop), §4 (GraphContextRegistry), and §11 (_NodeLifecycleEventCollector) have been superseded by the SessionTree redesign (Phase 3: `tree.deliver` + `tree.wait_quiesce`; Phase 4: `ModexGraphContext` + resolver closure). See `docs/design/session-tree/issues/13-phase3-botagent-execute-rewrite.md` and `14-phase4-turn-context-config-pipeline.md` for the active design.

## Context

### The problem

`AgentPool._agents[name]` holds one pool-resident `AgentInstance` per agent. Its `pipeline` holds a `TurnContextBuilder` carrying pool-level configuration. `build_runtime_and_context` constructs a fresh `AgentContext` per turn.

Five distinct turn contexts exist:

| Context | How it arises | Graph config today |
|---|---|---|
| Normal-session main agent | user input → `AgentPipeline.receive` → `build_runtime_and_context` | N/A (pool config correct) |
| Normal-session subagent (inbox) | `InboxPoller` → `pool._process_message` → `build_runtime_and_context` | N/A (pool config correct) |
| Graph-node-direct | `LinearScheduler` → `BotAgentNode.execute` → `runner.execute_turn` → `build_runtime_and_context` | Post-build mutation in `BotAgentNode.execute` lines 168–239 |
| Graph-scheduling subagent (inbox) | graph node agent dispatches subagent → subagent runs via `InboxPoller` → `build_runtime_and_context` | **Missing (P0-6)** — subagent has no graph awareness |
| External agent | `ExternalTurnRunner.process_locked` | N/A |

Two problems:
1. **P0-6**: subagent dispatched from graph node has no `graph_context` → GraphWorkflowProvider empty, no deliver tool, approval may deadlock, SubagentAutoSendHook can't propagate graph metadata.
2. **Node lifecycle**: `BotAgentNode.execute` calls `runner.execute_turn` once (turn A). If agent dispatches subagent (async, fire-and-forget) and turn A ends without deliver, `execute` returns → `node.run` completes → graph advances → but agent hasn't received subagent result yet. The node's work is not done.

### Key constraint: subagent is NOT a graph node

Subagent is an agentNode **internal capability** — the agent calls task tool, dispatches a subagent that runs via the normal session inbox mechanism. The subagent is NOT registered as a graph node, NOT in the graph schedule, NOT driven by the graph engine. It runs as a normal session turn via `InboxPoller → pool._process_message → build_runtime_and_context`.

### Key constraint: cannot pause the entire graph

`GraphInterrupt` pauses the **entire graph instance** (ParallelScheduler D13 cancels all sibling nodes; LinearScheduler aborts the loop). A node waiting for an internal subagent must NOT pause the graph. The node's `execute` must handle the wait **internally**, without `GraphInterrupt`.

## Decision

### 2. TurnContextConfigPipeline — one configuration path

Model per-turn runtime configuration on `SystemPromptPipeline`:

```python
class TurnContextConfigurator(ABC):
    @abstractmethod
    async def applies(self, desc: TurnContextDescriptor) -> bool: ...
    @abstractmethod
    def apply(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None: ...

class TurnContextConfigPipeline:
    async def configure(self, ctx: AgentContext, desc: TurnContextDescriptor | None) -> None:
        if desc is None: return  # short-circuit for callers that omit descriptor
        for c in self._configurators:
            if await c.applies(desc):
                c.apply(ctx, desc)
```

`build_runtime_and_context` calls `pipeline.configure(agent_context, desc)` at the end. When `turn_descriptor` is None (existing callers), `configure()` short-circuits — behavior identical to today.

### 3. TurnContextDescriptor — Pydantic, carries all runtime dimensions

```python
class TurnContextDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    agent_name: str
    agent_kind: AgentCommKind                # MAIN / SUBAGENT
    execution_strategy: ExecutionStrategyKind  # REACT / EXTERNAL
    session_id: str
    workspace: Path | None
    pool_data: Any | None = None

    # Graph context (None = normal session)
    graph_context: GraphContext[Any] | None = None
    graph_node_name: str | None = None
    graph_instance_id: int | None = None
    is_node_execution: bool = False           # True: BotAgentNode.execute; False: inbox-driven

    # Pre-built graph artifacts (only for is_node_execution + MAIN)
    graph_deliver_tool: Any | None = None     # pre-built, preserves ADR-0038 cache
    graph_topology_section: str | None = None # pre-built topology markdown
    graph_node_description: str | None = None # pre-resolved role_description
```

**Pre-built artifacts**: BotAgentNode constructs topology/description/deliver_tool at descriptor construction time (it has `_graph_ref`, `self.name`, pool access). Configurators are thin installers — they don't construct, just install. This preserves the ADR-0038 cache on `BotAgentNode._deliver_tool` and avoids framework→business dependency.

### 5. Graph context propagation — via envelope metadata (not graph_context check)

**Critical finding**: `SubagentAutoSendHook` runs on the **subagent's** `AgentContext`, where `graph_context` is always None (subagent builds its own context via its own pipeline, not through `BotAgentNode.execute`). Checking `ctx.graph_context is not None` in the hook would **never fire** (false negative).

**Correct mechanism**: propagate `graph_instance_id` through envelope metadata → subagent per-turn state → hook reads per-turn state.

#### Propagation chain (two paths, both required):

**Path 1 — parent→subagent (TASK_REQUEST)**:
```
Graph node agent calls task tool → SubagentDispatchStrategy.build_envelope:
  req.context.graph_context is not None (graph node agent HAS graph_context)
  → envelope.metadata["graph_instance_id"] = req.context.graph_context.graph_instance_id
  → envelope.metadata["source_node_id"] = current node name
  → bus.send(subagent_session, envelope)
```

**Path 2 — subagent→parent (AGENT_RESULT)**:
```
Subagent completes → SubagentAutoSendHook.finally_graph:
  gid = ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_INSTANCE_ID)
  if gid is not None:
    → envelope.metadata["graph_instance_id"] = gid
    → envelope.metadata["source_node_id"] = ...
    → bus.send(parent_inbox, envelope)
```

**Subagent receives graph_instance_id via per-turn state**:
```
subagent receives TASK_REQUEST:
  turn_runner._process_locked_inner:
    reads input_metadata["graph_instance_id"]
    → stamps registry (with context_resolver to get GraphContext)
    → build_runtime_and_context:
      registry.resolve(subagent_session_id) → stamp
      → desc.graph_context = stamp.graph_ctx
      → GraphContextBindingConfigurator: agent_context.graph_context = ctx ✅
      → GraphApprovalConfigurator: approval = None ✅
      → state.custom[TurnCustomKey.GRAPH_INSTANCE_ID] = gid  ← for SubagentAutoSendHook
```

### 6. Stamp sites — NOT in InboxFlushHook

**InboxFlushHook stays purely mode-agnostic**: pulls messages, reads `reminder_kind`/`invocation_id` (message-formatting), appends to history. No graph logic, no registry stamp, no `graph_context` setting.

**Stamp happens in two existing orchestrators**:

| Turn path | Stamp site | Why |
|---|---|---|
| Inbox-driven (subagent result, graph subagent TASK_REQUEST) | `turn_runner._process_locked_inner` | Already reads `input_metadata`, already does side effects (session registration, etc.) |
| Graph-node-direct | `BotAgentNode.execute` | Already holds `ctx: GraphContext` |

**Resolve (pure read) in `build_runtime_and_context`**: `registry.resolve(session_id)` → fill `desc.graph_context` → configurators apply/skip. This is the single configuration convergence point.

### 7. Configurator hierarchy — subagent vs graph node

| Configurator | applies() | Graph node (MAIN, is_node_execution) | Graph subagent (SUBAGENT) | Normal session |
|---|---|---|---|---|
| `GraphContextBindingConfigurator` | `graph_context is not None` | ✅ set `ctx.graph_context` | ✅ set | ❌ skip |
| `GraphApprovalConfigurator` | `graph_context is not None` | ✅ `approval=None` | ✅ `approval=None` | ❌ skip |
| `GraphMaxTurnsConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ `MAX_TURNS=3` | ❌ skip | ❌ skip |
| `GraphToolConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ deliver+knowledge tool | ❌ skip | ❌ skip |
| `GraphTopologyConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ topology+node_desc | ❌ skip | ❌ skip |
| `GraphKnowledgeConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ knowledge keys | ❌ skip | ❌ skip |

**Subagent gets minimal graph config**: `graph_context` (for SubagentAutoSendHook/GraphWorkflowProvider) + `approval=None` (no user approval in graph scheduling) + `GRAPH_INSTANCE_ID` in per-turn state (for hook to propagate). **No** deliver tool, MAX_TURNS=3, topology, knowledge keys — subagent is an atomic capability provider.

**Future extensibility**: if subagents need graph tools in the future, add a `GraphSubagentToolConfigurator` with `applies() → graph_context is not None and agent_kind == SUBAGENT`. The pipeline pattern supports this without changing existing configurators.

### 8. BotAgentNode.execute post-build mutation fully replaced

The 70 lines (168–239) are extracted into 6 configurators with **full coverage** (all 8 `state.custom` keys):

| Configurator | Sets | Source lines |
|---|---|---|
| `GraphContextBindingConfigurator` | `agent_context.graph_context` | L185 |
| `GraphToolConfigurator` | GraphDeliverTool + KnowledgeTool via GraphToolPreset; GRAPH_KNOWLEDGE_DIR/REQUIRE_READ/REQUIRE_WRITE | L168-182, L230-239 |
| `GraphApprovalConfigurator` | `runtime.services.approval = None` | L202-203 |
| `GraphMaxTurnsConfigurator` | `state.custom[MAX_TURNS] = 3` | L223 |
| `GraphTopologyConfigurator` | GRAPH_TOPOLOGY_CONTEXT + GRAPH_NODE_DESCRIPTION | L224-229 |
| `GraphWorkspaceConfigurator` | `agent_context.workspace` (when not None) | (new) |

### 9. The single-switch principle

`agent_context.graph_context` is the sole switch for the entire graph system. Once set by `GraphContextBindingConfigurator`, 4 graph-aware components auto-activate:

| Component | Switch | Graph turn | Normal turn |
|---|---|---|---|
| `GraphWorkflowProvider` | `ctx.graph_context is None` | inject graph guidance | empty (skip) |
| `KnowledgeHook` | `ctx.graph_context is None → return` | reset + inject + enforce | skip (no-op) |
| `DeliverRetryHook` | deliver tool existence | enforce deliver | skip |
| `GraphDeliverTool.execute` | `ctx.graph_context is None` | works | not installed |

### 10. DeliverRetryHook modification

`DeliverRetryHook` currently does NOT check `ctx.graph_context` — it checks `deliver_count` and `MAX_TURNS`. For subagent turns (no deliver tool, MAX_TURNS defaults to 1), it returns after the first turn. But if pool default MAX_TURNS > 1, it would request continuation with deliver_count=0 → potential loop.

**Fix**: `DeliverRetryHook` checks deliver tool existence before enforcing:

```python
if ctx.tool_manager is None or ctx.tool_manager.get_tool("deliver") is None:
    return  # no deliver tool → not a graph node turn → don't enforce deliver
```

### 12. Streaming output support (future)

Streaming output observation via `CompositeEmitter` (existing) wrapping — supports multiple consumers. BotAgentNode can wrap the emitter to collect streaming deltas (`emit_delta`/`MODEL_OUTPUT`) for graph-level output forwarding. Emitter covers streaming content deltas across multiple turns.

## Considered Options

- **Per-session agent rebuild**: rejected. Agent is stateless; rebuilding buys no isolation, costs MCP/memory/tool re-wiring, doesn't solve P0-6.
- **GraphInterrupt for subagent waiting**: rejected. Pauses the **entire graph** (ParallelScheduler D13 cancels siblings; LinearScheduler aborts loop). A node's internal subagent wait must not affect graph scheduling.
- **InboxFlushHook as stamp site**: rejected. InboxFlushHook must stay purely mode-agnostic (pull messages + append to history). Stamp moved to `turn_runner._process_locked_inner` + `BotAgentNode.execute`.
- **`ctx.graph_context` as switch in SubagentAutoSendHook**: rejected. Subagent's `graph_context` is always None (subagent builds its own context). Correct switch is `state.custom[GRAPH_INSTANCE_ID]` set from envelope metadata.

## Consequences

- `build_runtime_and_context` gains optional `turn_descriptor` parameter + `configure` call at end. Existing callers unaffected (short-circuit when None).
- `TurnContextBuilder` gains `_config_pipeline` field (post-construction setter).
- `turn_runner._process_locked_inner` gains a stamp step: reads `input_metadata["graph_instance_id"]` → stamps registry (via `context_resolver` closure).
- `GraphOrchestrator` gains `is_instance_active(gid) → bool` + `get_graph_context(gid) → GraphContext | None` (read-only query methods).
- `SubagentDispatchStrategy.build_envelope` appends `graph_instance_id`/`source_node_id` when `req.context.graph_context is not None`.
- `SubagentAutoSendHook` reads `state.custom[GRAPH_INSTANCE_ID]` (not `ctx.graph_context`) → appends graph metadata to reply envelope.
- `InboxFlushHook` — **unchanged**. Pure mode-agnostic.
- `DeliverRetryHook` — checks deliver tool existence before enforcing.
- `BotAgentNode.execute` — 70 lines of post-build mutation deleted, replaced by descriptor construction + configurators.
- External agents — unaffected (don't go through `build_runtime_and_context`).

## Scope limitation

This design is validated for **LinearScheduler** (sequential graph) and **ParallelScheduler** (concurrent nodes). The configurator pipeline + descriptor approach is scheduler-agnostic — all turns go through `build_runtime_and_context` regardless of scheduler.

If per-node `GraphInterrupt` pause/resume is needed in the future (e.g. for HITL approval within a specific node without blocking siblings), `interrupt_policy.py` mentions `NodeOnlyPolicy` as a future extension concept — but this is a separate concern from subagent waiting.
