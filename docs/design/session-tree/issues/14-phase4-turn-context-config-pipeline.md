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
    async def applies(self, desc: TurnContextDescriptor) -> bool: ...
    @abstractmethod
    def configure(self, ctx: AgentContext, desc: TurnContextDescriptor) -> None: ...

class TurnContextConfigPipeline:
    def __init__(self, configurators: list[TurnContextConfigurator]) -> None:
        self._configurators = configurators

    async def configure(self, ctx: AgentContext, desc: TurnContextDescriptor | None) -> None:
        if desc is None:
            return  # short-circuit for callers that omit descriptor
        for c in self._configurators:
            if await c.applies(desc):
                c.configure(ctx, desc)
```

`build_runtime_and_context` gains `turn_descriptor: TurnContextDescriptor | None = None` parameter. At the END of the method (after emitter selection, before return):
```python
if turn_descriptor is not None and self._config_pipeline is not None:
    await self._config_pipeline.configure(agent_context, turn_descriptor)
```

When `turn_descriptor=None` (all existing non-graph callers) → short-circuit → behavior identical to today.

**Configurator ordering constraint**: `GraphContextBindingConfigurator` MUST run first — it sets `agent_context.graph_context`, which other configurators read to determine applicability and to install graph-specific tools/keys. Registration order in the pipeline list enforces this: ContextBinding at index 0.

**Hook point validation** (confirmed via code exploration of `build_runtime_and_context` lines 398-548):

Configurators run at the END of `build_runtime_and_context`, after ALL construction is complete. At this point, `agent_context` has: `system_prompt`, `history`, `tool_manager` (pool-level shared, unwrapped), `session`, `comm_kind`, `max_iterations`, `system_prompt_pipeline`, `identity`, `workspace_snapshot`, `workspace`, `runtime` (with services + state + custom keys). Configurators see a fully-built AgentContext — same state that `BotAgentNode.execute` sees today when it does its inline mutation.

### 4. Configurator matrix (6 configurators)

ADR-0039 §7 matrix retained unchanged. Ordering enforced by registration position in pipeline list:

| Order | Configurator | applies() | Graph node (MAIN, is_node_execution) | Graph subagent (SUBAGENT) | Normal session |
|---|---|---|---|---|---|
| 0 | `GraphContextBindingConfigurator` | `graph_context is not None` | ✅ set `ctx.graph_context` | ✅ set | ❌ skip |
| 1 | `GraphApprovalConfigurator` | `graph_context is not None` | ✅ `approval=None` | ✅ `approval=None` | ❌ skip |
| 2 | `GraphMaxTurnsConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ `MAX_TURNS=3` | ❌ skip | ❌ skip |
| 3 | `GraphToolConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ deliver+knowledge tool | ❌ skip | ❌ skip |
| 4 | `GraphTopologyConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ topology+node_desc | ❌ skip | ❌ skip |
| 5 | `GraphKnowledgeConfigurator` | `is_node_execution and agent_kind == MAIN` | ✅ knowledge keys | ❌ skip | ❌ skip |

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

**`emitter`** — **open item, not yet decided**: `build_runtime_and_context` selects emitter at lines 538-546 but returns it as a **separate tuple element** — `agent_context.emitter` stays `None` in the ReAct path. Emitter selection does NOT depend on `graph_context`. Current `BotAgentNode.execute` does NOT override emitter (uses build default).

Analysis: if graph nodes use the same pool-level emitter as normal turns (current behavior), no configurator is needed. If graph-specific emitter is needed in the future, two options exist: (a) configurator sets `agent_context.emitter` and call site converges to read from there (external path already does this at external/turn_runner.py:199); (b) configurator replaces the tuple element. **Recommendation**: no configurator for emitter in Phase 4 (match current behavior). Revisit if graph-specific emitter becomes necessary. **Pending: confirm whether graph nodes need a different emitter than normal turns.**

**`current_input` stale docstring** — **open item, not yet decided**: `core/agent.py:119-125` documents `current_input` as "set by `build_runtime_and_context`", but grep confirms it is NOT set there (stays `None`). No configurator currently reads it.

Analysis: either fix the docstring (remove the false claim) or set `current_input` in build. Non-blocking for Phase 4 design. **Recommendation**: fix the docstring as a separate cleanup. **Pending: decide docstring fix vs. actually setting the field.**

### 5. _process_locked_inner — single descriptor construction site

After Phase 3 rewrite, ALL ReAct turns (graph node / graph subagent / normal main / normal subagent) go through `_process_locked_inner` (turn_runner.py:424). This is the single descriptor construction point.

