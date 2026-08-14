# T14: Phase 4 — TurnContextConfigPipeline + Configurators

> Type: `wayfinder:ticket`
> Status: **Active — design complete**
> Depends on: T13 (execute rewrite), T01 (SessionTree core)
> Refines: ADR-0039 §2, §3, §5, §6, §7, §8, §9, §10 (retained sections)

## Question

How does per-turn runtime configuration (graph context, graph tools, approval, turn limits, topology, knowledge keys) flow to `build_runtime_and_context` after Phase 3 removes the 70-line inline mutation from `BotAgentNode.execute`?

## Background

Phase 3 (T13) rewrites `execute` as a thin shell — turns go through `InboxPoller → _process_locked_inner → build_runtime_and_context` (the normal path). But `build_runtime_and_context` currently has no graph awareness. The 70 lines of mutation that installed deliver tool / graph_context / approval=None / MAX_TURNS / topology / knowledge keys must migrate to a configuration step at the end of `build_runtime_and_context`.

ADR-0039 proposed `TurnContextConfigPipeline` for this. Phase 4 refines and implements it. The "agent singleton + config-driven differentiation" principle from ADR-0039 is the core retained idea: one agent object (pool singleton), fresh `AgentContext` per turn, configurators install context-specific behavior.

## Resolution

### 1. TurnContextDescriptor — Pydantic frozen model

Carries all runtime dimensions. Constructed once per turn at `_process_locked_inner` (the single construction site for all turn types).

```python
class TurnContextDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    # Agent identity (from agent_descriptor, NOT from envelope)
    agent_kind: AgentCommKind                # MAIN / SUBAGENT

    # Execution strategy
    execution_strategy: ExecutionStrategyKind  # REACT / EXTERNAL

    # Graph context (None = normal session, no graph)
    graph_context: GraphContext[Any] | None = None
    graph_node_name: str | None = None
    graph_instance_id: int | None = None
    is_node_execution: bool = False           # True: BotAgentNode.execute path; False: inbox-driven

    # Pre-built graph artifacts (only when is_node_execution=True and agent_kind=MAIN)
    graph_artifacts: GraphTurnArtifacts | None = None
```

**Field sourcing**:
- `agent_kind`: from `_is_subagent()` / `agent_descriptor.comm_kind` (verified: `_is_subagent()` at turn_runner.py:461, `comm_kind` at turn_context_builder.py:441). **NOT from envelope metadata.** `comm_kind` is the agent's static property, covers all 4 turn scenarios correctly.
- `execution_strategy`: from runner type (ReActTurnRunner → REACT, ExternalTurnRunner → EXTERNAL).
- `graph_context`, `graph_node_name`, `graph_instance_id`, `is_node_execution`: from envelope `input_metadata` (graph fields written by SubagentDispatchStrategy / BotAgentNode.execute).
- `graph_artifacts`: from `ModexGraphContext._node_artifacts[node_name]` (resolved by `graph_context_resolver`).

**Removed from ADR-0039 §3**: `agent_name` (implicit), `session_id` / `workspace` / `pool_data` (stay as `build_runtime_and_context` params, not descriptor fields). Three separate pre-built fields (`graph_deliver_tool`, `graph_topology_section`, `graph_node_description`) consolidated into single `graph_artifacts` field.

### 2. GraphTurnArtifacts — pre-built by BotAgentNode

```python
class GraphTurnArtifacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    deliver_tool: Any          # pre-built, preserves ADR-0038 cache
    topology_section: str      # pre-built from BotAgentNode._graph_ref
    node_description: str      # pre-resolved from agent descriptor
    knowledge_config: Any      # BotAgentNode._knowledge_config
    knowledge_dir: Path | None = None
```

Built once by `BotAgentNode.execute` (it has `_graph_ref`, `self.name`, pool access), stored on `ModexGraphContext._node_artifacts[node_name]`. Configurators are thin installers — they read from descriptor, install on `AgentContext`. No construction in configurators.

### 3. TurnContextConfigPipeline — ordered configurators

**Pipeline model**: ordered configurators with `applies()` gate + `configure()` mutating `AgentContext` in place. This is a synthesis of three existing framework primitives (verified via codebase exploration of 11 pipeline/chain abstractions):

1. **`applies() -> bool` per-call predicate gate** — precedented by `_CommSubProvider.applies()` (providers.py:129) and `SkillCommandHandler.can_handle()` (handlers.py:235). The framework already endorses this gate shape.
2. **Mutating `AgentContext` in place** — precedented by `TurnContextBuilder.build_runtime_and_context()` itself (turn_context_builder.py:436-536) and `BotAgentNode.execute`'s post-build mutation (agent_node.py:168-239). `HookRunner.dispatch` also mutates `ctx` via side effects.
3. **Ordered list, registration order** — every pipeline in the framework uses list-position ordering (SystemPromptPipeline, UserInputPipeline, InterceptorChain, HookRunner, CompositeGovernance, ChainedContentFilter). None use priority/dependencies.

