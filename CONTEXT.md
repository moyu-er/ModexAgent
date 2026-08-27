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
The reasoning loop that interleaves model calls with tool execution — a 4-node graph (START → LLM → TOOL → END) configured on top of the **Graph Engine**. The `ReActAgent` is one configuration of the graph engine, not the only agent runtime. Other agents that need tool use (`ArchiveSummarizer`, `KnowledgeConsolidator`, `ExperienceReviewAgent`) inherit `ScopedFileAgent` and internally build a `ReActAgent` in clean mode; `ExternalAgent` does not use the graph engine at all.
_Avoid_: ReAct loop, reasoning loop (when referring to the module/agent), agent loop

**Graph Engine**:
The standalone, framework-agnostic graph + scheduling engine that powers graph-shaped agent execution. Lives in `src/modex_graph/` as a **sibling package of `modex_agent`** — `modex_agent` depends on `modex_graph`, the reverse is forbidden and enforced by architecture guard test. Depends only on Pydantic + standard library. Provides `Graph[S]` and `CompiledGraph[S]` topologies, `GraphEngine` with linear and parallel schedulers, `GraphContext[S]` with shared state and runtime services, async `Node[S]` execution, `GraphState`, `GraphRuntime`, and the `GraphInterrupt` + `GraphBubbleUp` exception family. Per ADR-0033.
_Avoid_: state machine, workflow engine, pipeline (use "pipeline" only for `AgentPipeline`), graph (use "Graph Engine" for the package; "Graph" informally for a configured topology)

**Node**:
The unit of execution in the Graph Engine. `Node[S]` is an ABC whose `async execute(ctx: GraphContext[S], integrated_input: IntegratedInput) -> None` method reads and mutates shared `ctx.state`, optionally emits via `ctx.emit(...)`, optionally interrupts via `ctx.interrupt(...)`, and calls `deliver(content, next_node, ctx)` to select downstream work. After execution, `submit(ctx)` groups delivers and routes them through `ctx.dispatch(...)`. A `Graph` can itself be a `Node` (Graph-is-a-Node, Flow-is-a-Node) enabling nested subgraphs.
_Avoid_: step (too generic), task (collides with multi-agent task), stage (collides with input pipeline stages)

**GraphRuntime**:
The framework-defined ABC that lets a `Node` invoke AOP concerns (hooks, snapshot, control drain, governance, approval suspension) without the Graph Engine knowing their concrete types. Lives in the Graph Engine package as an ABC with no-op default methods. `modex_agent` provides `ReactGraphRuntime` (and similar) implementations that bridge to `HookRunner` / `SnapshotPolicy` / `ControlChannel` / `ApprovalRuntime`. A standalone graph user supplies `GraphRuntime()` (no-op defaults) or their own subclass. The engine auto-invokes `before_node(ctx, node_name)` and `after_node(ctx, node_name)`; iteration-level hooks (`BEFORE_ITERATION` / `AFTER_ITERATION`) are dispatched explicitly by ReAct nodes via `ctx.runtime.dispatch_hook(...)`, NOT engine-auto-invoked, because "iteration" is a ReAct concept, not a universal graph concept. The `around` method routes `ITERATION` scope only — `TOOL_CALL` and `LLM_STREAM` are node-local AOP invoked directly via `InterceptorChain` (`around_tool_call` / `around_llm_stream`) because their typed contexts are not constructible from `GraphContext` (ADR-0033 D5).
_Avoid_: runtime services (that is `AgentRuntimeServices`), runtime context (too generic), graph services

**GraphBubbleUp**:
The control-flow-via-exception family for the Graph Engine. Base class `GraphBubbleUp(Exception)`; concrete subclasses `GraphInterrupt` (HITL suspend, raised by `ctx.interrupt(value)`), `GraphDrained` (cooperative shutdown — class exists, wiring deferred), and `ParentCommand` (subgraph→parent routing — class exists, wiring deferred). The engine never swallows these; they propagate to the caller (typically `TurnRunner.process_locked`).
_Avoid_: graph exception (too generic), control exception (collides with `modex_agent/control/`)

**GraphInterrupt**:
The exception type nodes raise to pause graph execution after persisting resumable turn state. Carries `value` and `node_name`. Approval suspension is the primary producer. Must propagate upward — never caught and swallowed.
_Avoid_: pause, suspend (when referring to the mechanism), checkpoint exception

**Trigger Mode**:
The per-node policy deciding when a node with multiple incoming edges becomes eligible to execute. Two values: `ON_ALL_PREDS` — the node fires once, after all *activated* predecessors have completed (the stable, recommended mode); `ON_RECEIVE` — the node fires once *per* activated predecessor that completes (**deprecated / experimental**: `Graph.compile()` emits a `DeprecationWarning`, `GraphSpec` rejects it; the per-node FIFO serial gate is in-memory only and not persisted across crashes). "Activated" means the predecessor was actually selected by an upstream routing decision — a predecessor that is never routed to does not count toward the node's readiness, so conditional branches that skip one arm do not deadlock the join. Each graph has a default Trigger Mode (recommended `ON_ALL_PREDS`); individual nodes may override it.
_Avoid_: join policy, fan-in mode, activation strategy

**Activated Predecessor**:
A predecessor node that was actually selected by an upstream deliver and will therefore execute in the current graph run. Contrast with a *declared* predecessor — a node connected by an edge that may or may not receive a deliver. Trigger Mode readiness is computed against activated predecessors only, never against all declared predecessors, so that a conditional branch skipping one arm does not leave a downstream join waiting forever for a predecessor that will never fire.
_Avoid_: reachable predecessor (reachability is a graph-property term; activation is a runtime-routing decision)

**Superstep**:
One execution round of a BSP (Bulk Synchronous Parallel) graph scheduler: all nodes that are *ready* (their Trigger Mode is satisfied) execute concurrently, then their results are merged at a barrier, then the next superstep computes its ready set from the updated state. A graph run is a sequence of supersteps. **The `ParallelScheduler` in modex_graph does NOT use supersteps** — it uses continuous scheduling (instances start the moment dependencies are satisfied, no barrier). The term is retained for contrast with langgraph's BSP model. See ADR-0034 D2.
_Avoid_: iteration (that is a ReAct concept — one LLM+TOOL cycle; a superstep is a graph-scheduler concept), batch, wave