**External agent turns**: `ExternalTurnRunner` (external/turn_runner.py:76) has its own `process_locked` (L135) that does NOT call `_process_locked_inner` or `build_runtime_and_context`. The configurator pipeline does NOT apply to external turns — this is correct: external agents don't have ReAct graph components (GraphWorkflowProvider, KnowledgeHook, DeliverRetryHook, GraphDeliverTool). External agent graph integration is via `_LightGraphContext` (§6.3) — `ExternalTurnRunner` reads `graph_instance_id` from `input_metadata` and sets `agent_context.graph_context = _LightGraphContext(gid)`. This lets `SubagentAutoSendHook` uniformly read `ctx.graph_context.graph_instance_id` (§6.4) for both ReAct and External subagents.

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
┌─ Sender (has graph_context) ─────────────────────────────────────┐
│  ctx.graph_context.graph_instance_id → envelope.metadata         │
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
│   → resolver(gid)          │  │   → ctx.graph_context =           │
│   → ModexGraphContext      │  │     _LightGraphContext(gid)       │
│   → configurators install  │  │   (no resolver, no three-comp)    │
│   → three components active│  │   → SubagentAutoSendHook reads    │
│                            │  │     ctx.graph_context.gid         │
│                            │  │     → writes AGENT_RESULT.metadata│
└────────────────────────────┘  └──────────────────────────────────┘
```

#### 6.2 Injection points (4 sites)

| # | Site | File:Line | Covers | Logic | Lines |
|---|------|-----------|--------|-------|-------|
| 1 | `SendStrategy.execute` (post-build_envelope) | base.py:77-93 | SubagentDispatch + ParentReply (+ SubagentAutoSendHook if converged to SendStrategy by Phase 0-2) | `if req.context.graph_context is not None: envelope.metadata["graph_instance_id"] = req.context.graph_context.graph_instance_id` | +3 |
| 2 | `SubagentAutoSendHook._notify_parent` | subagent_auto_send.py:463 | AGENT_RESULT (subagent→parent auto-reply) | `if ctx.graph_context is not None: envelope.metadata["graph_instance_id"] = ctx.graph_context.graph_instance_id` | +3 (**may be absorbed by Site 1 if Phase 0-2 converges SubagentAutoSendHook to SendStrategy**) |
| 3 | `ExternalTurnRunner.process_locked` | external/turn_runner.py:166-178 | External agent receives graph_instance_id | `gid = input_metadata.get("graph_instance_id"); if gid is not None: agent_context.graph_context = _LightGraphContext(gid)` | +5 |
| 4 | `modexctl SendRequest` + `facade.send` | models.py:138 / facade.py:369 | CLI send carries graph_instance_id | SendRequest adds `graph_instance_id: int \| None = None` field; facade constructs `_LightGraphContext(gid)` on AgentContext | +model field +3 |

**Note on Site 2**: SubagentAutoSendHook currently bypasses SendStrategy (constructs envelope inline, calls `tree.deliver` directly). Phase 0-2 may converge this to SendStrategy (T09/T12). If converged, Site 2 is absorbed by Site 1 (one injection covers all). If not converged, Site 2 is independent. **Implementation: check Phase 0-2 final state.**

#### 6.3 _LightGraphContext

A lightweight GraphContext subclass for External and CLI paths — carries only `graph_instance_id`, no artifacts, no state. Does NOT trigger resolver or configurators (ExternalTurnRunner doesn't go through `build_runtime_and_context`).

```python
class _LightGraphContext(GraphContext[Any]):
    """Minimal GraphContext for external/CLI paths. Only carries graph_instance_id
    for SubagentAutoSendHook to read. Does NOT activate three-components
    (ExternalTurnRunner doesn't dispatch BEFORE_TURN/AFTER_TURN hooks)."""
    
    def __init__(self, graph_instance_id: int) -> None:
        self.graph_instance_id = graph_instance_id
```

**Why not full ModexGraphContext?** External agents don't have ReAct graph components (GraphDeliverTool, GraphWorkflowProvider, KnowledgeHook, DeliverRetryHook). These are all ReAct-hook-gated or require `runtime`/`ReActTurnState` that ExternalTurnRunner doesn't build. `_LightGraphContext` provides the sole thing external needs: `graph_instance_id` for SubagentAutoSendHook to propagate back.

#### 6.4 SubagentAutoSendHook — unified read logic

Both ReAct and External subagents read `ctx.graph_context.graph_instance_id`:

```python
# In SubagentAutoSendHook._notify_parent:
gid = (
    ctx.graph_context.graph_instance_id
    if ctx.graph_context is not None
    else None
)
if gid is not None:
    envelope.metadata["graph_instance_id"] = gid
