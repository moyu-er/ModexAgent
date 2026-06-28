# ModexAgent

Multi-agent framework where a main agent coordinates subagents through a star-topology pool, with multi-live workspace isolation.

## Language

**Pool**:
A group of agents coordinated through a main agent in star topology. The main agent dispatches to subagents via communication tools; subagents respond through the bus. There is exactly one pool per configuration unit (`PoolConfig`).
_Avoid_: cluster, fleet, swarm, group

**Pool Instance**:
Deployment-scoped runtime resources for one pool — providers, tool managers, skill managers, broker bridge, communication service. Not per-turn data; lives for the pool's lifetime.
_Avoid_: pool container, pool context

**Workspace**:
An isolated execution context that owns its own pools and per-pool data (memory, runtime stores, experience). Multiple workspaces can coexist (multi-live), each addressable via `/cd`.
_Avoid_: environment, sandbox, session space

**Assembly**:
The process of constructing runtime objects (pools, workspace stacks, communication infrastructure) from configuration (`AppConfig` / `PoolConfig`). Pool mode is the only assembly mode.
_Avoid_: initialization, bootstrapping, wiring (use "assembly" for the process, "wiring" for specific connections)

**Input Pipeline**:
The staged pre-processing chain a user input traverses before reaching an agent — resolving workspace, pool, channel, and session, then enqueuing an `InputMessage` for `AgentPipeline.receive()`. The bot layer composes it from framework stages (S1..S8); IM and webui use different stage subsets. It is the single entry path for everything a user sends, including (per ADR-0008) a webui approval decision, which rides as a structured `InputMessage.approval_decision` rather than a slash command.
_Avoid_: message router, ingress (use "input pipeline" for the staged chain; "input adapter" for the physical queue endpoint)

**Main Agent**:
The entry-point agent in a pool that receives user input, dispatches to subagents, and produces final output. Identified by `main_agent_name` in `PoolConfig`.
_Avoid_: primary agent, root agent, orchestrator

**ReAct Agent**:
The reasoning loop built on `Graph[R]` — a 4-node state machine (START → LLM → TOOL → END) that interleaves model calls with tool execution. The `ReActAgent` is the only shipped agent runtime; other agent types (Summarizer, ExperienceReview) are built on the same graph engine.
_Avoid_: ReAct loop, reasoning loop (when referring to the module/agent), agent loop

**Graph**:
The state-machine engine powering agent execution. `Graph[R]` holds named `Node[R]` instances and directed `Edge` instances; `GraphEngine` iterates nodes, propagates `GraphInterrupt`, and reads the result from per-turn state (`TurnCustomKey.GRAPH_RESULT`). Generic over result type `R`.
_Avoid_: state machine, workflow engine, pipeline (use "pipeline" only for `AgentPipeline`)

**GraphInterrupt**:
The exception type nodes raise to pause graph execution after persisting resumable turn state. Carries `value`, `node_name`, `iteration`. Approval suspension is the primary producer. Must propagate upward — never caught and swallowed.
_Avoid_: pause, suspend (when referring to the mechanism), checkpoint exception

**Approval**:
A human-in-the-loop gate that pauses a main agent's turn before a tool call takes effect, persisting the turn so it resumes after a human decision. One shared state machine owns suspend → prompt → decide → resume; delivery channels (IM, webui) differ only in how the prompt is rendered and the decision collected (the `ApprovalUserInterface` adapter). Off by default — opt-in per main agent via config; applies only to main agents, never subagents. Path-tiered: a listed tool's calls are auto-allowed inside the project dir, gated outside it.
_Avoid_: permission, auth, 鉴权-as-authentication (the human gate is "approval"; where 鉴权 is used in discussion it maps to approval)

**ApprovalBatch**:
The atomic unit of approval — every gated tool call in one main-agent turn. Atomic: a denial against any one request cancels the whole batch, including already-approved and approval-exempt calls. There is at most one suspended batch per session at a time. Execution (resume) happens once the batch is sealed — by a denial (short-circuit) or by every request being approved.
_Avoid_: approval group, tool batch (use "ApprovalBatch" for the approval unit; "tool batch" for the `ToolBatchState` execution grouping)

**ApprovalRequest**:
One tool call awaiting a human decision within an ApprovalBatch. It is the display and decision unit: on webui each request is one card; on IM requests are surfaced one at a time. A request's decision is one of `pending | allowed | denied | preempted`.
_Avoid_: approval item, approval entry

**AppConfig**:
The root Pydantic config object loaded from YAML, aggregating 13 typed config sections (`LLMConfig`, `AgentConfig`, `PoolConfig`, `MemoryConfig`, `ApprovalConfig`, etc.). Single entry point for full-app usage; components can be used independently by loading their individual config directly.
_Avoid_: root config, top-level config, settings