**Continuous Scheduling**:
The `ParallelScheduler` execution model (ADR-0034 D2): instances start as independent `asyncio.Task`s the moment their dependencies are satisfied — no batch barrier (`asyncio.gather`). A short task (10ms) completing does not wait for a long task (10s) in the same batch. All tasks share the same `GraphContext` (no per-task shells — `copy(ctx)` was removed); state isolation is via per-node `node_scratch` keys, and invocation identity (instance ID, invocation context) is task-local via a `ContextVar` execution context. Contrast with BSP supersteps (langgraph's model), where a barrier waits for all tasks in a step to complete before merging.
_Avoid_: event-driven scheduling (too generic), task-parallel (overloaded with distributed computing sense)

**Ready**:
The state of a node instance whose Trigger Mode is satisfied: for `ON_ALL_PREDS`, all activated predecessors have dispatched AND reachability is clear (no active instance can reach this node via outgoing edges); for `ON_RECEIVE`, a dispatch has been received (no reachability check). A READY instance is launched as an `asyncio.Task` under continuous scheduling.
_Avoid_: runnable, available (both too generic)

**Scheduler**:
The pluggable scheduling strategy behind `GraphEngine`. An ABC with two implementations: `LinearScheduler` (single-node pointer and sequential execution; the default) and `ParallelScheduler` (continuous multi-instance execution, trigger modes, and reachability-based readiness for ON_ALL_PREDS; opt-in via `Graph.compile(scheduler="parallel")`). Both share the same `CompiledGraph`, async `Node` contract, deliver/submit routing path, shared graph state, and `GraphRuntime` AOP bridge. See ADR-0034.
_Avoid_: executor (too generic), runner (collides with `TurnRunner`), engine (that is `GraphEngine` — the entry point that delegates to a Scheduler)

**Node Instance**:
One execution of a node under `ParallelScheduler`. Identified by `{node_name}#{global_seq}` (e.g. `body#0`, `body#1`). Each instance has an independent lifecycle (DORMANT → READY → RUNNING → COMPLETED) and runs as an `asyncio.Task` sharing the graph's `GraphContext`, with invocation identity carried by a task-local `ContextVar` execution context. Loops produce new instances rather than resetting state. Re-activation by a loop edge creates a fresh instance with a new sequence number. A per-node serial gate guarantees the same node never has two concurrently executing instances, under any Trigger Mode.
_Avoid_: node execution (too generic — that is the act, not the identity), node state (collides with `GraphState`)

**Dispatch**:
The framework-level primitive for routing a delivered payload from one node to another. During `execute`, a node calls `deliver(content, next_node, ctx)` to accumulate downstream work. After `execute` returns, `submit(ctx)` groups those delivers and calls `ctx.dispatch(target, state_update={"delivered": payload, ...})`; the registered scheduler handler selects or activates the target and routes the payload through the coordinator. See ADR-0034 D5.
_Avoid_: send (collides with multi-agent `send_to_agent`), route (that is the computation of dispatch targets from transition/edges), deliver (too generic)

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
The fraction of `max_context_tokens` at which session compression fires. Compression starts when the session's non-system token weight exceeds `max_context_tokens × max_token_ratio`.
_Avoid_: threshold (ambiguous — say trigger ratio or keep ratio)

**Keep Ratio** (`keep_ratio`):
The hard upper bound, as a fraction of `max_context_tokens`, on how much the kept region may weigh after compression. The boundary accumulates tokens from the tail until this cap; it never exceeds it.
_Avoid_: retention ratio, keep target (target implies soft; this is a hard cap)

**CommandTool** (`bash` tool, `modex_agent.tools.terminal.command_tool.CommandTool`):
The agent-facing command execution tool for persistent PTY sessions. Orchestrates `Session.primitives` + `poll_until_settled` + `Session.apply_outcome(result)` per ADR-0010 Decision 7 — Session owns state, the Tool owns orchestration. Returns XML `<command_result>` with status (completed / executing / timed_out / paginated / waiting_input / stuck). Falls back to `SubprocessTool` when terminal backends are unavailable. _Avoid_: ShellTool, bash (informal)

**ProcessTool** (`process` tool, `modex_agent.tools.terminal.process_tool.ProcessTool`):
The agent-facing tool for interacting with a running process inside a persistent PTY session (write / submit / send_keys / paste / interrupt / kill). Same orchestration pattern as CommandTool: `Session.primitives` + `poll_until_settled` + `Session.apply_outcome(result)`. Never used by subagents. _Avoid_: terminal (too generic)

**Shell Family** (`ShellFamily`):
The behavioural class of a shell — bash / zsh / sh / cmd / powershell — embodied by `ShellInfo(family, path, platform)`. ADR-0010 elevates Shell Family to one of two **upstream-visible design axes** for the terminal system (the other is Terminal Visibility). OS is NOT a design axis: it is an implementation fork collapsed entirely into the `TerminalBackend` subclasses. _Avoid_: shell type, terminal type (too generic)

**Terminal Visibility** (`TerminalVisibility` enum, `VISIBLE` / `HIDDEN`):
Whether a human can observe or intervene in the terminal session's window. ADR-0010 (partially superseded by ADR-0032 — see disposition in `docs/adr/0010-...md`; the structural-vs-switch diagnostic stands) splits visibility expressions by mechanism:
- *Structural* visibility difference (different I/O architecture) → subclass split (`WinptyHiddenBackend` vs `WinptyConsoleWindowBackend`: in-process winpty vs external host process + IPC bridge). Per ADR-0032 D2 the IPC bridge is `asyncio.open_connection` / `asyncio.start_server` (was: raw `socket.socket`); the structural split itself is unchanged.
- *Switch* visibility difference (one `new_session(attach=…)` flag) → single class with `visibility=` parameter (`TmuxBackend`).
Unsupported (transport, visibility) combinations are rejected at the factory (`UnsupportedVisibilityForTransport`) rather than silently falling back. _Avoid_: visible mode, window mode (too vague)

**TerminalTool** (`terminal` tool, `modex_agent.tools.terminal.tool.TerminalTool`):
The agent-facing tab-management tool (open / close / list / select / history / interrupt). It is the *only* consumer of `TerminalSession.detect_interference` (which reads the `_expected_state` slot set by `set_expected_state(...)`); per ADR-0010 Decision 7, this interference-detection slot is orthogonal to the three slots (`_busy_after_timeout` / `_last_status` / `_command_started_at`) owned by `apply_outcome(result)`, and is therefore preserved when apply_outcome is introduced. _Avoid_: tab manager (informal)

**Async-Safety Contract** (ADR-0032 D1):
The structural rule that every `TerminalBackend` I/O method (`write`, `read_pending`) must be genuinely non-blocking when awaited. Realised by a template-method pattern in `TerminalBackend`: `write` / `read_pending` are concrete and delegate to opt-in hooks `_write_blocking` / `_read_blocking` (default `raise NotImplementedError`), wrapping them in `loop.run_in_executor(None, …)`. Backends with synchronous PTY libraries (winpty-hidden, pexpect) implement the hooks and inherit the template. Backends with native async I/O (visible-windows, post-ADR-0032 D2 — uses `asyncio.StreamWriter` / `StreamReader`) or with a different I/O model (tmux — control-protocol snapshots) override `write` / `read_pending` directly and do not implement the hooks. The hooks are opt-in (not abstract) so neither escape-hatch backend is forced to provide no-op stubs. Enforced by `tests/architecture/test_terminal_async_safety.py`. _Avoid_: async wrapper (too generic), executor pattern (too generic)

**Blocking-IO Hook** (`_write_blocking` / `_read_blocking`):
The opt-in hooks on `TerminalBackend` that a synchronous-PTY backend implements to satisfy the **Async-Safety Contract**. The base-class `write` / `read_pending` template methods wrap them in `loop.run_in_executor`. A backend that overrides `write` / `read_pending` natively (visible-windows after ADR-0032 D2, tmux) does NOT implement these hooks — they remain at their default `raise NotImplementedError`. _Avoid_: blocking backend (confuses transport with hook)

**Shell Family Hook** (`_shell_family() -> ShellFamily`):
The abstract method on `TerminalBackend` that returns the shell family of the running shell. Concrete backends implement it, typically as `return _family_from_path(self._shell or "")`. The base class consumes it in `clear_input_line()` (writes `\x01\x0b` iff `_shell_family().uses_readline()`) and `drain_startup()` (passes `uses_readline=` to the shared `drain_windows_startup` helper). Replaces the three divergent shell-detection heuristics that existed pre-ADR-0032 (substring `"bash" in self._shell`, `_family_from_path`, inline suffix tuple). _Avoid_: shell detector, shell probe

**Snapshot Backend**:
A `TerminalBackend` whose I/O model is a control-protocol snapshot rather than a byte stream. Only `TmuxPtyBackend` today: `send_keys` writes; `capture_pane` reads a pane snapshot. A snapshot backend overrides `write`, `read_pending`, `current_segment`, and `drain_startup` directly (per ADR-0032 D1 point 3) and does not participate in the Blocking-IO Hook pattern. The byte-stream backends (visible-windows, hidden-windows, pexpect) write bytes to a PTY and accumulate them in a `SlidingOutputBuffer`; the snapshot backend accumulates pane snapshots in `_last_capture` and derives new output via prefix-match diff over `capture-pane -p -S -` (full scrollback, ADR-0032 D5). Not a ubiquitous-language term — implementation shape, not domain concept.

**Attachment**:
A file bound to a message in a conversation, identified by an opaque id, rendered direction-agnostically but stored asymmetrically. The system's purpose is conversation-level file awareness + tool-based inspection by the agent, plus symmetric IM/WebUI download — not file transfer in isolation. `kind` (image / extractable-document / other) is classified once at ingest from magic-byte MIME. Two inspection mechanisms coexist: mechanism B (tool-based — the agent sees only a path reference and a tool's text result; works with any model; the v1 path) and mechanism A (native multimodal — file bytes inline as a model content block, gated on `ModelCapabilities`; implemented per ADR-0014). The injected agent reference carries `name + mime + size + absolute_path` (no download id).
_Avoid_: upload, media file, blob (use "Attachment" for the bound-to-message concept)

**MediaStore**:
The framework ABC that persists **inbound** attachment bytes, swappable to object storage (S3-class) later behind one ABC, with a `LocalFileMediaStore` now. Routed per-(workspace,pool), mirroring `WorkspaceScopedTranscriptStore.store_for` — a service-singleton with unified workspace+pool routing, not a parallel mechanism. `save`/`read` are stream/path-oriented (never buffer the largest configured file into memory). Only inbound bytes flow through it; **outbound** reads the workspace filesystem directly, in place, uncopied. Enforces the storage gate (single-file cap, per-session bytes budget with oldest-by-mtime deletion, executable deny-list).
_Avoid_: media service, file store (use "MediaStore" for the framework ABC and its inbound persistence contract)

**ModelCapabilities**:
A frozen value object on `LLMConfig` exposing the modalities a provider/model can natively consume — a set of `Modality` enum values (`TEXT` always present; `IMAGE` / `VIDEO` / `AUDIO` default-off, extensible). It is the switch the dormant provider-side renderer binds to: when a modality is on, matching attachments that pass the **model-facing gate** (type allow-list + strict inline size — image ≤ 20 MB, text/doc ≤ 10 MB) render into model content blocks; on model rejection they strip back to path placeholders. Fully implemented per ADR-0014 — `supports()` is actively consulted by the tool manager and ReAct LLM node; `enrich_inline_media` injects matching attachments as model content blocks. Independent of the storage gate.
_Avoid_: vision config, modality flag (use "ModelCapabilities" for the provider/model capability set)

**Attachment Locator**:
The `locator` field on an Attachment (`media` | `workspace`) — the single internal switch selecting the storage backend and download read path (`media` → `MediaStore.read`, `workspace` → filesystem read at the literal stored path). It is an internal read-dispatch detail, invisible to the frontend. It also selects path semantics: `media` paths are relative to the workspace root (a location we control); `workspace` paths are the literal absolute path the agent provided. The download + degradation contract is symmetric across both (file present → serve, file gone → fallback icon).
_Avoid_: storage type, source field (use "Attachment Locator" for the media/workspace switch)