**Why not other models**:
- SystemPromptPipeline (read-only → string): cannot mutate AgentContext.
- HookRunner (isinstance type-gate): static, cannot express per-turn runtime predicates (e.g. "is this a subagent?", "is graph_context set?").
- UserInputPipeline (early-terminate): `Terminate` semantics don't fit "configure context" — a configurator that doesn't apply should be skipped, not stop the pipeline.
- InterceptorChain (onion wrapping): for call-wrapping, not sequential context configuration.

**Method name**: `configure()` (NOT `apply()`) — avoids semantic collision with `ContextGovernance.apply()` (core/governance.py:26) and `ContentFilter.apply()` (pipeline/filters.py:17) which both **return a new value** (copy-transform). `configure()` mutates in place — different contract, different name.

```python
class TurnContextConfigurator(ABC):
    @abstractmethod
    def applies(self, desc: TurnContextDescriptor) -> bool: ...
    @abstractmethod
    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None: ...

class TurnContextConfigPipeline:
    def __init__(self, configurators: list[TurnContextConfigurator]) -> None:
        self._configurators = configurators

    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor | None) -> None:
        if desc is None:
            return  # short-circuit for callers that omit descriptor
        for c in self._configurators:
            if c.applies(desc):
                c.configure(ctx, desc)
```

**applies() + configure() 都是 sync。** 所有 6 个 configurator 的 `applies()` gate 都是纯字段读取(`graph_context is not None`、`graph_instance_id is not None`、`is_node_execution and agent_kind == MAIN`)— 无 I/O,不需要 async。`configure()` 执行同步 mutation(tool_manager wrap、approval=None、state.custom 赋值)— 也无 I/O。

`build_runtime_and_context` 保持 sync。在方法末尾(emitter selection 之后,return 之前):
```python
if turn_descriptor is not None and self._config_pipeline is not None:
    self._config_pipeline.configure(agent_context, turn_descriptor)
```

When `turn_descriptor=None` (all existing non-graph callers) → short-circuit → behavior identical to today.

**Configurator ordering constraint**: `GraphContextBindingConfigurator` MUST run first — it sets `agent_context.graph_context`, which other configurators read to determine applicability and to install graph-specific tools/keys. Registration order in the pipeline list enforces this: ContextBinding at index 0.

**Hook point validation** (confirmed via code exploration of `build_runtime_and_context` lines 398-548):

Configurators run at the END of `build_runtime_and_context`, after ALL construction is complete. At this point, `agent_context` has: `system_prompt`, `history`, `tool_manager` (pool-level shared, unwrapped), `session`, `comm_kind`, `max_iterations`, `system_prompt_pipeline`, `identity`, `workspace_snapshot`, `workspace`, `runtime` (with services + state + custom keys). Configurators see a fully-built AgentContext — same state that `BotAgentNode.execute` sees today when it does its inline mutation.

### 4. Configurator matrix (6 configurators)

ADR-0039 §7 matrix retained unchanged. Ordering enforced by registration position in pipeline list:

| Order | Configurator | applies() | Graph node (MAIN, is_node_execution) | Graph subagent (SUBAGENT) | Normal session |
|---|---|---|---|---|---|
| 0 | `GraphContextBindingConfigurator` | `graph_instance_id is not None` | ✅ set `ctx.graph_instance_id`; set `ctx.graph_context` if resolver returns non-None | ✅ set `ctx.graph_instance_id`; set `ctx.graph_context` if resolver returns non-None | ❌ skip |
| 1 | `GraphApprovalConfigurator` | `graph_instance_id is not None` | ✅ `approval=None` | ✅ `approval=None` | ❌ skip |
| 2 | `GraphMaxTurnsConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ `MAX_TURNS=3` | ❌ skip | ❌ skip |
| 3 | `GraphToolConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ deliver+knowledge tool | ❌ skip | ❌ skip |
| 4 | `GraphTopologyConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ topology+node_desc | ❌ skip | ❌ skip |
| 5 | `GraphKnowledgeConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ knowledge keys | ❌ skip | ❌ skip |

**GraphContextBindingConfigurator applies() correction**: The gate is `graph_instance_id is not None`, NOT `graph_context is not None`. This handles the resolver stale-reference scenario (§11): when the graph has crashed and `_active_contexts` is cleaned, `resolver(gid)` returns None → `desc.graph_context = None`. But `desc.graph_instance_id` is still set (from envelope.metadata) — the subagent IS a graph subagent even if the graph is dead.

**GraphContextBindingConfigurator configure() behavior**:
- Always sets `ctx.graph_instance_id = desc.graph_instance_id` (so SubagentAutoSendHook can always read it, even if resolver failed).
- Sets `ctx.graph_context = desc.graph_context` only if resolver returned non-None (i.e., `desc.graph_context is not None`). When resolver failed, `graph_context` stays None — ReAct graph components (GraphWorkflowProvider, KnowledgeHook, DeliverRetryHook, GraphDeliverTool) correctly skip.

This ensures: SubagentAutoSendHook always reads `ctx.graph_instance_id` correctly (§6.4), regardless of resolver success/failure. In the stale-reference scenario, the hook reads `ctx.graph_instance_id` (set by configurator) and propagates it back to the parent — the parent's tree receives the result with graph metadata, and stale tracks are cleaned by Phase 0-2's `recover_tree`.