```

**One code path, no ReAct/External branch for graph_instance_id.** ReAct subagent has `graph_context` set by `GraphContextBindingConfigurator`; External subagent has `graph_context` set by `ExternalTurnRunner` (`_LightGraphContext`). Both expose `.graph_instance_id`.

#### 6.5 External agent — environment variable bridge

External agents (opencode) already have a graph_instance_id bridge via environment variables:

- `ExternalEnvSpec.task_id` = `str(graph_instance_id)` (types.py:178) → `MODEX_TASK_ID` env var
- `MODEX_WORKFLOW_ID`, `MODEX_NODE_ID` also set by `ExternalEnvBuilder` when spawned by `BotAgentNode`
- `modexctl deliver` already reads `MODEX_TASK_ID` (deliver.py:120-124)

**SSE child discovery**: opencode's internal subagent forks (via SSE `session.created` with `parentID`) inherit parent's environment variables. Child env snapshots (agent.py:733-741) set `comm_kind=SUBAGENT` + `parent_session_id`. The `MODEX_TASK_ID` env var is inherited automatically → child external subagent also has graph_instance_id.

**modexctl send convergence**: SendRequest (models.py:138) adds `graph_instance_id: int | None = None` field. When set, `facade.send` constructs `_LightGraphContext(gid)` on the AgentContext → SendStrategy.execute reads it → propagates to envelope.metadata. This converges CLI send with agent-to-agent communication.

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

**Liveness is acceptable**: In normal flow, graph finalizes AFTER `BotAgentNode.execute` returns (execute's `wait_quiesce` waits for all subagents → execute returns → node.run completes → graph advances → finalize). So subagent turns always find the graph still RUNNING. In abnormal flow (crash/timeout), resolver returns None → descriptor.graph_context=None → configurators skip → subagent runs without graph config, but tree's track fallback prevents deadlock.

### 10. ModexGraphContext._node_artifacts clearing

`ModexGraphContext` instances are owned by `_active_contexts`. `finalize` clears `_active_contexts` → ModexGraphContext dropped → `_node_artifacts` GC'd with it. No separate clearing path needed.

## Coverage — 5 turn scenarios

| Turn scenario | Path | graph_context source | configurators | graph_instance_id propagated back? |
|---|---|---|---|---|
| Normal main | InboxPoller → _process_locked_inner | None | all skip | N/A |
| Normal subagent | InboxPoller → _process_locked_inner | None | all skip | N/A |
| Graph node main (ReAct) | BotAgentNode.execute → tree.deliver → InboxPoller → _process_locked_inner | resolver → ModexGraphContext (full) | all 6 apply | ✅ via SubagentAutoSendHook / SendStrategy |
| Graph subagent (ReAct) | (parent dispatch) → InboxPoller → _process_locked_inner | resolver → ModexGraphContext (full) | ContextBinding + Approval | ✅ via SubagentAutoSendHook (Site 2) |
| Graph subagent (External) | (parent dispatch) → InboxPoller → ExternalTurnRunner.process_locked | _LightGraphContext (gid only) | none (bypasses configurator pipeline) | ✅ via SubagentAutoSendHook (unified read, §6.4) |
| Graph subagent (External, SSE child) | opencode internal fork → inherits MODEX_TASK_ID env | _LightGraphContext (gid from env) | none | ✅ via SubagentAutoSendHook + env inheritance (§6.5) |
| modexctl send | CLI → facade → SendStrategy → tree.deliver | _LightGraphContext (from SendRequest field) | depends on receiver path | ✅ via SendStrategy.execute (Site 1) |

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

## Implementation order

Phase 3 and Phase 4 must ship together (or Phase 4 slightly before Phase 3):
- Phase 4 without Phase 3: configurators ready but execute still does inline mutation → configurators never run (no `turn_descriptor` passed) → no harm but no benefit.
- Phase 3 without Phase 4: execute uses tree.deliver but InboxPoller-driven turn has no graph config → deliver tool missing, graph_context missing → broken.

**Recommended**: implement Phase 4 configurators + `build_runtime_and_context` param + `_process_locked_inner` descriptor construction first (can be tested with normal turns — `turn_descriptor=None` short-circuits). Then implement Phase 3 execute rewrite (flips the switch — graph node turns start passing descriptors).