**Agent Inbox** (or **Inbox**):
The per-session, per-pool queue that holds all turn-starting inputs between enqueue and consumption. It is the **single level source of truth** for a message's existence: a message is either pending in the inbox, folded into a running turn, or consumed — never "spawned into its own turn". Keyed by the receiver's `session_id` (unique within a pool). Holds both inter-agent messages (`message_type` = `task_request` / `subagent_result` / `agent_message`) and human/external inputs (`message_type` = `external_input`). Storage + dedup semantics (FIFO, exactly-once) are defined on the `InboxMQ` ABC (`InboxServer` is a deprecated alias); the inbox layer is transport, not orchestration. Each pool owns its own `InboxMQ` (file backend under `<workspace_data>/inbox/<pool_name>/`, SQLite backend in the workspace `state.db`). See ADR-0015 (revised).
_Avoid_: mailbox, queue (too generic), channel (overloaded)

**InboxPoller**:
The sole between-turn driver — one long-lived poller per pool, ticking every ~200ms. Each tick it enumerates sessions with pending inbox input and starts a drain task for each idle one; that task consumes a batch and dispatches **one agent turn per envelope** (`dispatch_envelope`). **Single-flight** is enforced by an `inflight: dict[session_id, asyncio.Task]` table: the poller sets the entry synchronously before scheduling the task and the task pops it in a `finally`; `reconcile_inflight()` every tick evicts any done-but-leaked entry. A session that already has a live `inflight` task is skipped (fold-in handles mid-turn). The poller also lazy-**materialize**s a subagent instance when it finds an idle+pending session with no live instance. Replaces ADR-0015's per-session Drainer / `SessionInputQueue` / `_session_gates` (see the poll-driven redesign). There is **no per-session execution lock** — mutual exclusion within a session is structural (one `inflight` task).
_Avoid_: Drainer, consumer loop, dispatch task (these name the older ADR-0015 mechanism the poller replaced)

**Fold-in**:
The mid-turn consumption path: a turn already running drains its own inbox on each iteration (`InboxFlushHook.before_iteration`) and injects new inter-agent messages into the current turn's history as `role=AGENT`, rather than starting a follow-up turn. It consumes with `only_types = {task_request, subagent_result, agent_message}` — it does **not** consume `external_input`, so a human DM always starts a fresh turn. It is the *only* mid-turn consumption path; between turns the InboxPoller's turn runner consumes all types. See ADR-0015 (revised).

**Materialize** (a subagent):
Building a subagent's agent instance lazily, on the first turn of its session, rather than when a message is sent to it. `send` mints/resolves the session id and enqueues without any instance existing; the poller's `_materialize_then_turn` builds the instance from its template on that first turn. "Cold start" is thus "materialize on first turn" — one path, not a per-direction special case. Normals are NOT materialized — they are eager-registered at boot by business wiring (Design B). See ADR-0015 (revised).

**Target resolution** (`_resolve_target`):
The send-side step that decides what kind of receiver a name is. `AgentCommunicationService._resolve_target(name)` returns `(comm_kind, template)`: `template` set when the name matches a subagent template (cold-start); otherwise the pool registry's `comm_kind` for an already-live agent. The star-topology policy (a subagent sender may target only its own parent; subagent→subagent is forbidden) is enforced inline in `_send`, not by a separate target object. (ADR-0015 D4 sketched an `AgentTarget` class hierarchy; it was never implemented — the seam is `_resolve_target` + `_send`'s policy checks.) As of ADR-0019 the seam shifted: the `CommunicationTargetStore` is the single routing source of truth — the store entry carries `pool_name` and `bus_ref`, and `_send` dispatches by store-lookup, not by a separate resolver.
_Avoid_: AgentTarget (the unbuilt ADR-0015 abstraction), target descriptor, recipient (too generic)

**Communication Target**:
A typed entry in a pool's `CommunicationTargetStore` describing one reachable agent. Fields: `name` (agent name, unique within the store across all reachable pools — duplicate `add` is rejected at registration), `kind` (`AgentCommKind`), `pool_name` (owning pool — local pool or a configured peer pool), `bus_ref` (optional direct reference to the target pool's `AgentMessageBus`; `None` means local — route to the pool's own bus), `description`. The store is the single source of truth for communication tool routing: `task`/`send_to_peer`/`send_to_agent` each look up targets by name via `list_subagents()`/`list_peers()`/`list()`. When `bus_ref` is set, `PeerNormalStrategy` delivers directly to the peer pool's bus — no framework knowledge of "peer pool" topology, no new transport, no broker involvement. Defined in ADR-0019.
_Avoid_: contact, recipient, peer descriptor

**Peer Pool**:
A pool explicitly configured to exchange messages with another pool, at the business layer. Peer communication is **root-to-root** — the two trees' main agents are equals (the receiving peer session is a root session, `parent_session_id=null`); a relationship parallel to parent-child dispatch, not a flattening into generic node links. Peer configuration is **bidirectional by invariant** (declaring B as a peer of A requires declaring A as a peer of B, enforced at registration) and same-workspace in v1. The framework itself has no "peer pool" concept — it only sees `CommunicationTarget` entries whose `bus_ref` points at another pool's bus. The business layer's assembly discovers configured peers, acquires bus references, and populates each pool's `CommunicationTargetStore` with peer main-agent entries. Defined in ADR-0019; tree framing per ADR-0042.
_Avoid_: linked pool, federated pool, neighbour pool

**Session Group**:
The implicit set of all sessions, across peer pools, that share a session-id prefix as the result of peer communication. When agent A (session `convA.mainA`) sends to peer agent C, C's receiving session is `convA.mainC` — same prefix. C replying routes to `convA.mainA`. Communication context therefore propagates across the session group as a property of the shared prefix: agents see each other's contributions as if multiple people were in one room. This is a **design semantic, not a defect** — peer-pool v1 deliberately adopts the session-group model over pair-isolated sessions (which would lose bidirectional continuity). See ADR-0019 for the trade-off analysis and the deferred "context fork for peer sessions" item that would let an agent isolate per-peer context if needed.
_Avoid_: conversation cluster, fan-out session (those imply a different topology)