**Ordering constraint**: `GraphContextBindingConfigurator` (index 0) MUST run before all others — it sets `agent_context.graph_context`, which other configurators' `applies()` checks depend on. Since `applies()` reads `desc.graph_context` (not `ctx.graph_context`), this constraint is about the `configure()` side-effect: ContextBinding sets `ctx.graph_context` so that execution-time components (GraphDeliverTool, GraphWorkflowProvider, KnowledgeHook) find it. The `applies()` gate itself reads from the descriptor, not from ctx, so ordering doesn't affect gate evaluation — but the side-effect ordering matters for any configurator that reads `ctx.graph_context` during `configure()`.

**Subagent gets minimal graph config**: graph_context (for SubagentAutoSendHook / GraphWorkflowProvider) + approval=None (no deadlock) + GRAPH_INSTANCE_ID in per-turn state (for hook propagation). No deliver tool, MAX_TURNS, topology, knowledge keys — subagent is atomic capability provider.

**Note on ADR-0039 §8 GraphWorkspaceConfigurator**: ADR-0039 §8 table lists a 7th `GraphWorkspaceConfigurator`. `build_runtime_and_context` already has a `workspace` parameter (turn_context_builder.py:407) that sets `agent_context.workspace`. No configurator needed — workspace is handled by the existing param. GraphWorkspaceConfigurator dropped.

#### 4.1 Post-build mutation validation (verified via code exploration)

Configurators run at END of `build_runtime_and_context` (after line 546, before return at line 548). Each mutation target is confirmed as a valid post-build extension point — `BotAgentNode.execute` already performs the exact same mutations post-build today (agent_node.py:168-239):

| Configurator | Mutates | Current precedent | Post-build viable? |
|---|---|---|---|
| GraphContextBinding | `ctx.graph_context` | agent_node.py:185 (`agent_context.graph_context = ctx`) | ✅ build never sets it (defaults None) |
| GraphApproval | `ctx.runtime.services.approval` | agent_node.py:203 (`approval = None`) | ✅ `AgentRuntimeServices` is mutable dataclass |
| GraphMaxTurns | `ctx.runtime.state.custom[MAX_TURNS]` | agent_node.py:223 | ✅ `state.custom` is a dict |
| GraphTool | `ctx.tool_manager` (wrap) | agent_node.py:182 (`preset.build_tool_manager(tm)`) | ✅ read → wrap → replace pattern established |
| GraphTopology | `ctx.runtime.state.custom[GRAPH_*]` | agent_node.py:224-227 | ✅ `state.custom` dict |
| GraphKnowledge | `ctx.runtime.state.custom[GRAPH_KNOWLEDGE_*]` | agent_node.py:230-239 | ✅ `state.custom` dict |

**`tool_manager` wrapping** (GraphToolConfigurator): `build_runtime_and_context` sets `tool_manager = self._tool_manager` (pool-level shared, unwrapped) at line 439. Configurator reads `agent_context.tool_manager`, builds wrapping `ToolManager` via `GraphToolPreset.build_tool_manager(existing)`, assigns back. This is the exact pattern `BotAgentNode.execute` uses at line 182 — proven safe. Nothing in build after line 439 reads `tool_manager`.

**`graph_context`** (GraphContextBindingConfigurator): `build_runtime_and_context` does NOT set `graph_context` (confirmed via grep — only production assignment is agent_node.py:185). Field defaults to `None` (core/agent.py:101). Configurator sets it; execution-time consumers (`graph_deliver.py:215`, hooks) find it. No build-time dependency violated.

**`emitter`** — **DECIDED: no configurator for emitter in Phase 4.** `build_runtime_and_context` selects emitter at lines 538-546 and returns it as a separate tuple element — `agent_context.emitter` stays `None` in the ReAct path. Emitter selection does NOT depend on `graph_context`. Current `BotAgentNode.execute` does NOT override emitter (uses pool default). Phase 3-4 matches this behavior. If graph-specific emitter becomes necessary in the future (e.g. for ADR-0039 §12 streaming), add a configurator then — YAGNI.

