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
- "**Approval**" lives in `modex_agent/approval/` as tier definitions and classifiers, but per `modex_agent/AGENTS.md` the tiered approval is **not wired in pool mode** — `pool_builder.py` skips `RuntimeAssembler`, and subagents' `ToolNode._get_tier()` always returns `NORMAL`. The terminology is in use; the runtime coverage is partial.