**Reasoning Effort**:
A per-model enum (`ReasoningEffort`) controlling how much internal reasoning a model performs. Allowed values: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`. Persisted in `config/model.yml` per model. Both `ReasoningEffort.NONE` and an absent field mean the provider does **not** send a `reasoning_effort` parameter, preserving existing behavior. Any other value is passed through to the LLM API. The user is responsible for selecting a value compatible with the model. See ADR-0021.
_Avoid_: reasoning mode, thinking level

**Reasoning Content**:
The thinking chain produced by a reasoning-capable model, surfaced to the frontend via `reasoning_content` events and rendered as a reasoning block. It is intentionally not persisted to memory: `ChatMessage.to_dict()` strips it before storage. The framework always shows reasoning content when it is present; there is no `show_reasoning` toggle.
_Avoid_: thinking content, thought chain, reasoning chain (use "reasoning content" when referring to the persisted/streamed artifact)

**Core Memory** (formerly *Knowledge layer*):
The agent-managed in-context memory layer holding `SOUL.md` (agent identity / behavioural guidelines), `USER.md` (user profile / preferences), and `MEMORY.md` (distilled facts). Always injected into the system prompt at priority 100, persisted as markdown files per scope, edited by the agent through scoped file tools. Aligned with Letta's "Core Memory" concept (memory blocks `persona` + `human`) and mem0's "User Memory / Semantic Memory". Renamed from "knowledge" to disambiguate from **KnowledgeBase** (RAG sense) and to match industry terminology. The XML tag values (`<your_identity>`, `<user_profile>`, `<known_facts>`) and the markdown file names (`SOUL.md` / `USER.md` / `MEMORY.md`) are **not** renamed — they are agent-facing prompt artifacts and user workspace data.
_Avoid_: knowledge (overloaded — see KnowledgeBase), persona layer (persona is a block label inside Core Memory, not the layer itself), profile layer (too narrow — Core Memory also holds agent identity and distilled facts), core layer (ambiguous — there is also a `memory/core/` directory that holds the subsystem's ABCs)

**KnowledgeBase** (forthcoming):
A retrievable collection of static, shared domain knowledge consulted by an agent on demand — RAG document corpus, FAQ, structured reference data, knowledge graph, or any other indexed content. Distinct from **Core Memory**: Core Memory is in-context, agent-managed, and per-user/per-agent; KnowledgeBase is out-of-context, retrieved via search/tool/injection, and typically shared across agents or scoped to a pool/workspace. Aligned with LangChain / LlamaIndex / mem0's RAG-vs-Memory distinction ("RAG answers 'what does this document say?'; Memory answers 'what does this user need?'"). The framework ABC will be minimal — only `search()` is mandatory; CRUD and maintenance operations are optional mixins or business-layer concerns.
_Avoid_: archival memory (Letta-specific synonym for a particular implementation), document store (an implementation detail, not the abstraction), corpus (too academic for English-as-Second-Language readers)

**RecordScope**:
A frozen Pydantic **base** model carrying framework-level dimensional fields (workspace_id, session_id, session_prefix, agent_id, agent_role, user_id, tenant_id, channel, chat_id, invocation_id, parent_session_id). Business layers subclass it to add business dimensions (e.g. the bot project's `BotRecordScope` adds `pool`). These field names are canonical across Python and SQL generated-column extraction (`agent_id`, never `agent` or `agent_name`). Its canonical JSON is the sole source for a DB store's generated dimensions; ordinary domain keys and payload columns remain explicit. `canonical()` produces a deterministic JSON string (recursive key sorting) for uniqueness and comparison — **a base `RecordScope` and a subclass instance with extra fields produce different canonical JSON, therefore different `scope_key` values; mixing them in the same table partitions records into separate storage buckets by construction** (this is intentional: framework-managed records vs business-scoped records are naturally isolated). `to_path_segment(*dimensions)` derives file-path segments for file-backed stores. Replaces `CompositeScope` string-join for DB-backed stores; `CompositeScope` remains for file-backed stores.
_Avoid_: scope key (that is the string output), scope object (too generic), business scope (use the specific subclass name, e.g. BotRecordScope)

**Canonical JSON** (`modex_agent.utils.canonical_json`):
The recursive deterministic serializer used by `RecordScope.canonical()` and all DB payload columns requiring stable comparison. Dict keys sorted at every nesting level; sets sorted and converted to lists; lists preserve element order with recursive canonicalization; non-finite floats are rejected. Same semantic data always produces the same byte sequence.
_Avoid_: sorted JSON (imprecise — it is recursive, not just top-level sort)

**State DB** (`<workspace>/.modex/state.db`):
The per-workspace SQLite database holding transactional structured state: inbox messages, turn snapshots, session index, pool routing, todos, memory session/KV/cursors, archive metadata, external session map, approval audit log. WAL mode, `foreign_keys=ON`, `busy_timeout=5000` on every connection. One writer at a time per workspace; different workspaces write concurrently.
_Avoid_: workspace database (ambiguous — could mean the registry DB)

**Registry DB** (`<home>/.modex/_registry/state.db`):
The global SQLite database holding cross-workspace routing only: workspace registry (workspace_id, target_path, display_name, last_active) and session→workspace map. Small (tens to hundreds of rows). Does not participate in high-frequency writes.
_Avoid_: global database (too generic)

**Generated Scope Column**:
A `STORED` generated column derived from the `scope_key` JSON column via `json_extract(scope_key, '$.dimension')`. Real column — supports true B-tree composite indexes on any dimension combination. Application code writes each dimension only through `scope_key` (the canonical JSON of a `RecordScope` subclass); generated columns are derived by the database engine. New dimensions added via `ALTER TABLE ADD COLUMN ... GENERATED ALWAYS AS ...` with no dimension write-path change. **Only dimensions that have a real query path should get a generated column** — speculative `pool`-dimension columns and their indexes were removed because no adapter ever filtered on `pool` (the pool concept is business-layer routing via `PoolRoutingStore`, not a storage dimension).
_Avoid_: functional index (that is a different mechanism — single-dimension, not composite B-tree)

**Epoch Millisecond Timestamp**:
The single canonical timestamp representation across all DB columns and file-backend JSON payloads: a non-negative `INTEGER`/`BIGINT` counting UTC milliseconds since the Unix epoch. Replaces the previous mixed types (`TEXT` ISO strings, `REAL` epoch seconds, `INTEGER` ms in one bot table, `datetime` objects in file-backend JSON). Producers obtain it via one framework utility (`now_ms()` in `modex_agent.utils.time`); consumers store and compare it as an integer — never re-derive via `time.time()` inline. SQL `DEFAULT` is `CAST(strftime('%s','now') AS INTEGER) * 1000` (SQLite) / `(EXTRACT(EPOCH FROM now()) * 1000)::BIGINT` (PostgreSQL). Append-only tables (audit log, transcript events, archive entries, delivered-id records) keep their domain-specific timestamp column (`decided_at`, `timestamp_ms`, `created_at`, `delivered_at`) and do not add `created_at`/`updated_at`; every other table has both. `updated_at` is auto-managed by a SQL trigger (`WHEN NEW.updated_at IS OLD.updated_at`) that fires only when the application did not explicitly set the value — so manual `UPDATE ... SET updated_at = ?` overrides the trigger, while forgetting to set it lets the trigger fill the current time. **File-backend JSON payloads** (`StorageRevision.updated_at`, archive entry `created_at`, `InboxMessage.timestamp` serialization) are unified to `int` ms in the same phase. **Runtime in-memory dataclasses** (`TurnSnapshot`, `TurnStateBase`, `OperationState`, `ApprovalTransaction`, `TurnSummary`, `StateQueryScope`) retain `float` seconds in Phase 1 — the SQLite adapter converts at its boundary (`int(snapshot.created_at * 1000)` on write, `row["created_at"] / 1000.0` on read); Phase 2 (deferred spec) will propagate `int` ms to these dataclasses. `ChatMessage.created_at` stays ISO-8601 string (display-only business data, never parsed for storage decisions).
_Avoid_: epoch seconds (precision loss and unit-mismatch bugs), ISO timestamp string (parsing cost, non-sortable as integer), unix time (ambiguous unit)

**Session Message State Machine**:
The three-state lifecycle for session memory messages: `normal` (active, prunable) → `pinned` (active, prune-exempt) → `soft_deleted` (invisible to active queries, retained until TTL). Background TTL job physically deletes expired `soft_deleted` rows. Prune returns pruned message content to the caller (archive/pruned/URB consumers) in the same transaction as the soft-delete.
_Avoid_: message lifecycle (too generic)

**Approval Audit Log**:
An append-only table recording every approval decision (approve/deny) with `turn_uuid`, `session_id`, `tool_name`, `tool_call_id`, `decision`, `deny_reason`, `decided_at`, `decided_by`. Immutable — no UPDATE or DELETE (except TTL cleanup). Closes the compliance gap where approval decisions were previously lost when `TurnSnapshot` was overwritten by the next turn.
_Avoid_: approval history (use "audit log" to emphasize immutability)

**Session Artifact Cleaner** (`SessionArtifactCleaner` ABC):
The framework ABC coordinating DB + file cascade deletion when a session is deleted. DB operations delete rows from sessions, memory_session_messages, todos, turn_snapshots, inbox_messages, approval_audit_log. File operations delete pruned, media, trace, output directories. Called by the business-layer `SessionGarbageCollector`. Orphan scanning (artifacts without an index record) is also handled through this seam.
_Avoid_: garbage collector (that is the business-layer orchestrator; the cleaner is the framework executor)

**Column Projection** (`modex_agent.persistence.column_projection`):
A declarative field-mapping abstraction used by SQLite adapters to split a `dict` (the ABC contract unit) into table columns plus a residual JSON column, and to re-assemble the dict on read. Defined by a `ColumnProjection` holding a tuple of `ColumnField` descriptors — each declaring the DB column name, the candidate dict keys (first hit wins; on read-back only the first key is re-populated), and an optional `ColumnCodec` for non-identity conversions. A `ColumnCodec.encode(column, value)` returns a `dict[str, Any]` of column→value (allowing a single field to fan out into multiple columns — e.g. `ContentCodec` writes both `content` and `is_content_json`); `decode(columns)` is the inverse. File backends do not use this mechanism (they store the dict wholesale); only SQLite adapters do. **The abstraction is the deep module seam that lets the SQLite and file backends share one ABC contract while diverging in storage shape.** Adding a new extracted column is a one-line change to the projection tuple, not a scatter of `if isinstance` branches.
_Avoid_: row mapper (that is the read side only; projection is bidirectional), field mapping (too generic — lacks the column-vs-JSON-residual distinction)

**InboxMQ**:
The evolved `InboxServer` ABC. Adds `deliver()` (sync) for cross-process CLI use. `DeliveredIdTracker` is merged into `InboxMQ` internal — delivered ID tracking is part of the inbox transaction, not an independent ABC. The `inbox_topics` table is now a minimal FK anchor (topic_id + scope triplet + timestamps only) — the former topic state machine (`pending → active → idle → expired`), `message_count`, `last_active`, and `consumer_task` columns were removed because no production query ever SELECTed them (`sessions_with_pending` reads `inbox_messages` directly, bypassing topics).
_Avoid_: inbox server (the older name; InboxMQ emphasizes the MQ semantics)

**MemoryStoreBundle**:
A frozen Pydantic model returned by `MemoryStoreRegistry.resolve()`, holding `MessageStore`, `KVStore`, `CursorStore`, and optional `ArchiveStore`. Replaces the `MemoryStorage` god interface. File implementation: one `DefaultScopedStorage` instance implements all four interfaces. DB implementation: four independent SQLite adapters.
_Avoid_: storage bundle (too generic)

**Trace Replay**:
Read-only re-rendering of a recorded trace — no code executes, no LLM calls fire. The lowest-cost "replay" level: you see what happened but cannot change it. Served by the Trace Path (通路 A) alone. Not reproducibility in the scientific sense.
_Avoid_: replay (ambiguous — qualify with the level)

**Checkpoint Re-execution** (reproducibility level 2):
Resume agent execution from a saved graph-state snapshot; nodes after the checkpoint re-run, including fresh LLM calls (non-deterministic). The default reproducibility level in ModexAgent. Served by the Repro Path (通路 B1). Built on existing `TurnSnapshot` / `RuntimeStateCodec` infrastructure, extended to per-iteration granularity via `AfterIterationHook`-driven checkpointing.
_Avoid_: replay (too ambiguous), deterministic replay (this level is NOT deterministic)

**Deterministic Replay** (reproducibility level 4):
Bit-identical reproduction from a complete side-effect capture (cassette). No network calls fire; all boundaries (LLM client, tool dispatcher, clock, RNG, external reads, retries) are faked from the cassette. The only true reproducibility for LLM agents — `temperature=0` is empirically non-deterministic (GPU batching, provider drift, continuous-prefill optimizations). Served by the Repro Path (通路 B2), opt-in per config flag. External CLI agents (Pi/OpenCode) are inherently incomplete for L4 (subprocess internals uncapturable) and marked `repro.incomplete=true`.
_Avoid_: replay, re-execution (use "deterministic replay" to emphasize bit-identical fidelity)

**Input Replay** (reproducibility level 3):
Re-run an agent against saved inputs with a potentially different model or config (LangSmith dataset / MLflow evaluate model). Fully non-deterministic — everything is re-derived from the prompt. Not a ModexAgent built-in; emerges naturally from the Training Data Derivation layer exporting inputs.
_Avoid_: replay, re-evaluation (use "input replay" for the level)

**Trace Path** (通路 A):
The observability data path — existing 5 hook points upgraded to emit OpenTelemetry spans (`gen_ai.*` semantic conventions). Samplable, redactable, streaming-first, low-overhead. Backend: local `FileSpanExporter` (default-on, JSONL) + optional OTLP HTTP to Langfuse (self-hosted) via OTel's native multi-`SpanProcessor` chain. Retains `reasoning_content` (decision b) while Memory layer still strips it. Carries `trace_id` that links to the Repro Path. **Replaces** the legacy `OperationRecord` / `operations.jsonl` format via a 3-phase migration (dual-write → agent reads new → legacy removed); agent self-read is preserved because agents read JSON by field name and OTel attributes are more self-descriptive than `metadata.*`.
_Avoid_: observability path (too generic), telemetry path

**Repro Path** (通路 B):
The reproducibility data path, split into two layers: B1 (Checkpoint, default-on, per-iteration `TurnSnapshot` via `AfterIterationHook`) and B2 (Cassette, opt-in, 6-category side-effect capture). Does NOT redact (redaction breaks fidelity). Linked to Trace Path via `trace_id`. The two paths are separate because observability wants sampling+redaction+low-cost while reproducibility wants full-fidelity+no-sampling — these requirements conflict and cannot share one data path.
_Avoid_: persistence path (too generic)

**Cassette**:
The side-effect capture artifact for Deterministic Replay (level 4). A content-addressed local file storing 6 categories of side effects: (1) LLM calls — prompt + full response object + model id + sampling params + latency + retry count; (2) tool calls — name + args + result + error + latency; (3) time reads — every `time.time()`/`datetime.utcnow()`; (4) RNG draws — every `random.random()`/`secrets.token_hex()`; (5) external reads — vector search, DB queries, HTTP fetches; (6) retries — each attempt + delay. Default capture scope: 1+2+6 (LLM + tools + retries); full scope: +3+4+5 (time + RNG + external reads), opt-in. "Skip any of them and the replay drifts." Cassette is the payload; the OTel trace is the index — one `trace_id` ties them together.
_Avoid_: trace (the trace is the index, the cassette is the payload), recording

**Training Data Derivation** (派生层):
The layer that derives training data from the Trace Path — not an independent capture path. Write-time: tags spans with `gen_ai.training.relevant` (L1 rule filter, online, microsecond cost). Read-time: `TrainingDataExporter` aggregates spans by `trace_id` into trajectories, converts to target format (SFT / DPO / tool-use), applies L2 heuristic scoring and optional L3 annotation (approval-as-preference or LLM-as-judge). Default granularity: trajectory-level (primary) + iteration-level (auxiliary); turn-level is not produced (loses reasoning process). Avoids a third write path by deriving from already-recorded traces.
_Avoid_: training pipeline (derivation is extraction, not training), data collection (too generic)

**Trajectory** (training data unit):
One complete agent turn's execution record, aggregated from all spans sharing a `trace_id` — multi-round reasoning (`reasoning_content`), multi-step tool calls, and final response. The primary training data granularity. An iteration is a sub-unit (one ReAct round: think → act → observe). Format: OpenAI messages + `tool_calls` + custom `reasoning_content` field. Trajectory-level data serves SFT (multi-step reasoning), RLHF (trajectory reward), and STaR (self-bootstrapped reasoning).
_Avoid_: trace (a trace is the raw observability record; a trajectory is the training-shaped derivation), session (too coarse)

**Iteration Checkpoint**:
A `TurnSnapshot` captured at an iteration boundary (after `AfterIterationHook` fires), extending the existing approval-suspend snapshot mechanism to per-iteration granularity. Re-execution from iteration N replays iterations 1..N-1 from the snapshot (deterministic read) and re-runs iteration N+ (non-deterministic, fresh LLM calls). Driven by a `CheckpointHook(AfterIterationHook, SnapshotPolicy)` registered to `HookRunner` — the graph engine stays agnostic (it already dispatches `AFTER_ITERATION`). Does not use `getattr` dispatch (HookRunner uses `isinstance` + ABC per project convention).
_Avoid_: node checkpoint (too fine-grained), turn checkpoint (too coarse)

**PromptCaptureStrategy**:
The pluggable strategy ABC (ADR-0024 IN11) controlling how much of the LLM request prompt is captured into the `chat` span's `gen_ai.request.messages` attribute. Sole Phase-2 implementation: `SummaryPromptCapture` (last N messages, each truncated; system prompt excluded — records hash + length only). Extension point for future tiers: `MinimalPromptCapture` (model + count only), `FullPromptCapture` (all messages, cassette-redundant). Selected via `ObservabilityConfig.prompt_capture` (default `"summary"`). Exists because G2 prompt-capture scope is a trade-off between Langfuse analysis value and local IO pressure — the right scope is empirical and may change, so it must be swappable without modifying `TraceCollectorHook`.
_Avoid_: prompt recorder, message serializer (use "PromptCaptureStrategy" for the pluggable ABC)

**TraceDrivenLoopDetectorHook**:
A placeholder hook class (ADR-0024 IN13, Phase 3, **not registered**) documenting the future entry point for agent-self-read harness decisions. Intends to consume local `spans.jsonl` via `TraceQuery` to drive runtime decisions: loop detection (tool-call fingerprint hashing), error-recovery strategy selection, predictive compression. Inherits `AfterToolExecutionHook` + `AfterIterationHook`. Implementation deferred until Phase 2 trace data is available to observe actual agent behavior patterns. Reference targets: hermes-agent's stale-call circuit breaker (`chat_completion_helpers.py` #58962) and 27-reason `error_classifier.py` taxonomy — but strategies should be calibrated against real trace data first. Distinct from `ToolCallDeduplicator` (streak-based, in-memory, no trace read): this hook reads persisted trace history across iterations.
_Avoid_: loop detector (too generic), trace hook (too generic — `TraceCollectorHook` is the trace-emitting hook; this is the trace-consuming hook)

**Execution Strategy**:
A pool-shape recipe — a frozen `ExecutionStrategy` ABC implementation that owns one full pool shape (ReAct graph loop, external CLI harness, future shapes). Declares capability flags (`supports_subagents`, `requires_main_agent_tools`) and exposes two methods: `assemble(ctx) -> StrategyAssembly` (construct all runtime components this strategy needs) and `validate_pool_spec(spec)` (fail-fast at startup). The strategy is stateless: it is called once during pool assembly, returns a fully-configured `StrategyAssembly`, and is then never touched again at runtime. Adding a new pool shape = implementing this ABC + registering it; `pool_builder.create_pool` and `AgentPipeline` do not branch on strategy identity. Replaces the `ExecutionStrategy` enum (now renamed `ExecutionStrategyKind`, a closed string set used only for pool.yml lookup and registry resolution). See ADR-0025.
_Avoid_: agent type (too generic — that is the `Agent[E]` ABC), runtime mode (too vague), execution mode (too vague)

**AgentImplementation**:
A derived enum (`NATIVE` / `EXTERNAL`) classifying *how* an agent is implemented, orthogonal to its topology kind (`AgentCommKind.NORMAL` / `SUBAGENT`). Derived from `ExecutionStrategyKind` (`EXTERNAL` → `EXTERNAL`, all other strategies → `NATIVE`); **not a spec field** — the source of truth on disk and in `AgentDescriptor` is `execution_strategy`. Exists as a code-layer enum so judgement sites read `if impl == AgentImplementation.EXTERNAL` instead of `if execution_strategy == ExecutionStrategyKind.EXTERNAL` (rule 14: enums over raw strings). The four valid combinations are documented in the enum's docstring: `NORMAL+NATIVE` (default main agent), `NORMAL+EXTERNAL` (external coding CLI as pool main, ADR-0022), `SUBAGENT+NATIVE` (in-process subagent), `SUBAGENT+EXTERNAL` (external coding CLI as subagent, ADR-0027).
_Avoid_: implementation type (too generic), agent type (collides with older loose usage)

**SubagentSpec**:
The Pydantic model describing one subagent template inside a pool's `PoolSpec.subagents`. Fields: `agent_name`, `max_steps`, `tool_preset`, `tool_supplements`, `roles`, `system_prompt_mode`, `context_mode` (`fork` / `append` / `none`), `fork_max_messages`, `mcp` (per-agent MCP server selection), `prompt_name` (deferred — declared but not yet consumed by materialize). As of ADR-0027 also carries `execution_strategy: ExecutionStrategyKind` (default `REACT`) and `provider_kind: ProviderKind | None = None` — the same two fields `MainAgentSpec` carries — so a subagent may be implemented either as an in-process ReAct agent (default) or as an external coding CLI. A cross-field `@model_validator` enforces `provider_kind` is set iff `execution_strategy == EXTERNAL`; the same validator is backfilled to `MainAgentSpec`. `SubagentSpec` is `frozen=True, extra="forbid"` per pool-config convergence (ADR-0020).
_Avoid_: subagent config (too generic), subagent template (that is the `AgentTemplate` runtime object built from a `SubagentSpec`)

**SubagentExternalBuilder**:
The framework-layer ABC that builds an `AgentInstance` for a subagent whose `execution_strategy == EXTERNAL`. Single method `build(spec, descriptor, parent_session, invocation_id, deps) -> AgentInstance`. **Independent of the main agent's factory path** — `AgentMaterializeDeps.subagent_external_builder` is an optional field, injected only by pools that declare at least one external subagent; react-only pools leave it `None`. The builder owns the full per-invocation assembly of an external subagent: backend / session_store / parser / env_spec / pipeline, then returns the `AgentInstance` ready for `pool.register_resident`. Internalises the 4 external collaborators that `ExternalAwareFactory` consumes for the main-agent path — but built per-invocation rather than pool-scoped, because backends (OpenCode SSE, Pi subprocess) are not safe to share across concurrent subagent invocations. Symmetric to `ExternalAwareFactory` (main path) — one builder for the subagent path, one factory for the main path, neither depends on the other. See ADR-0027.
_Avoid_: external subagent factory (collides with the main-agent `ExternalAwareFactory`), subagent external assembler (too generic)

**BackendProvider**:
The framework-layer ABC that is the unified backend lifecycle seam for `ExternalAgent`. Three methods: `acquire(modex_session_id, turn_context) -> StreamingProviderBackend` (called at turn start), `release(backend, *, turn_failed) -> None` (called at turn end, with failure flag so the provider can invalidate a backend on stale-session errors), and `close_all() -> None` (called at pool shutdown). Replaces `ExternalAgent`'s historical fixed `backend` constructor field — the agent no longer holds a backend, it borrows one per turn from its `BackendProvider`. **Unified across main and subagent paths**: main agent path injects `PoolScopedBackendProvider` (single pool-scoped backend, reused across all turns — externally indistinguishable from the pre-ADR-0027 fixed-backend behavior); subagent path injects `CachingBackendProvider` (per-modex_session_id cache for warm backends with `MAX_WARM_BACKENDS` LRU cap; shared single instance per provider_kind for stateless per-turn backends). The `MAX_WARM_BACKENDS` cap applies **only** to warm backends (`OpenCodeServerBackend`); per-turn-spawn backends (`OpenCodeBackend`, `PiBackend`) are stateless and need no cap. See ADR-0027.
_Avoid_: backend factory (collides with `ExternalAwareFactory` and `SubagentExternalBuilder`), backend resolver (superseded — `BackendProvider` is the unified seam, `acquire/release` is richer than `resolve`), backend manager (too generic)

**SubagentNotificationArtifactKind**:
The discriminator for how a subagent's `<artifacts>` block in a `<subagent_notification>` is populated. Two values (matching `AgentImplementation`): `NATIVE` → react subagent, artifacts contain `<trace>` (spans.jsonl path), `<output>` (OUTPUT.md path), `<output_status>` (written|missing); `EXTERNAL` → external coding CLI subagent, artifacts contain only `<replied>` (bool — whether the subagent emitted at least one `modexctl send` to its parent during the turn). The uniform parts of the notification (`agent`, `invocation_id`, `status`, `stop_reason`, `is_normal`, `error`, `hint`, `summary`) are identical across both kinds — the parent agent's decision logic reads only the uniform parts and does not branch on artifact kind. The external `<replied>` flag replaces OUTPUT.md presence as the "did the subagent produce a deliverable" signal, because external CLIs do not write OUTPUT.md. See ADR-0027.
_Avoid_: artifact type (too generic), notification kind (collides with message_type vocabulary)

**Pool Assembly**:
The two-layer construction process for a pool, extending the existing "Assembly" term. Layer 1 — **common assembly** (executed by `pool_builder.create_pool` for every pool): broker, `InboxMQ`, `AgentMessageBus`, `InboxPoller`, `SessionIdFactory`, `AgentPool`, `CommunicationTargetStore`, notification/communication services. Layer 2 — **strategy assembly** (delegated to the pool's `ExecutionStrategy.assemble()`): all strategy-specific components — `Agent`, `TurnRunner`, provider/tool/skill/MCP/terminal managers (react), or `StreamingProviderBackend` + `ExternalSessionMapStore` (external). The strategy returns a `StrategyAssembly` (frozen dataclass — a runtime-object container, NOT a Pydantic value object per rule 12's runtime-object exemption); the pool builder then performs common post-assembly (main-agent registration, communication tool registration, `AgentPipeline` construction) using only the assembly's common surface. No `if execution_strategy == ...` branches in either layer. See ADR-0025.
_Avoid_: pool wiring (use "wiring" for specific connections, "assembly" for the process), pool construction (too generic)

**Turn Runner**:
The locked-turn orchestrator sitting between `AgentPipeline` and `Agent.run()`. `AgentPipeline` owns pre-lock dispatch (route → dedup → busy mode → session lock) and then delegates the locked turn to a `TurnRunner` ABC implementation via `process_locked()`. The runner owns everything strategy-specific about a single turn: context assembly, approval suspend/resume, governance, hooks, interceptors, runtime state (react's `ReActTurnRunner`); or the minimal "set `current_input`, call `agent.run()`, fire on_session_start/end" path (external's `ExternalTurnRunner`). `AgentPipeline` holds a `TurnRunner` (ABC) reference, never a concrete subclass — adding a new strategy never touches the pipeline. The ABC lives in `pipeline/turn_runner_abc.py` (1 abstract method `process_locked` + 3 lifecycle methods with no-op defaults + 2 post-construction wiring methods + 12 read-only properties — see ADR-0025 D3 deviations for why the surface is larger than the original "one method" spec); concrete runners live alongside their agents (`pipeline/turn_runner.py` for react, `agents/external/turn_runner.py` for external). See ADR-0025.
_Avoid_: turn executor (too generic), turn handler (too generic)

**Strategy Assembly** (`StrategyAssembly`):
The frozen dataclass returned by `ExecutionStrategy.assemble()`. A runtime-object container carrying everything the pool builder and pipeline need: the `Agent`, the `TurnRunner`, react-only collaborators (`LLMProvider` / `ToolManager` / `SkillManager` / `MCPManager` / `TerminalManager` / `ContextManager` / `DreamEngine` / `CommandProcessor` / `InMemoryControlChannel` — all `None` for external), external-only collaborators (`StreamingProviderBackend` / `ExternalSessionMapStore` — all `None` for react), common services (`AgentNotificationService` / `AgentCommunicationService` / `CommunicationTargetStore`), and `extra_cleanup` hooks. Typed as a frozen `@dataclass` per rule 12's runtime-object exemption (NOT a Pydantic `BaseModel` — its fields are live objects with connections/state, not serializable values); cross-module but single-purpose (passed once from strategy to pool_builder, never serialized). See ADR-0025.
_Avoid_: pool bundle (use "Pool Instance" for deployment-scoped runtime resources), strategy result (too generic)

**Graph Spec**:
The declarative definition of a graph workflow — a YAML document compiled into a `CompiledGraph` topology (nodes, edges, scheduler kind, trigger mode, entry node). Identified by `spec_id`. Immutable once compiled; a new spec version creates a new `spec_id` record, not a mutation. Distinct from **Graph Instance** (one runtime materialization of a spec) and **Graph Invocation** (one execution of an instance). The REST API exposes specs at `/api/graphs/specs`.
_Avoid_: graph definition, graph config (collides with `PoolConfig`/`AppConfig`), graph template (collides with `AgentTemplate`)

**Graph Instance**:
One runtime materialization of a **Graph Spec**, identified by `graph_instance_id`. Carries a version chain via `GraphInstanceStore.begin_invocation` — each version is one **Graph Invocation**. An instance is reusable: a completed/failed instance can be re-invoked (version+1) to execute the spec again with new input, preserving the same `graph_instance_id`. This is the "conversation-level" entity in the graph domain, analogous to a chat session. The WebUI manages instances per-spec (instance list scoped by `spec_id`). Distinct from **Node Instance** (a `ParallelScheduler` execution unit `node_name#seq`, which is per-node-per-execution, not per-graph).
_Avoid_: graph run (collides with the deprecated `GraphRunRecord` name — use "Graph Invocation" for one execution), graph execution (that is the act, not the identity), graph context (collides with `GraphContext` the runtime object)

