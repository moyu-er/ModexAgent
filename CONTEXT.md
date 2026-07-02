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

**CommandTool** (`bash` tool, `modex_agent.tools.terminal.command_tool.CommandTool`):
The agent-facing command execution tool for persistent PTY sessions. Orchestrates `Session.primitives` + `poll_until_settled` + `Session.apply_outcome(result)` per ADR-0010 Decision 7 — Session owns state, the Tool owns orchestration. Returns XML `<command_result>` with status (completed / executing / timed_out / paginated / waiting_input / stuck). Falls back to `SubprocessTool` when terminal backends are unavailable. _Avoid_: ShellTool, bash (informal)

**ProcessTool** (`process` tool, `modex_agent.tools.terminal.process_tool.ProcessTool`):
The agent-facing tool for interacting with a running process inside a persistent PTY session (write / submit / send_keys / paste / interrupt / kill). Same orchestration pattern as CommandTool: `Session.primitives` + `poll_until_settled` + `Session.apply_outcome(result)`. Never used by subagents. _Avoid_: terminal (too generic)

**Shell Family** (`ShellFamily`):
The behavioural class of a shell — bash / zsh / sh / cmd / powershell — embodied by `ShellInfo(family, path, platform)`. ADR-0010 elevates Shell Family to one of two **upstream-visible design axes** for the terminal system (the other is Terminal Visibility). OS is NOT a design axis: it is an implementation fork collapsed entirely into the `TerminalBackend` subclasses. _Avoid_: shell type, terminal type (too generic)

**Terminal Visibility** (`TerminalVisibility` enum, `VISIBLE` / `HIDDEN`):
Whether a human can observe or intervene in the terminal session's window. ADR-0010 splits visibility expressions by mechanism:
- *Structural* visibility difference (different I/O architecture) → subclass split (`WinptyHiddenBackend` vs `WinptyConsoleWindowBackend`: in-process winpty vs external host process + TCP socket bridge).
- *Switch* visibility difference (one `new_session(attach=…)` flag) → single class with `visibility=` parameter (`TmuxBackend`).
Unsupported (transport, visibility) combinations are rejected at the factory (`UnsupportedVisibilityForTransport`) rather than silently falling back. _Avoid_: visible mode, window mode (too vague)

**TerminalTool** (`terminal` tool, `modex_agent.tools.terminal.tool.TerminalTool`):
The agent-facing tab-management tool (open / close / list / select / history / interrupt). It is the *only* consumer of `TerminalSession.detect_interference` (which reads the `_expected_state` slot set by `set_expected_state(...)`); per ADR-0010 Decision 7, this interference-detection slot is orthogonal to the three slots (`_busy_after_timeout` / `_last_status` / `_command_started_at`) owned by `apply_outcome(result)`, and is therefore preserved when apply_outcome is introduced. _Avoid_: tab manager (informal)

**Attachment**:
A file bound to a message in a conversation, identified by an opaque id, rendered direction-agnostically but stored asymmetrically. The system's purpose is conversation-level file awareness + tool-based inspection by the agent, plus symmetric IM/WebUI download — not file transfer in isolation. `kind` (image / extractable-document / other) is classified once at ingest from magic-byte MIME. Two inspection mechanisms coexist: mechanism B (tool-based — the agent sees only a path reference and a tool's text result; works with any model; the v1 path) and mechanism A (native multimodal — file bytes inline as a model content block, gated on `ModelCapabilities`; deferred). The injected agent reference carries `name + mime + size + absolute_path` (no download id).
_Avoid_: upload, media file, blob (use "Attachment" for the bound-to-message concept)

**MediaStore**:
The framework ABC that persists **inbound** attachment bytes, swappable to object storage (S3-class) later behind one ABC, with a `LocalFileMediaStore` now. Routed per-(workspace,pool), mirroring `WorkspaceScopedTranscriptStore.store_for` — a service-singleton with unified workspace+pool routing, not a parallel mechanism. `save`/`read` are stream/path-oriented (never buffer the largest configured file into memory). Only inbound bytes flow through it; **outbound** reads the workspace filesystem directly, in place, uncopied. Enforces the storage gate (single-file cap, per-session bytes budget with oldest-by-mtime deletion, executable deny-list).
_Avoid_: media service, file store (use "MediaStore" for the framework ABC and its inbound persistence contract)

**ModelCapabilities**:
A frozen value object on `LLMConfig` exposing the modalities a provider/model can natively consume — a set of `Modality` enum values (`TEXT` always present; `IMAGE` / `VIDEO` / `AUDIO` default-off, extensible). It is the switch the dormant provider-side renderer binds to: when a modality is on, matching attachments that pass the **model-facing gate** (type allow-list + strict inline size — image ≤ 20 MB, text/doc ≤ 10 MB) render into model content blocks; on model rejection they strip back to path placeholders. v1 carries it as an unused placeholder (TEXT only); nothing reads it yet. Independent of the storage gate.
_Avoid_: vision config, modality flag (use "ModelCapabilities" for the provider/model capability set)