**`current_input` stale docstring`** — **DECIDED: fix the docstring.** `core/agent.py:119-125` documents `current_input` as "set by `build_runtime_and_context`", but grep confirms it is NOT set there (stays `None`). Fix the docstring to reflect reality. Do NOT set the field — ReAct agents use history; external agents will be addressed when external agent integration needs it. Tracked in T15.

### 5. _process_locked_inner — single descriptor construction site

After Phase 3 rewrite, ALL ReAct turns (graph node / graph subagent / normal main / normal subagent) go through `_process_locked_inner` (turn_runner.py:424). This is the single descriptor construction point.

**External agent turns**: `ExternalTurnRunner` (external/turn_runner.py:76) has its own `process_locked` (L135) that does NOT call `_process_locked_inner` or `build_runtime_and_context`. The configurator pipeline does NOT apply to external turns — this is correct: external agents don't have ReAct graph components (GraphWorkflowProvider, KnowledgeHook, DeliverRetryHook, GraphDeliverTool). External agent graph integration is via `agent_context.graph_instance_id` (§6.3) — `ExternalTurnRunner` reads `graph_instance_id` from `input_metadata` and sets `agent_context.graph_instance_id = gid`. This lets `SubagentAutoSendHook` uniformly read `ctx.graph_instance_id` (§6.4) for both ReAct and External subagents.

Current `_process_locked_inner` already:
- Reads `input_msg.metadata` at L448 (where graph_instance_id will be)
- Has `_is_subagent()` check at L461
- Has `pool_data` at L452 (resolver injection source)
- Calls `build_runtime_and_context` at L523

**Addition** (before `build_runtime_and_context` call):
```python
desc = self._build_turn_descriptor(input_metadata, session, pool_data)
agent_context, emitter = self._builder.build_runtime_and_context(
    session, context_state, ctx_mgr,
    input_metadata=input_metadata, pool_data=pool_data,
    turn_descriptor=desc,  # NEW param
    inline_attachments=input_msg.attachments_resolved,
    workspace=input_msg.workspace,
)
```

`_build_turn_descriptor` logic:
1. `agent_kind` = SUBAGENT if `_is_subagent()` else MAIN
2. `execution_strategy` = REACT (ReActTurnRunner) or EXTERNAL (ExternalTurnRunner)
3. `graph_instance_id` = `input_metadata.get("graph_instance_id")`
4. `graph_node_name` = `input_metadata.get("graph_node_name")`
5. `is_node_execution` = `input_metadata.get("is_node_execution", False)`
6. If `graph_instance_id` is not None:
   - `graph_context` = `self._graph_context_resolver(gid)` (closure → `orchestrator.get_graph_context`)
   - If graph_context is ModexGraphContext and `graph_node_name` is set:
     - `graph_artifacts` = `graph_context.get_node_artifacts(graph_node_name)`
7. Else: `graph_context=None`, `graph_artifacts=None`

### 6. Unified graph_instance_id data flow

**Design principle**: separate propagation (int, serializable) from resolution (GraphContext object, needs resolver). All communication paths propagate `graph_instance_id` via `envelope.metadata`. ReAct receivers resolve to full `ModexGraphContext` via resolver; External receivers use a lightweight proxy (no resolution, no three-component activation).

**Peer communication excluded**: graph scheduling shields peer agents — communication tools (`task` peer direction, `send_to_agent` to non-parent) are disabled in graph mode at the business layer. `PeerNormalStrategy` is NOT in the propagation scope. This is a deliberate constraint: graph subagents must not communicate with peers, only with their parent (graph node agent).

#### 6.1 Architecture

```
┌─ Sender (has graph_instance_id) ─────────────────────────────────┐
│  ctx.graph_instance_id → envelope.metadata                       │
│  Injection: SendStrategy.execute (post-build_envelope)           │
│  Covers: SubagentDispatch + ParentReply (+ SubagentAutoSendHook) │
└──────────────────────────┬───────────────────────────────────────┘
                           │ envelope.metadata["graph_instance_id"] = 42
                           ▼
┌─ tree.deliver (converged sink — passes through, no injection) ───┐
└──────────────────────────┬───────────────────────────────────────┘
                           │
               ┌────────────┴────────────┐
               ▼                         ▼