**PoolConfig**:
The config for one agent pool (one deployment of one system). Holds `LLMConfig`, a list of `AgentConfig`, optional `MCPConfig` / `MemoryConfig` / `SkillsConfig`, and `TerminalConfig`. Pool identity = name of the agent with `role="main"`. Per ADR-0001, pool mode is the only assembly mode.
_Avoid_: agent system config, fleet config, cluster config

**Active-Workspace Resources Resolver**:
The framework port the business layer implements to hand the framework the currently active workspace's per-pool resources (memory/runtime/trace stores). Canonical type: `WorkspaceManager` (a single method returning `WorkspaceResources`). Distinct from `WorkspaceResolver` (resolves a workspace by id) and `WorkspaceControlPort` (cd/switch/list control).
_Avoid_: workspace manager (the historical single-active switch-engine concept, since removed)

**Session Eviction**:
The pool dropping a subagent task session from its tracking. Two independent triggers: TTL staleness (a session inactive longer than the retention window) and LRU count cap (when a subagent exceeds `max_sessions_per_subagent`, the least-recently-used session). A session's creation time is metadata only and is never an eviction ordering key.
_Avoid_: session GC

**Session Compression**:
The act of pruning the oldest session messages (and archiving them) when the session's token weight exceeds its budget, keeping only a recent tail. The budget is measured in tokens over all non-system message roles; message count is not a budget. The kept tail is bounded by a hard token cap; any tool chain the boundary would split is evicted (archived), never partially kept. Per ADR-0009.
_Avoid_: summarization (that is the archive step), context windowing (that is the request-time governance backstop)

**Token Estimator**:
The swappable component that estimates the token weight of messages and text. A single injected instance is shared by the compression trigger and the request-time governance, so both agree on what "over budget" means. The framework ships a char-based default; the example bot supplies a tiktoken-backed estimator.
_Avoid_: tokenizer (that is the underlying encoder), counter

**Compression Trigger Ratio** (`max_token_ratio`):
The fraction of `max_tokens` at which session compression fires. Compression starts when the session's non-system token weight exceeds `max_tokens × max_token_ratio`.
_Avoid_: threshold (ambiguous — say trigger ratio or keep ratio)

**Keep Ratio** (`keep_ratio`):
The hard upper bound, as a fraction of `max_tokens`, on how much the kept region may weigh after compression. The boundary accumulates tokens from the tail until this cap; it never exceeds it.
_Avoid_: retention ratio, keep target (target implies soft; this is a hard cap)

## Relationships

- A **Workspace** owns one or more **Pool Instances**; pool instances are not shared across workspaces.
- A **Pool** is described by exactly one **PoolConfig**; multiple pools in one workspace each have their own `PoolConfig`.
- A **Pool** contains one **Main Agent** (the entry point) and zero or more subagents. Subagents are not separate pools — they share the pool's bus, broker, and tracker.
- **Assembly** turns `AppConfig` (root) into nested `PoolConfig` instances, then into `Pool` runtime objects held by a `Workspace`.
- A **ReAct Agent** runs on a **Graph**; the graph is the execution substrate, the ReAct agent is one configuration of it (4-node loop).
- A **GraphInterrupt** is raised by a `Node[R]`; the engine propagates it, the pipeline catches it for approval, and re-enters the graph after persistence.

## Flagged ambiguities

- "**control channel**" historically meant the runtime control plane in `modex_agent/control/`, but that package is largely **vestigial** — channels are constructed and threaded but have no live producers/consumers. Real cancellation is `asyncio.Task.cancel()` in `AgentPipeline`. Use "control channel" only when quoting the package; prefer "control plane" for the abstraction.
- "**pipeline**" was overloaded: it meant both the old `create_app`/`App` entry point (removed by ADR-0001) and the `AgentPipeline` orchestration layer that survives. "Pipeline" alone now means `AgentPipeline`; the old entry point is gone.
- "**Approval**" was historically **not wired in pool mode** (pool_builder skipped `RuntimeAssembler`; `ToolNode._get_tier()` always returned `NORMAL`). Now **implemented end-to-end** (framework → bot → webui; see **ADR-0008** + its Implementation outcome): path-tiered, default-off (opt-in per main agent), main-agents-only, one shared state machine (`apply_resume`) with per-channel surfacing. webui decisions flow as a structured `InputMessage.approval_decision` through the input pipeline (not slash commands); IM uses `/approve` text. Push + pull share one `ApprovalRequestView` DTO; pull (GET pending) is webui-only. Per **ADR-0011**: the batch is **atomic** (deny one = cancel all, including approved/exempt); webui **Deny All** short-circuits and seals the batch, **Approve** is per-request, IM stays one-at-a-time; webui surfacing is **pull-driven** by a dedicated suspend signal, and GET returns only genuinely-pending requests.