**Graph Invocation**:
One execution of a **Graph Instance**, identified by `(graph_instance_id, version)`. Created by `GraphInstanceStore.begin_invocation(gid)` which appends a new version row with `status=RUNNING`. The prior version's node states remain as history but are implicitly superseded by the new version's higher version numbers (`load_latest` returns `ORDER BY version DESC LIMIT 1`). Carries one `GraphIORecord` (input + output) once the `GraphIORecord` model is version-scoped. Terminal states: `completed` / `failed` / `crashed` / `stopped`. A completed/failed invocation can be followed by a new invocation on the same instance (version+1). This is the "turn-level" entity in the graph domain, analogous to one chat turn. The `bootstrap` entry point derives seed nodes for one invocation; re-invocation of a completed instance starts a fresh execution from `entry_node` (bootstrap returns `[entry_node]` when no CRASHED/RUNNING nodes or PENDING delivers exist, regardless of prior COMPLETED records).
_Avoid_: graph run (deprecated — the old `GraphRunRecord`/`runGraph`/`getRuns` API names are being renamed to align with this term), run (too generic)

**Topology Injection**:
The structured graph context injected into an agent's system prompt when the agent runs inside a graph node (`BotAgentNode.execute`). Carries the full DAG — graph name, all node names without type labels (no role descriptions), edges, the current node highlighted, END with its aggregation-semantic label, and Origin Request linkage. Serialized by `BotAgentNode` into `TurnCustomKey.GRAPH_TOPOLOGY_CONTEXT` and read by `GraphWorkflowProvider` at LLM-call time. Node descriptions are excluded from topology (noise from unrelated branches); they are provided only at interaction points — upstream descriptions in `_format_integrated_input`, downstream descriptions in `GraphDeliverTool.description`. See ADR-0038.
_Avoid_: graph context (too generic — could mean GraphContext the runtime object), topology context (ambiguous — could mean the serialized string or the concept), graph prompt (collides with BasePromptProvider)