┌─ ReAct receiver ──────────┐  ┌─ External receiver ──────────────┐
│ _process_locked_inner:     │  │ ExternalTurnRunner.process_locked:│
│   read metadata.gid        │  │   read metadata.gid               │
│   → resolver(gid)          │  │   → agent_context.graph_instance_id│
│     = ModexGraphContext    │  │     = gid (int, no GraphContext)  │
│   → configurators install  │  │   → SubagentAutoSendHook reads    │
│   → three components active│  │     ctx.graph_instance_id         │
│                            │  │     → writes AGENT_RESULT.metadata│
└────────────────────────────┘  └──────────────────────────────────┘
```

**Design principle**: `graph_instance_id` is a first-class field on `AgentContext` (`int | None`), NOT an attribute hidden inside `GraphContext`. This avoids the need for `_LightGraphContext` (a lightweight GraphContext subclass that would require dummy state/runtime/coordinator). `graph_context` (the `GraphContext[Any]` object) remains the sole switch for ReAct graph components (GraphWorkflowProvider, KnowledgeHook, DeliverRetryHook, GraphDeliverTool) — only set for ReAct graph turns. `graph_instance_id` (the int) is the graph-association marker — set for both ReAct and External graph subagents.

#### 6.2 Injection points (4 propagation sites + 1 origin)

**Origin point**: `BotAgentNode.execute` constructs the graph input envelope (`_build_graph_input_envelope`) with `metadata: {graph_instance_id, graph_node_name, is_node_execution=True}`. This is the ORIGIN of graph_instance_id in the communication path — graph → main agent. It's not a "propagation" site (it doesn't propagate from a sender; it reads from `ctx.graph_instance_id` at construction). T13 execute skeleton L34 documents this.

**Key correction (verified via code exploration)**: `_deliver` (base.py:164) is NOT the converged injection point — `PeerNormalStrategy` overrides `deliver` (peer_normal.py:72) and bypasses `_deliver` entirely. The truly converged point is `SendStrategy.execute` (base.py:73-89), the template method inherited by ALL three strategies. Specifically the seam between `build_envelope` (L79) and `deliver` (L80).

`AgentMessageEnvelope` is a plain `@dataclass` (NOT frozen, NOT Pydantic). `metadata: dict[str, Any]` is a regular mutable dict. Post-construction mutation `envelope.metadata["key"] = value` works — proven by `_TracePropagatingPeerNormal` (service.py:82) which does exactly `envelope.metadata["traceparent"] = traceparent`.

| # | Site | File:Line | Covers | Logic | Lines |
|---|------|-----------|--------|-------|-------|
| 1 | `SendStrategy.execute` (between build_envelope and deliver) | base.py:79-80 | SubagentDispatch + ParentReply + PeerNormal (ALL three strategies) | `if req.context.graph_instance_id is not None: envelope.metadata["graph_instance_id"] = req.context.graph_instance_id` | +3 |
| 2 | `SubagentAutoSendHook._notify_parent` | subagent_auto_send.py:480 | AGENT_RESULT (subagent→parent auto-reply, bypasses SendStrategy) | `if ctx.graph_instance_id is not None: envelope.metadata["graph_instance_id"] = ctx.graph_instance_id` | +3 |
| 3 | `ExternalTurnRunner.process_locked` | external/turn_runner.py:166-178 | External agent receives graph_instance_id | `gid = input_metadata.get("graph_instance_id"); if gid is not None: agent_context.graph_instance_id = gid` | +3 |
| 4 | `modexctl SendRequest` + `facade.send` | models.py:138 / facade.py:369 | CLI send carries graph_instance_id | SendRequest adds `graph_instance_id: int \| None = None` field; facade sets `agent_context.graph_instance_id = gid` | +model field +2 |

**Site 1 is the converged injection for SendStrategy paths.** One line in `execute` (base.py:79-80) covers SubagentDispatch + ParentReply + PeerNormal. No per-strategy subclassing needed (unlike the `_TracePropagatingPeerNormal` precedent which is per-strategy — that pattern is correct for traceparent which is peer-only, but wrong for graph_instance_id which must cover all strategies).

**Site 2 is independent** — SubagentAutoSendHook constructs its envelope inline (subagent_auto_send.py:469-481) and calls `tree.deliver` directly (L484), bypassing SendStrategy entirely. This is NOT converged by Site 1. Phase 0-2 has NOT converged SubagentAutoSendHook to SendStrategy (verified: hook still calls tree.deliver directly). Site 2 remains a separate injection point.

**PeerNormalStrategy exclusion**: graph mode shields peer communication at the business layer (task tool / send_to_agent reject peer targets when `graph_context is not None`). PeerNormalStrategy is NOT used in graph scenarios. Site 1's `if req.context.graph_context is not None` check naturally skips peer sends in normal mode (graph_context is None for normal sessions). No explicit PeerNormal exclusion needed in the injection logic.

#### 6.3 AgentContext.graph_instance_id field (replaces _LightGraphContext)

`AgentContext` gains a new first-class field:

```python
# core/agent.py — AgentContext dataclass
graph_instance_id: int | None = None    # NEW — graph-association marker
```

**Semantics**:
- `graph_context: GraphContext[Any] | None` (existing, unchanged) — the full graph engine context object. Only set for ReAct graph turns (by `GraphContextBindingConfigurator`). This is the sole switch for ReAct graph components (GraphWorkflowProvider, KnowledgeHook, DeliverRetryHook, GraphDeliverTool).
- `graph_instance_id: int | None` (NEW) — the graph instance ID as a serializable int. Set for both ReAct graph turns (by `GraphContextBindingConfigurator`, alongside `graph_context`) AND External graph subagents (by `ExternalTurnRunner`, without `graph_context`). This is the sole source for `SubagentAutoSendHook` to propagate graph_instance_id back.

**Why not `_LightGraphContext(GraphContext[Any])`?** A `GraphContext` subclass requires `state: S` + `runtime: GraphRuntime` + `coordinator: GraphPersistenceCoordinator` (all mandatory `__init__` params). Creating dummy objects for these is wasteful and fragile — any code that accesses `ctx.graph_context.state` or `ctx.graph_context.coordinator` on an external turn would hit dummy objects. A separate `int | None` field is simpler, safer, and doesn't abuse polymorphism.

**Who sets what**:

| Turn scenario | `graph_context` | `graph_instance_id` | Set by |
|---|---|---|---|
| Normal main | None | None | (defaults) |
| Normal subagent | None | None | (defaults) |
| Graph node main (ReAct) | ModexGraphContext (full) | gid (int) | `GraphContextBindingConfigurator` |
| Graph subagent (ReAct) | ModexGraphContext (full) | gid (int) | `GraphContextBindingConfigurator` |
| Graph subagent (External) | None | gid (int) | `ExternalTurnRunner.process_locked` |
| modexctl send | None | gid (int) | `facade.send` |

#### 6.4 SubagentAutoSendHook — unified read logic

Both ReAct and External subagents read `ctx.graph_instance_id` (the first-class field, NOT `ctx.graph_context.graph_instance_id`):

```python
# In SubagentAutoSendHook._notify_parent:
gid = ctx.graph_instance_id  # one field, one read, no type check, no fallback
if gid is not None:
    envelope.metadata["graph_instance_id"] = gid