**Attachment Locator**:
The `locator` field on an Attachment (`media` | `workspace`) — the single internal switch selecting the storage backend and download read path (`media` → `MediaStore.read`, `workspace` → filesystem read at the literal stored path). It is an internal read-dispatch detail, invisible to the frontend. It also selects path semantics: `media` paths are relative to the workspace root (a location we control); `workspace` paths are the literal absolute path the agent provided. The download + degradation contract is symmetric across both (file present → serve, file gone → fallback icon).
_Avoid_: storage type, source field (use "Attachment Locator" for the media/workspace switch)

**Agent Inbox** (or **Inbox**):
The per-session, per-pool queue that holds all turn-starting inputs between enqueue and consumption. It is the **single level source of truth** for a message's existence: a message is either pending in the inbox, folded into a running turn, or consumed — never "spawned into its own turn". Keyed by the receiver's `session_id` (unique within a pool). Holds both inter-agent messages (`message_type` = `task_request` / `subagent_result` / `agent_message`) and human/external inputs (`message_type` = `external_input`). Storage + dedup semantics (`pending.jsonl` + `delivered_ids`, FIFO, exactly-once) are defined on the `InboxServer` ABC; the inbox layer is transport, not orchestration. Each pool owns its own `InboxServer` under `<workspace_data>/inbox/<pool_name>/`. See ADR-0015 (revised).
_Avoid_: mailbox, queue (too generic), channel (overloaded)

**InboxPoller**:
The sole between-turn driver — one long-lived poller per pool, ticking every ~200ms. Each tick it enumerates sessions with pending inbox input and starts a drain task for each idle one; that task consumes a batch and dispatches **one agent turn per envelope** (`dispatch_envelope`). **Single-flight** is enforced by an `inflight: dict[session_id, asyncio.Task]` table: the poller sets the entry synchronously before scheduling the task and the task pops it in a `finally`; `reconcile_inflight()` every tick evicts any done-but-leaked entry. A session that already has a live `inflight` task is skipped (fold-in handles mid-turn). The poller also lazy-**materialize**s a subagent instance when it finds an idle+pending session with no live instance. Replaces ADR-0015's per-session Drainer / `SessionInputQueue` / `_session_gates` (see the poll-driven redesign). There is **no per-session execution lock** — mutual exclusion within a session is structural (one `inflight` task).
_Avoid_: Drainer, consumer loop, dispatch task (these name the older ADR-0015 mechanism the poller replaced)

**Fold-in**:
The mid-turn consumption path: a turn already running drains its own inbox on each iteration (`InboxFlushHook.before_iteration`) and injects new inter-agent messages into the current turn's history as `role=AGENT`, rather than starting a follow-up turn. It consumes with `only_types = {task_request, subagent_result, agent_message}` — it does **not** consume `external_input`, so a human DM always starts a fresh turn. It is the *only* mid-turn consumption path; between turns the InboxPoller's turn runner consumes all types. See ADR-0015 (revised).

**Materialize** (a subagent):
Building a subagent's agent instance lazily, on the first turn of its session, rather than when a message is sent to it. `send` mints/resolves the session id and enqueues without any instance existing; the poller's `_materialize_then_turn` builds the instance from its template on that first turn. "Cold start" is thus "materialize on first turn" — one path, not a per-direction special case. Normals are NOT materialized — they are eager-registered at boot by business wiring (Design B). See ADR-0015 (revised).

**Target resolution** (`_resolve_target`):
The send-side step that decides what kind of receiver a name is. `AgentCommunicationService._resolve_target(name)` returns `(comm_kind, template)`: `template` set when the name matches a subagent template (cold-start); otherwise the pool registry's `comm_kind` for an already-live agent. The star-topology policy (a subagent sender may target only its own parent; subagent→subagent is forbidden) is enforced inline in `_send`, not by a separate target object. (ADR-0015 D4 sketched an `AgentTarget` class hierarchy; it was never implemented — the seam is `_resolve_target` + `_send`'s policy checks.)
_Avoid_: AgentTarget (the unbuilt ADR-0015 abstraction), target descriptor, recipient (too generic)

## Relationships