**Origin Request**:
The user's original input that triggered a graph run. Enters the graph through `__start__`, flows as `ctx.user_input.content`, and is the root task every node works towards. Injected into agent context as a `[Origin Request]:\n<content>` block in the SYSTEM_REMINDER message (via `_format_integrated_input` in `BotAgentNode.execute`). The topology section links it to `__start__ (entry — receives Origin Request)` so the agent distinguishes it from node-to-node delivers. Distinct from upstream node delivers (`[Input from graph node 'X']`) — Origin Request is the graph-level root input, not a node-level handoff.
_Avoid_: user input (too generic — could mean any user message), original request (ambiguous — could mean the first HTTP request), trigger input (implementation jargon)

**Turn Context Configuration**:
The runtime configuration layer that shapes a per-turn `AgentContext` after `build_runtime_and_context` constructs the fresh per-turn objects. A pipeline of ordered configurators, each deciding via `applies(descriptor)` whether it applies to the current turn and mutating `AgentContext` in place. Orthogonal to pool wiring (which handles main/subagent/native/external differences at assembly time) and to the system prompt pipeline (which shapes prompt content); this layer shapes runtime concerns — tools, approval, turn limits, graph context binding, workspace binding. Graph-scheduling turns (direct node execution and inbox-driven subagent turns under a graph context) activate the graph configurators; normal-session turns skip them via `applies() → false`. See ADR-0039.
_Avoid_: turn setup (too generic), context builder (collides with TurnContextBuilder), per-turn config (ambiguous — could mean config read at turn time)