```

**One code path, no ReAct/External branch for graph_instance_id.** ReAct subagent has `graph_instance_id` set by `GraphContextBindingConfigurator` (alongside `graph_context`). External subagent has `graph_instance_id` set by `ExternalTurnRunner` (without `graph_context`). Both expose the same `int | None` field.

#### 6.5 External agent — environment variable bridge

External agents (opencode) already have a graph_instance_id bridge via environment variables:

- `ExternalEnvSpec.task_id` = `str(graph_instance_id)` (types.py:178) → `MODEX_TASK_ID` env var
- `MODEX_WORKFLOW_ID`, `MODEX_NODE_ID` also set by `ExternalEnvBuilder` when spawned by `BotAgentNode`
- `modexctl deliver` already reads `MODEX_TASK_ID` (deliver.py:120-124)

**SSE child discovery**: opencode's internal subagent forks (via SSE `session.created` with `parentID`) inherit parent's environment variables. Child env snapshots (agent.py:733-741) set `comm_kind=SUBAGENT` + `parent_session_id`. The `MODEX_TASK_ID` env var is inherited automatically → child external subagent also has graph_instance_id.

**modexctl send convergence**: SendRequest (models.py:138) adds `graph_instance_id: int | None = None` field. When set, `facade.send` sets `agent_context.graph_instance_id = gid` → SendStrategy.execute reads it → propagates to envelope.metadata. This converges CLI send with agent-to-agent communication.

**modexctl deliver already works**: `deliver.py` carries `--graph-instance-id` (defaults to `MODEX_TASK_ID`). No change needed.

#### 6.6 Propagation coverage matrix

| Communication path | Direction | Msg Type | Injection site | graph_instance_id propagated? |
|---|---|---|---|---|
| SubagentDispatchStrategy | parent→subagent | TASK_REQUEST | Site 1 (SendStrategy.execute) | ✅ |
| ParentReplyStrategy | subagent→parent (explicit tool) | AGENT_MESSAGE | Site 1 (SendStrategy.execute) | ✅ |
| PeerNormalStrategy | peer→peer | AGENT_MESSAGE | **EXCLUDED** — graph mode shields peers | ❌ (by design) |
| SubagentAutoSendHook | subagent→parent (auto) | AGENT_RESULT | Site 2 (or absorbed by Site 1) | ✅ |
| modexctl send | CLI→agent | (routed via strategies) | Site 4 (SendRequest field) | ✅ |
| modexctl deliver | CLI→graph | (REST direct) | Already works via MODEX_TASK_ID | ✅ (no change) |
| pool.submit_input | human→agent | EXTERNAL_INPUT | N/A (not graph context) | ❌ (out of scope) |
| SSE child discovery | opencode internal fork | (provider-native) | Environment variable inheritance | ✅ (MODEX_TASK_ID inherited) |

### 7. Single-switch principle (retained from ADR-0039 §9)

`agent_context.graph_context` is the sole switch. Once set by `GraphContextBindingConfigurator`, 4 components auto-activate:

| Component | Switch | Graph turn | Normal turn |
|---|---|---|---|
| `GraphWorkflowProvider` | `ctx.graph_context is None` | inject graph guidance | empty (skip) |
| `KnowledgeHook` | `ctx.graph_context is None → return` | reset + inject + enforce | skip (no-op) |
| `DeliverRetryHook` | deliver tool existence | enforce deliver | skip |
| `GraphDeliverTool.execute` | `ctx.graph_context is None` | works | not installed |

### 8. DeliverRetryHook fix (retained from ADR-0039 §10)

Current DeliverRetryHook checks `deliver_count` + `max_turns` only. For subagent turns (no deliver tool, MAX_TURNS defaults), it returns after first turn. But if pool default MAX_TURNS > 1, it would request continuation with deliver_count=0 → potential loop.

**Fix**: check deliver tool existence before enforcing:
```python
if ctx.tool_manager is None or ctx.tool_manager.get_tool("deliver") is None:
    return  # no deliver tool → not a graph node turn → don't enforce deliver