- A **Workspace** owns one or more **Pool Instances**; pool instances are not shared across workspaces.
- A **Pool** is described by exactly one **PoolConfig**; multiple pools in one workspace each have their own `PoolConfig`.
- A **Pool** contains one **Main Agent** (the entry point) and zero or more subagents. Subagents are not separate pools — they share the pool's bus, broker, and tracker.
- **Assembly** turns `AppConfig` (root) into nested `PoolConfig` instances, then into `Pool` runtime objects held by a `Workspace`.
- A **ReAct Agent** runs on a **Graph**; the graph is the execution substrate, the ReAct agent is one configuration of it (4-node loop).
- A **GraphInterrupt** is raised by a `Node[R]`; the engine propagates it, the pipeline catches it for approval, and re-enters the graph after persistence.
- **Terminal Visibility** is the second upstream-visible design axis of the terminal system (alongside **Shell Family**). The OS axis does NOT exist at this level: Windows-vs-Linux is an implementation fork realised inside `TerminalBackend` subclasses. **Two invariants**: (a) the manager layer (`BaseTerminalManager`) is forked ONLY by (Shell Family, Visibility), never by OS or by capability — capabilities are folded inward as default-off flags per ADR-0010 Decision 8; (b) `_expected_state` (set by `set_expected_state(...)`, consumed by `detect_interference` on visible sessions via `TerminalTool`) and the trio `_busy_after_timeout` / `_last_status` / `_command_started_at` (updated by `apply_outcome(...)`) are **two orthogonal state slots** in `TerminalSession` and coexist under ADR-0010 Decision 7 — neither subsumes the other.
- Every inter-agent message flows through one per-pool **Agent Inbox** addressed to a receiver session. The pool's **InboxPoller** is the sole between-turn driver; **single-flight** per session is enforced by an `inflight` task table (no per-session lock). A message arriving mid-turn is **folded into** the running turn (`InboxFlushHook`, inter-agent types only), not turned into a follow-up turn; a human DM (`external_input`) is left for the next between-turn. A subagent instance is **materialized** on the first turn of its session. Both directions — `send_to_agent` (agent→agent `task_request`) and the subagent reply (`SubagentAutoSendHook` → `agent_result`) — converge on one carrier, `bus.send(session_id, envelope)`. Decided in ADR-0015 as revised by the poll-driven redesign.

## Flagged ambiguities

- "**control channel**" historically meant the runtime control plane in `modex_agent/control/`, but that package is largely **vestigial** — channels are constructed and threaded but have no live producers/consumers. Real cancellation is `asyncio.Task.cancel()` in `AgentPipeline`. Use "control channel" only when quoting the package; prefer "control plane" for the abstraction.
- "**pipeline**" was overloaded: it meant both the old `create_app`/`App` entry point (removed by ADR-0001) and the `AgentPipeline` orchestration layer that survives. "Pipeline" alone now means `AgentPipeline`; the old entry point is gone.
- "**Approval**" was historically **not wired in pool mode** (pool_builder skipped `RuntimeAssembler`; `ToolNode._get_tier()` always returned `NORMAL`). Now **implemented end-to-end** (framework → bot → webui; see **ADR-0008** + its Implementation outcome): path-tiered, default-off (opt-in per main agent), main-agents-only, one shared state machine (`apply_resume`) with per-channel surfacing. webui decisions flow as a structured `InputMessage.approval_decision` through the input pipeline (not slash commands); IM uses `/approve` text. Push + pull share one `ApprovalRequestView` DTO; pull (GET pending) is webui-only. Per **ADR-0011**: the batch is **atomic** (deny one = cancel all, including approved/exempt); webui **Deny All** short-circuits and seals the batch, **Approve** is per-request, IM stays one-at-a-time; webui surfacing is **pull-driven** by a dedicated suspend signal, and GET returns only genuinely-pending requests.
- The **session id format** is `{prefix}.{agent_name}` (dot separator, two segments) from `SessionIdFactory`. The prefix is `encode_snowflake(conversation id)` for a normal session and the verbatim `invocation_id` for a subagent session; `send_to_agent` echoes the subagent's `invocation_id` in its ack. (An older `multi_agent/AGENTS.md` draft used a colon-and-three-segment format; that text is long gone.)
- The older inter-agent mechanism had **two competing consumption paths** (`InboxFlushHook` fold-in vs eager `_handle_inbox_wakeup` poll-and-spawn-turn) and a **per-message dispatch that could be cancelled while waiting for the per-session lock, dropping messages**. ADR-0015 collapsed these onto a single-flight **Drainer**; the **poll-driven redesign** (`docs/superpowers/specs/2026-07-02-poll-driven-unified-inbox-design.md`) then replaced the Drainer / `SessionInputQueue` / `_session_gates` layers with a per-pool **InboxPoller** + `inflight` table. Fold-in remains the only mid-turn path; there is no per-session lock. (ADR-0015's D4 `AgentTarget` class was never implemented — targeting is `_resolve_target` + `_send` policy checks.)