**Graph-aware message**:
An `AgentMessageEnvelope` carrying graph-scheduling metadata (`graph_instance_id`, `source_node_id`, `graph_spec_id`) in its `metadata` map, marking it as produced by a graph-scheduling turn rather than a normal inter-agent message. The consumer stamps the receiver's session with the graph context so the receiver's turn runs with graph-aware configuration (GraphDeliverTool, approval disabled, topology injection, MAX_TURNS). Without this metadata, the receiver runs as a normal-session turn. The metadata is the sole signal distinguishing a graph-scheduling subagent turn from a normal subagent turn — both use the same inbox mechanism.
_Avoid_: graph message (too generic), graph envelope (collides with AgentMessageEnvelope the type)

**Scope**:
The sole structural primitive of assembly (ADR-0042): a node in the declared scope tree whose resources, paths, and config defaults resolve along its parent chain. Landed as the frozen `ScopeSpec` model (`modex_agent/scope/spec.py`) with exactly two v1 kinds; the production declaration is one YAML file (`config/scopes/bot.yml`) loaded by `load_scope_declaration` — the legacy `config/pools/` roster is deleted. Workspace and pool are preset scope *kinds* — machinery face is skeleton, data face is configuration — not architecture concepts.
_Avoid_: layer, container, environment

**Scope Kind**:
The preset type of a scope (`ScopeKind.WORKSPACE` / `ScopeKind.POOL` in v1): its runtime-machinery binding is fixed skeleton; its contents (resources, nesting, defaults) are declared configuration. A workspace layer (`WorkspaceSpec`) selects the memory backend (`persistence`), path layout (`paths`), and shared MCP server set (`mcp`) and hosts the pool trees — every selection field is `None` = inherit the service-level domain config, so an undeclared deployment keeps today's data layout. A pool (`PoolSpec`) carries one flat agents tree plus peer links; a pool-as-root declaration (no workspace layer) boots the single-home stack.
_Avoid_: scope type, scope class

**Declaration Tree**:
The compile-time, config-declared tree of scopes and agents stating who *can* dispatch whom — validated by the two-phase `ScopeTreeValidator` (declaration-shape rules V1-V5/V7/V10/V11 pre-derivation; effective-value rules V6/V9 on the compiler's output) and compiled by the pure `ScopeCompiler` (`compile_scope`) into per-agent `AssemblySpec`s, effective toolsets, and provenance records; never executed. Root-ness is derived (the sole in-degree-0 node), never declared. Counterpart to the runtime-emergent **Session Tree**; the two are never conflated.
_Avoid_: scope graph, config tree

**Session Tree**:
The runtime record of who *actually* dispatched whom: SessionTreeManager + SessionTreeStore track parent-child sessions and invocation-id branches as `task` dispatches happen, at any depth. The machine is skeleton (always on, one per pool); only the storage backend is swappable (InMemory / LocalFile / Sqlite).
_Avoid_: dispatch tree, conversation tree

**Skeleton**:
The fixed runtime machinery hosting assembled products: InboxPoller, AgentMessageBus, SessionTreeManager, ScopeRegistry (renamed from WorkspaceRegistry by the addressing convergence), PoolRouter (the session→pool dispatch shell; agent→pool ownership is a compile-time declaration lookup, not a runtime scan), state.db lifecycle. Always on, never slot-replaced, never config-swapped — behavior parameters flow through config; machines are architectural constants (ADR-0042). Contrast the 10 swappable component slots (ADR-0041).
_Avoid_: runtime core, kernel, engine (collides with Graph Engine)

**Context Chain**:
The layered read-only data surface passed to component factories at assembly: `WorkspaceContext → PoolContext → AgentContext`, each a frozen typed carrier. Implemented as a diamond — `AgentContext(WorkspaceContext, PoolContext, AssemblyContext)` in `plugins/assembly/context.py`, built per assembly by `agent_context_chain`. A factory's signature declares which layers it may read — the type is the capability boundary (in contrast to the reference project's comment-convention host/agent planes). Generalizes `AssemblyContext`; the `SubagentInvocationContext` special case is absorbed and deleted.
_Avoid_: DI container, factory context, assembly inputs

**Profile**:
A named defaults macro referenced by scopes and agents; resolution is inheritance + deep merge (framework defaults ← profile ← local declaration), single level (the `ProfileStore` refuses nested references), list fields replaced wholesale, effective-value provenance inspectable (per-field winning layer via `AgentProvenance`; the WebUI bill recomputes from YAML per request — no boot-time cache). Landed as `Profile`/`ProfileStore` with `STANDARD_PROFILES` — the five toolset presets as code-level frozen constants — bound by position-derived toolset selection (root → `full`, non-root → `read_write`). Generalized `tool_preset` (the field is dead; its values live on as position-derived defaults).
_Avoid_: template (collides with AgentTemplate), preset (collides with tool_preset)

**Canonical Message**:
The single typed message model (`ChatMessage`) shared by storage, memory, governance, and LLM requests; protocol engines translate it into each wire format at call time. Reasoning state rides on declared fields — never on `model_extra`. Per ADR-0046.
_Avoid_: unified message, internal message, neutral message model

**Wire Message**:
The per-protocol request structure (Pydantic) that one protocol engine builds from canonical messages; it exists only for the lifetime of a single request and is never persisted.
_Avoid_: API message, provider message

**Protocol Engine**:
One wire-protocol implementation (`LLMProtocol`): request-body building, stream-to-event translation, and provider-quirk parsing (e.g. think-tag extraction). Stateless across requests; all per-request state lives in the translate generator's closure. Per ADR-0046.
_Avoid_: adapter, provider backend

**Event Stream**:
The provider-neutral `LLMStreamEvent` sequence (text/reasoning deltas, completed tool calls, usage, finish) — the single streaming primitive of the provider system. `chat_stream` is a fold of the event stream with delta callbacks into one `LLMResponse`. Per ADR-0046.
_Avoid_: stream chunks, token stream

**Reasoning Replay**:
The declared assistant-message fields (`reasoning_content`, `reasoning_signature`, `reasoning_item_id`, `reasoning_encrypted_content`) that carry a turn's chain-of-thought state so the next request can pass it back (DeepSeek reasoning_content passback, Anthropic thinking signature, OpenAI Responses item_reference/encrypted_content).
_Avoid_: thinking passback, CoT state

**Tool Stream Key**:
The stream-local identifier a provider uses while streaming one tool call (chat/Anthropic block `index`, Responses `item_id`) — always distinct from the final `call_id` that tool results must reference. Keying accumulation on the stream key, never on `call_id`, is the rule that survives interleaved parallel deltas.
_Avoid_: tool index (ambiguous), call_id (that is the pairing key, not the stream key)

## Relationships