```

Verified: current DeliverRetryHook does NOT have this check — this is the addition.

### 9. GraphContextResolver — liveness

`GraphOrchestrator._active_contexts: dict[int, GraphContext]` stores context at `run_instance` (L330), clears at `finalize` (in `_finalize_instance`).

- `get_graph_context(gid) → GraphContext | None`: returns from `_active_contexts`, or None if finalized/evicted.
- `is_instance_active(gid) → bool`: `gid in self._active_contexts`.

**Liveness is acceptable**: In normal flow, graph finalizes AFTER `BotAgentNode.execute` returns (execute's `wait_quiesce` blocks until all subagents complete → execute returns → node.run completes → graph advances → finalize). So subagent turns always find the graph still RUNNING. In abnormal flow (crash), resolver returns None → descriptor.graph_context=None → configurators skip → subagent runs without graph config, but tree's track fallback prevents deadlock.

### 10. ModexGraphContext._node_artifacts clearing

`ModexGraphContext` instances are owned by `_active_contexts`. `finalize` clears `_active_contexts` → ModexGraphContext dropped → `_node_artifacts` GC'd with it. No separate clearing path needed.

## Coverage — 5 turn scenarios

| Turn scenario | Path | graph_context source | configurators | graph_instance_id propagated back? |
|---|---|---|---|---|
| Normal main | InboxPoller → _process_locked_inner | None | all skip | N/A |
| Normal subagent | InboxPoller → _process_locked_inner | None | all skip | N/A |
| Graph node main (ReAct) | BotAgentNode.execute → tree.deliver → InboxPoller → _process_locked_inner | resolver → ModexGraphContext (full) | all 6 apply | ✅ via SubagentAutoSendHook / SendStrategy |
| Graph subagent (ReAct) | (parent dispatch) → InboxPoller → _process_locked_inner | resolver → ModexGraphContext (full) | ContextBinding + Approval | ✅ via SubagentAutoSendHook (Site 2) |
| Graph subagent (External) | (parent dispatch) → InboxPoller → ExternalTurnRunner.process_locked | `graph_instance_id` field (int, no GraphContext) | none (bypasses configurator pipeline) | ✅ via SubagentAutoSendHook (unified read, §6.4) |
| Graph subagent (External, SSE child) | opencode internal fork → inherits MODEX_TASK_ID env | `graph_instance_id` from env (via ExternalEnvSpec) | none | ✅ via SubagentAutoSendHook + env inheritance (§6.5) |
| modexctl send | CLI → facade → SendStrategy → tree.deliver | `graph_instance_id` field (from SendRequest) | depends on receiver path | ✅ via SendStrategy.execute (Site 1) |

## ADR-0039 relationship

ADR-0039's retained sections (§2 ConfigPipeline, §3 Descriptor, §5 propagation, §6 stamp sites, §7 configurator matrix, §8 mutation extraction, §9 single-switch, §10 DeliverRetryHook fix) ARE the Phase 4 design. ADR-0039's废弃 sections (§1 event loop, §4 GraphContextRegistry, §11 _NodeLifecycleEventCollector) have been deleted from ADR-0039 — superseded by SessionTree Phase 3 (T13: tree.deliver + wait_quiesce + ModexGraphContext + resolver).

**Refinements from ADR-0039 §3**: agent_kind sourcing changed (from envelope → from `_is_subagent()`); graph_artifacts consolidated (3 separate fields → 1); session_id/workspace/pool_data removed (stay as build params).

## Verification

- `build_runtime_and_context` (turn_context_builder.py:398-548) — NO `turn_descriptor` param today. Phase 4 adds it. Configurator pipeline runs at END (after emitter selection L538-546, before return L548).
- `_process_locked_inner` (turn_runner.py:424) — NO graph_instance_id reading. Phase 4 adds it.
- `_is_subagent()` (turn_runner.py:461) — EXISTS, returns `agent_descriptor.comm_kind == SUBAGENT`.
- `agent_descriptor.comm_kind` (turn_context_builder.py:441) — EXISTS.
- `SendStrategy.execute` (base.py:77-93) — NO graph_instance_id injection post-build_envelope. Phase 4 §6.2 Site 1 adds it.
- `SubagentAutoSendHook._notify_parent` (subagent_auto_send.py:463) — does NOT read graph_context.graph_instance_id. Phase 4 §6.2 Site 2 adds it (may be absorbed by Site 1 if Phase 0-2 converges hook to SendStrategy).
- `ExternalTurnRunner.process_locked` (external/turn_runner.py:166-178) — does NOT read graph_instance_id from metadata, does NOT set graph_context. Phase 4 §6.2 Site 3 adds it.
- `modexctl SendRequest` (models.py:138) — NO graph_instance_id field, `extra="forbid"`. Phase 4 §6.2 Site 4 adds the field.
- `modexctl deliver` (deliver.py:120-124) — ALREADY carries graph_instance_id via MODEX_TASK_ID. No change needed.
- `ExternalEnvSpec.task_id` (types.py:178) — ALREADY documents `task_id = str(graph_instance_id)`. No change needed.
- `DeliverRetryHook` (deliver_retry.py) — does NOT check deliver tool existence. Phase 4 adds it.
- `GraphOrchestrator` — NO `_active_contexts` / `get_graph_context`. Phase 4 adds them.
- `PeerNormalStrategy` (peer_normal.py:41) — NOT in propagation scope (graph mode shields peers, §6 design constraint).
- **Post-build mutation validated**: `BotAgentNode.execute` (agent_node.py:168-239) already performs all 6 configurators' mutations post-build today. Configurator pipeline formalizes this existing ad-hoc pattern. All mutation targets (`tool_manager`, `graph_context`, `runtime.services.approval`, `runtime.state.custom[*]`) are mutable post-build.
- **`tool_manager` wrapping validated**: `GraphToolPreset.build_tool_manager(existing)` at agent_node.py:182 is the proven read→wrap→replace pattern. Configurator uses same pattern.
- **`graph_context` NOT set by build**: confirmed via grep — only production assignment is agent_node.py:185 (post-build). Field defaults to None (core/agent.py:101).
- **`emitter` — open item**: returned as tuple element (line 548), `agent_context.emitter` stays None in ReAct path. Graph nodes currently use pool default emitter. Not yet decided whether Phase 4 needs an emitter configurator. See §4.1.
- **Pipeline model precedented**: `applies()` gate from `_CommSubProvider.applies()` (providers.py:129) + `SkillCommandHandler.can_handle()` (handlers.py:235). Mutating AgentContext from `TurnContextBuilder` itself. Ordered list from all framework pipelines.
- **`current_input` stale docstring — open item**: core/agent.py:119-125 claims set by build, but NOT set. Not yet decided: fix docstring vs. set the field. See §4.1.

### 11. Risk mitigation design

#### 11.1 Resolver stale-reference degradation

**Scenario**: Graph crashes/terminates → `GraphOrchestrator._active_contexts` cleaned (finally block pops gid) → `resolver(gid)` returns `None` → `desc.graph_context = None`. But `desc.graph_instance_id` is still set (from envelope.metadata — the subagent was dispatched before the graph died).

**Degradation behavior**:
- `GraphContextBindingConfigurator`: `applies() = graph_context is not None` → **skips** (nothing to bind — correct)
- `GraphApprovalConfigurator`: `applies() = graph_instance_id is not None` → **fires** (`approval=None` — prevents deadlock, see §4 matrix correction)
- `GraphMaxTurns/Tool/Topology/Knowledge`: `applies() = is_node_execution and agent_kind == MAIN` → **skips** (no artifacts to install without ModexGraphContext — correct, the subagent runs as a bare agent)
- `SubagentAutoSendHook`: reads `ctx.graph_instance_id` → `GraphContextBindingConfigurator` always sets it (even when resolver fails) → `gid` is set → graph_instance_id IS in AGENT_RESULT metadata. The hook fires correctly; the parent's tree receives the result with graph metadata. Stale tracks 由 Phase 0-2 的 `recover_tree` + `on_session_evicted` 清理。

**Net effect**: subagent runs without graph config (no deliver tool, no topology, no knowledge keys) but also without approval deadlock. The subagent completes naturally, its result is delivered to the parent's tree. No infinite wait, no deadlock.

#### 11.2 Concurrency safety summary

Verified via code exploration (6 risk points analyzed):

| Risk | Status | Mitigation |
|------|--------|------------|
| SessionTreeManager shared state (`_running`/`_pending_input`/`_quiesce_events`) | SAFE | asyncio 单线程 — 同步 set/dict 操作在 await 之间原子。Stores 有 `asyncio.Lock` 保护自身的 read-modify-write。 |
| `ctx.current_invocation` race under ParallelScheduler | FIXED (T15-2) | 删除字段;所有读取用 `get_execution()` ContextVar (task-local)。 |
| `reached_end` race | SAFE | 单调布尔 (False→True, 运行中不重置)。只在 `run_async` 返回后读。 |
| `Node._pending_delivers` stale-turn write | SAFE | Per-node serial gate 序列化 `run()`。Stale InboxPoller turn 调 `tree.deliver()` (manager), 不是 `node.deliver()` (Node) — 不同对象。 |
| InboxPoller dispatch per-session overlap | SAFE | 结构性 single-flight via `_inflight` dict。跨 session 并发,per-session 串行。 |
| `_active_contexts` resolver vs finally race | SAFE | asyncio 单线程 — dict.get/dict.pop 同步原子。Resolver 返回 None → 降级 (§11.1)。 |

## 不做的设计 (Explicitly Rejected)

以下设计在探索过程中被考虑过但最终否决,列出以避免后续理解产生错误。详细理由见 T13 "不做的设计"。

- **§A wait_quiesce lost-wakeup fix**: asyncio 单线程下不可触发。不做。
- **§B cancel_tree**: 引入 GraphOrchestrator → SessionTreeManager 跨层依赖。Phase 0-2 的 recover_tree + on_session_evicted 已覆盖清理。不做。
- **§C crash_count guard**: crash recovery 已是收敛的原生机制,限制重试是业务策略。不做。
- **§D wait_quiesce timeout**: 无限阻塞等待,心跳检测后续处理。不做。
- **§E Graph-level emitter configurator**: 不涉及 streaming,YAGNI。不做。
- **§F current_input 字段设置**: ReAct 用 history。只修 docstring。不做。

## Implementation order

Phase 3 and Phase 4 must ship together (or Phase 4 slightly before Phase 3). T15 (technical debt cleanup) is a prerequisite — it fixes issues that Phase 3-4 builds on.

**Implementation batches** (see MAP.md for full batch breakdown):

1. **Batch 0 — T15 technical debt cleanup** (prerequisite): fork() deletion + current_invocation removal + UndeliveredError/retry loop deletion. These are independent and can be verified separately.
2. **Batch 1 — Phase 4 infrastructure**: TurnContextDescriptor + GraphTurnArtifacts + TurnContextConfigurator ABC + 6 configurators + `build_runtime_and_context` param + resolver wiring. Testable independently (`turn_descriptor=None` short-circuits).
3. **Batch 2 — graph_instance_id propagation**: 4 injection sites + `AgentContext.graph_instance_id` field + DeliverRetryHook fix.
4. **Batch 3 — GraphOrchestrator + SessionTreeManager extensions**: `_active_contexts` + `get_graph_context` + `ModexGraphContext` + `tree_id_for_session` + `track_consume`.
5. **Batch 4 — pipeline_wiring binding**: `graph_context_resolver` post-construction setter.
6. **Batch 5 — Phase 3 execute rewrite**: thin shell + delete 70-line inline mutation + delete auto-deliver + delete isinstance assert.