- A **Workspace** owns one or more **Pool Instances**; pool instances are not shared across workspaces.
- A **Pool** is described by exactly one **PoolConfig**; multiple pools in one workspace each have their own `PoolConfig`.
- A **Pool** contains one **Main Agent** (the entry point) and zero or more subagents. Subagents are not separate pools — they share the pool's bus, broker, and tracker.
- **Assembly** turns `AppConfig` (root) into nested `PoolConfig` instances, then into `Pool` runtime objects held by a `Workspace`.
- A **ReAct Agent** runs on the **Graph Engine**; the graph is the execution substrate, the ReAct agent is one configuration of it (4-node loop). Other tool-using agents (`ArchiveSummarizer`/`KnowledgeConsolidator`/`ExperienceReviewAgent`) inherit `ScopedFileAgent` and internally construct a `ReActAgent` in clean mode, so they indirectly use the Graph Engine. `ExternalAgent` does NOT use the Graph Engine — it drives a subprocess streaming harness directly.
- A **GraphInterrupt** is raised by a `Node[S]` (via `ctx.interrupt(value)`); the engine propagates it as part of the **GraphBubbleUp** family, the `TurnRunner` catches it for approval, and re-enters the graph after persistence. The Graph Engine's interrupt model is **suspend-without-re-execution** (the node's already-applied state updates persist; resume re-enters at the next iteration).
- **Terminal Visibility** is the second upstream-visible design axis of the terminal system (alongside **Shell Family**). The OS axis does NOT exist at this level: Windows-vs-Linux is an implementation fork realised inside `TerminalBackend` subclasses. **Two invariants**: (a) the manager layer (`BaseTerminalManager`) is forked ONLY by (Shell Family, Visibility), never by OS or by capability — capabilities are folded inward as default-off flags per ADR-0010 Decision 8; (b) `_expected_state` (set by `set_expected_state(...)`, consumed by `detect_interference` on visible sessions via `TerminalTool`) and the trio `_busy_after_timeout` / `_last_status` / `_command_started_at` (updated by `apply_outcome(...)`) are **two orthogonal state slots** in `TerminalSession` and coexist under ADR-0010 Decision 7 — neither subsumes the other.
- Every inter-agent message flows through one per-pool **Agent Inbox** addressed to a receiver session. The pool's **InboxPoller** is the sole between-turn driver; **single-flight** per session is enforced by an `inflight` task table (no per-session lock). A message arriving mid-turn is **folded into** the running turn (`InboxFlushHook`, inter-agent types only), not turned into a follow-up turn; a human DM (`external_input`) is left for the next between-turn. A subagent instance is **materialized** on the first turn of its session. Both directions — `send_to_agent` (agent→agent `task_request`) and the subagent reply (`SubagentAutoSendHook` → `agent_result`) — converge on one carrier, `bus.send(session_id, envelope)`. Decided in ADR-0015 as revised by the poll-driven redesign.

## Flagged ambiguities

- "**control channel**" historically meant the runtime control plane in `modex_agent/control/`, but that package is largely **vestigial** — channels are constructed and threaded but have no live producers/consumers. Real cancellation is `asyncio.Task.cancel()` in `AgentPipeline`. Use "control channel" only when quoting the package; prefer "control plane" for the abstraction.
- "**pipeline**" was overloaded: it meant both the old `create_app`/`App` entry point (removed by ADR-0001) and the `AgentPipeline` orchestration layer that survives. "Pipeline" alone now means `AgentPipeline`; the old entry point is gone.
- "**Approval**" was historically **not wired in pool mode** (pool_builder skipped `RuntimeAssembler`; `ToolNode._get_tier()` always returned `NORMAL`). Now **implemented end-to-end** (framework → bot → webui; see **ADR-0008** + its Implementation outcome): path-tiered, default-off (opt-in per main agent), main-agents-only, one shared state machine (`apply_resume`) with per-channel surfacing. webui decisions flow as a structured `InputMessage.approval_decision` through the input pipeline (not slash commands); IM uses `/approve` text. Push + pull share one `ApprovalRequestView` DTO; pull (GET pending) is webui-only. Per **ADR-0011**: the batch is **atomic** (deny one = cancel all, including approved/exempt); webui **Deny All** short-circuits and seals the batch, **Approve** is per-request, IM stays one-at-a-time; webui surfacing is **pull-driven** by a dedicated suspend signal, and GET returns only genuinely-pending requests.
- The **session id format** is `{prefix}.{agent_name}` (dot separator, two segments) from `SessionIdFactory`. The prefix is `encode_snowflake(conversation id)` for a normal session and the verbatim `invocation_id` for a subagent session; `send_to_agent` echoes the subagent's `invocation_id` in its ack. For **peer-pool communication** (ADR-0019) the sender's session prefix is reused verbatim as the receiving session's prefix, creating an implicit **Session Group**: A→C creates `convA.mainC`, and C→A's reply lands back on `convA.mainA`. No fresh invocation_id is minted; the sending agent never sees one in the ack or XML. The receiving peer session is a **root session** (`parent_session_id=null`) — peer agents are equals, not parent/child.
- The older inter-agent mechanism had **two competing consumption paths** (`InboxFlushHook` fold-in vs eager `_handle_inbox_wakeup` poll-and-spawn-turn) and a **per-message dispatch that could be cancelled while waiting for the per-session lock, dropping messages**. ADR-0015 collapsed these onto a single-flight **Drainer**; the **poll-driven redesign** (see the `InboxPoller` entry above) then replaced the Drainer / `SessionInputQueue` / `_session_gates` layers with a per-pool **InboxPoller** + `inflight` table. Fold-in remains the only mid-turn path; there is no per-session lock. (ADR-0015's D4 `AgentTarget` class was never implemented — targeting is store-lookup + strategy dispatch as of ADR-0019; before that it was `_resolve_target` + `_send` policy checks.)
- **Cross-pool communication** (ADR-0019) is an **optional framework capability**, not a new assembly mode. The framework gains a `bus_ref` field on `CommunicationTarget` and a `PeerNormalStrategy` that delivers to it; it has no concept of "peer pool". The business layer declares peers (bidirectionally), acquires bus references during post-assembly, and populates each pool's `CommunicationTargetStore`. Default state (no peer wiring) is byte-for-byte today's behaviour — `bus_ref=None` means "route locally". Three semantics follow from this design: (1) peer sessions share the sender's prefix, forming an implicit **Session Group** (designed behaviour, not context pollution); (2) the sender never sees an `invocation_id` in ack or XML; (3) the receiving pool's `InboxPoller` registers the session on first turn (sender does not write to peer registry). Deferred to later revisions: the generalized `AutoSendHook` (NORMAL peer reply), a communication-log artefact for multi-peer conversation history, per-pair context fork, and completion-notification auto-receipt.
- The **Trace Path** and **Repro Path** are two separate data paths linked by `trace_id`. They cannot merge because their requirements conflict: the Trace Path wants sampling + redaction + streaming + low overhead; the Repro Path wants full fidelity + no sampling + no redaction. **Training Data Derivation** is a read-time consumer of the Trace Path, not a third write path. The three layers (Trace / Repro / Training) share `trace_id` as the join key — any record in one layer can locate its counterpart in the others.
- An **Iteration Checkpoint** is captured when `AfterIterationHook` fires (the hook point already exists and is already dispatched by `GraphEngine.run`). The checkpoint is a regular `TurnSnapshot` with `SnapshotReason.ITERATION`; no new hook point or graph-engine change is needed. A `CheckpointHook(AfterIterationHook, SnapshotPolicy)` registered to `HookRunner` drives capture — unregistered means no checkpoint (existing behaviour unchanged). This extends the existing approval-suspend snapshot mechanism from per-turn to per-iteration granularity.
- **Deterministic Replay** (level 4) via **Cassette** is opt-in (`repro.cassette` config flag). Default scope captures LLM calls + tool calls + retries (categories 1+2+6); full scope adds time + RNG + external reads (3+4+5) via `repro.cassette=full`. External CLI agents (Pi/OpenCode) are inherently L4-incomplete — subprocess internals are uncapturable; the parent's `invoke_agent` CLIENT span is marked `repro.incomplete=true`. The Cassette is the payload; the OTel trace is the index.
- **reasoning_content** is retained in the **Trace Path** (for training data derivation) but stripped in the **Memory layer** (for context budget) — two different serialization paths, no conflict. The Memory layer's `ChatMessage.to_dict()` stripping is unchanged. The Trace Path records `usage.reasoning_tokens` as a custom OTel attribute (the `gen_ai.*` semconv does not yet have a dedicated reasoning-tokens field).
- The **Trace Path** has two consumer roles served by one data source (`spans.jsonl` + optional OTLP). (1) **Human analysis** — Langfuse receives `gen_ai.*` spans via OTLP, renders trace trees, computes cache hit rate / tool correctness rate / trajectory structure; the framework is Langfuse-agnostic (OTLP-only, no `langfuse` SDK dependency, per ADR-0024 IN9). (2) **Agent self-read** — the agent process reads local `spans.jsonl` via `TraceQuery` to drive runtime harness decisions (Phase 3, `TraceDrivenLoopDetectorHook` placeholder). One emission, two consumers — this dual-use is the harness-engineering advantage of the OTLP-only route over a vendor-SDK route. Phase 2 (span gap remediation, IN10) serves both: fixing G1–G11 makes traces complete enough for both Langfuse analysis and agent self-read.
