<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 -->

# multi_agent

## Purpose

Star-topology multi-agent orchestration. All agents live in one `AgentPool`
per pool instance; there are no per-agent execution queues and **no per-session
execution locks**. Inter-agent messaging is **poll-driven**: a single
`InboxPoller` per pool is the sole between-turn driver, and an `inflight` task
table gives single-flight semantics per session. Provides the communication
primitives (`AgentMessageBus`), the subagent
construction path (`AgentTemplate.materialize`), and framework-layer message
routing.

The messaging model is decided in **ADR-0015** (unified inbox) as revised by
the **poll-driven redesign** (InboxPoller replaced the per-session Drainer /
`SessionInputQueue` / `_session_gates` layers — see the `InboxPoller`,
`Fold-in`, and `Materialize` entries in `CONTEXT.md` for the current design).
The revised model collapses ADR-0015's Drainer / `SessionInputQueue` /
`_session_gates` layers to one poller + one inflight dict; ADR-0015's D4/D5/D7/D8
decisions (pure-router service, path resolution — now
`modex_agent/workspace/scope_path.py` after the addressing convergence —
`ContextForkBuilder`, ack paths, subagent→parent via the same bus) stand.

## How a message reaches a turn

There is exactly **one** consumer per session at a time, enforced structurally
(not by a lock):

```
writer (agent / human DM / approval / CLI modexctl send / external reply)
   └─ bus.send(session_id, envelope)            # PERSISTS + signal_wakeup() → poller Event
        ↓
   inbox[session_id]  (per-pool, on-disk, FIFO + dedup)
        ↓
InboxPoller (event-driven: Event.wait with ~interval tick fallback)
   ├─ session busy (inflight[sid] not done)?   → skip; fold-in handles mid-turn
   ├─ instance live?                           → _run_turn(sid, instance)
   └─ instance missing + template exists?      → _materialize_then_turn(sid, tmpl)
        ↓
   dispatch_envelope(sid, instance, envelope)  → pipeline.process_message
```

- **Single-flight**: `inflight: dict[session_id, asyncio.Task]` — set
  synchronously before the task is scheduled, popped in a `finally`;
  `reconcile_inflight()` every tick evicts any done-but-leaked entry.
- **Turn granularity**: between-turn dispatch is **one agent turn per envelope**
  (`_run_turn` consumes a batch then calls `dispatch_envelope` once per
  envelope). An envelope is the unit of a turn — N pending envelopes on an idle
  session become N serialized turns, not one batched turn.
- **Fold-in**: a turn already running consumes its own inbox on each iteration
  via `InboxFlushHook.before_iteration` (`only_types=AGENT_TYPES`) — a
  **batch pull** that appends each new inter-agent message to the running
  turn's history as a separate `role=SYSTEM_REMINDER` record. This is where multi-message
  batching lives (mid-turn), not between turns. It does NOT consume
  `external_input` — a human DM is a separate turn (P6).
- **Materialize-on-first-turn**: a subagent instance is built lazily by the
  poller's `_materialize_then_turn` when it finds an idle+pending session with
  no live instance. `send` never creates an instance.

## Construction — Design B (unified config, separated by layer)

| | normal (main) | subagent |
|---|---|---|
| built by | native Stage 4 / external strategy-aware registration | framework `AgentTemplate.materialize` (called by the poller) |
| tools | compiled spec tools (preset expansion + derived `task`/`send_to_peer` + supplements) | compiled spec tools (preset + derived `send_to_agent` + supplements + per-agent MCP) |
| skills | native root resolver looked up from the pool's `SkillsSupply` | native resolver looked up by compiled agent name from the same supply |
| memory | workspace memory (pool default context manager) | session-only (`build_session_only_memory`) |
| timing | eager at boot | lazy, on first turn |
| `comm_kind` | `NORMAL` (set explicitly by business) | `SUBAGENT` (set inside `materialize`) |

Normals are registered via `pool.register_resident(descriptor, instance)`.
Subagents are registered the same way at the end of `materialize`; the pool
keys instances by `agent_name`, so one instance per agent type is reused across
invocations of that type. (Invocation-specific system-prompt parts — FORK
context — are NOT baked into the instance; they are rebuilt per invocation
by pipeline providers, so reuse is safe.)

Skills construction is pool-owned: `SkillsSupply` builds the per-agent
`SkillCatalog` mapping once, while main assembly and `AgentTemplate.materialize`
only call `resolver_for(agent_name)`. An explicit `skills: false` compile veto
removes that agent's resolver; external agents carry no capabilities and never
enter native materialization.

## Key Files

| File | Description |
|------|-------------|
| `pool.py` | `AgentPool` — resident-agent registry, the poll-driven inbox surface (`submit_input`, `consume_inbox`, `sessions_with_pending`, `dispatch_envelope`, `recover_parent_session`), session/task eviction. `input_message_from_dispatch_envelope` reconstructs the full `InputMessage` (content + `approval_decision` + `attachments_resolved`) from a broker envelope. |
| `inbox_poller.py` | `InboxPoller` — the sole between-turn driver (one per pool). Event-driven via a pool-level `asyncio.Event` signalled from `LocalAgentMessageBus.send` (the single convergence point of all inbox writers), with an `interval`-cadence tick as a defensive fallback for writers that bypass the bus. Owns `inflight: dict[sid, Task]` single-flight + `reconcile_inflight`; delegates per-envelope turn execution to `pool.dispatch_envelope`. |
| `bus.py` | `AgentMessageBus` ABC + `LocalAgentMessageBus` — persist + signal the pool's `InboxPoller` via `signal_wakeup()` (in-process `Event.set`, the single convergence point for every inbox writer: user input, agent-to-agent, CLI `modexctl send`, external peer reply). `consume(only_types=)` for fold-in filtering; `sessions_with_pending()` for poller enumeration. The poller is wired post-construction via `set_poller()`; until then `send` is persist-only and the poller's tick fallback covers delivery. |
| `communication/` (package) | `AgentCommunicationService` — pure router. Strategy-dispatched (ADR-0019): `_send` resolves target → `TopologyPolicy.check` → one of three `SendStrategy` subclasses (`SubagentDispatchStrategy`, `ParentReplyStrategy`, `PeerNormalStrategy`) handles the full vertical slice (session construction, invocation_id semantics, envelope shape, delivery, result). See `communication/AGENTS.md` for the strategy contract. |
| `comm_kind.py` | `AgentCommKind` — `NORMAL` / `SUBAGENT` topology kind. |
| `tools.py` | `TaskDispatchTool` (main agent's subagent dispatch tool — strictly subagent-scoped: new task dispatch + session continuation) + `SendToPeerTool` (main agent's peer communication tool — cross-agent messaging, never task delegation; session-mode only, excluded from graph turns via `GraphToolPreset.excluded_base_tools`) + `SendToAgentTool` (subagent→parent consultation only) + `CommunicationTargetStore` (with `list_subagents()`/`list_peers()` views) + `CommunicationTarget` (carries `pool_name` + `tree_ref` for cross-pool routing per ADR-0019). All three tools converge on `AgentCommunicationService.send_async()`. |
| `template.py` | `AgentTemplate` — subagent preset + the **only** construction path (`materialize`). Builds native tools/session memory, looks up the compiled agent's `SkillResolver` from `SkillsSupply`, and wires per-invocation FORK prompt context. |
| `template_registry.py` | `AgentTemplateRegistry` — seeded per-pool subagent templates from the compiled scope declaration. |
| `materialize_deps.py` | `AgentMaterializeDeps` — regular runtime object bundling construction connections, including the pool-wide capability-supply mapping used for subagent resolver lookup. |
| `pool_instance.py` | `PoolInstance` — deployment resources for one pool, including the typed root `skill_resolver` consumed by the Bot input pipeline; it is a reference to the supply-owned catalog, not a second construction path. |
| `context_fork.py` | `ContextForkBuilder` — builds the FORK context XML from parent message history (pure computation, T18). `build()` queries the parent session's messages via `MemorySystem.get_full_history(limit=)`, returns the XML string. No fork files written to disk; no cleanup methods (file I/O removed in T17/T18). |
| `pool_router.py` | `PoolRouter` — session→pool dispatch shell (framework-level). Routes every message to the pool recorded in a `PoolRoutingStore`; agent→pool ownership is a compile-time declaration lookup (`agent_pool_ownership(spec)` — agent name → declaring pools in declaration order; a miss is an error log + drop, never a silent fallback or an all-pools scan). The path resolution half of addressing lives in `modex_agent/workspace/scope_path.py`. |
| `router.py` | `DefaultMeshRouter` — session identity resolved via `InputMessage.session` (no string parsing). |
| `envelope.py` | `AgentMessageEnvelope` — source, target, session id, agent_session_id, invocation id, message_type, payload. |
| `descriptor.py` | `AgentDescriptor`, `AgentInstance`, `AgentLLMConfig`, `ContextGovernanceConfig` — agent metadata + `comm_kind`. All are frozen Pydantic `BaseModel` (B5B). |
| `factory.py` | Agent instance factory — assembles `AgentInstance` via `create_agent()`. `DefaultAgentFactory` builds React agents with their bound skill resolver; `ExternalAwareFactory` builds the minimal external runner/pipeline without native tools, memory, hooks, or skills. |
| `execution_strategy.py` | Stateless pool-shape strategies. `ComponentRegistry`'s `EXECUTION_STRATEGY` slot is the sole registration source; service boot derives `ExecutionStrategyRegistry` from `SimpleFactory` instances. |
| `subagent_validator.py` | Framework-layer star-topology enforcement at registration. |
| `message_format.py` | Unified markdown message builder — `build_agent_comm_message` (single builder for all agent-facing message markdown, selected by `source_label` + optional `result` + optional `reply_contract`; renders the state-conditional result guidance paragraph (complete / deliverable-lost / judge / continue) after the result body), `build_dispatch_message` (convergence wrapper for subagent dispatch that never injects a reply contract — replies are auto-delivered by `SubagentAutoSendHook`; delegated to by `SubagentDispatchStrategy` only), `build_parent_reply_message` (appends the parent-reply `task` answer contract when the invocation_id is known; delegated to by `ParentReplyStrategy`), `ResultMeta` (frozen Pydantic model for hook-generated result metadata; carries `output_path` from the hook (the status enum was removed)), and `SourceLabel`/`ResultStatus` StrEnums. |
| `address.py` / `state.py` / `registry.py` | Agent addressing types (`AgentAddress` is a Pydantic `BaseModel` subclass of `Address`, B5B), state enums, registry ABC. |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `inbox/` | Inbox subsystem — `InboxMQ` ABC (`InboxServer` is a deprecated alias) + `LocalFileInboxMQ` / `InMemoryInboxServer`, `InboxProducer` / `InboxConsumer` (local-cache dedup). `DeliveredIdTracker` is deprecated (merged into `InboxMQ` internal in T11). Pure MQ: persist + atomic FIFO consume (with `only_types` filter and `sessions_with_pending`); no orchestration. SQLite adapter: `SqliteInboxMQ` (`modex_agent.persistence.adapters`). |
| `communication/` | Strategy-dispatched inter-agent messaging package (ADR-0019). `service.py` (thin orchestrator) → `TopologyPolicy.check` → `SendStrategy.execute` template method. Three concrete strategies: `SubagentDispatchStrategy` (NORMAL→SUBAGENT), `ParentReplyStrategy` (SUBAGENT→parent), `PeerNormalStrategy` (NORMAL→peer-NORMAL cross-pool via `target.tree_ref`). `result.py` holds `AgentSendResult` + `format_send_ack`. |

## Communication Contract

- `AgentCommKind.NORMAL`: one stable receiver session per conversation.
- `AgentCommKind.SUBAGENT`: task-scoped receiver sessions with `invocation_id`.
- Session id format: `{prefix}.{agent_name}` (dot separator) from
  `SessionIdFactory`. Normal: `prefix = encode_snowflake(conversation id)`, no
  `invocation_id` surfaced. Subagent: `prefix = invocation_id` verbatim, echoed
  in the ack. **Cross-pool peer** (ADR-0019): the sender's session prefix is
  reused verbatim as the receiving peer session's prefix, creating an implicit
  **session group** (A→B creates `convA.mainB`; B→A reply lands on
  `convA.mainA`). No fresh `invocation_id` is minted; the peer session is a root
  session (`parent_session_id=null`) — peer agents are equals, not parent/child.
- Three LLM-facing tools, all converging on `AgentCommunicationService.send_async`:
  - `task(target_agent, content, invocation_id?)` — the **main agent's
    work-delegation tool** (strictly subagent-scoped). Dispatches new subagent
    tasks (omit `invocation_id`), continues existing subagent sessions (pass
    `invocation_id`). Only registered for main agents when subagents exist.
    `TaskDispatchTool.list_targets()` returns only SUBAGENT targets from the
    shared store — peer targets are invisible to this tool.
  - `send_to_peer(target_peer, content)` — the **main agent's peer
    communication tool** (session-mode only). Sends a message to a peer
    agent for coordination, never for task delegation. `invocation_id` is
    always `None` (peer sessions reuse the sender's prefix, ADR-0019).
    Registered only when peers exist (`store.list_peers()` non-empty). In
    graph mode, `GraphToolPreset` excludes this tool via
    `excluded_base_tools={SEND_TO_PEER_TOOL_NAME}` so graph nodes cannot
    reach peers (they use `deliver` instead).
  - `send_to_agent(target_agent, content, invocation_id?)` — **subagent-only**
    tool for child→parent consultation. `SendToAgentTool.execute` resolves the
    target by name from `CommunicationTargetStore` and dispatches via
    `AgentCommunicationService.send_async`. The service runs `TopologyPolicy.check`
    (single star-topology enforcement point) then delegates to one of three
    `SendStrategy` subclasses based on the target's routing kind. It never creates
    an agent instance — the poller materializes lazily.
  - `invocation_id` omitted → mint a new subagent task session (cold-start; the
    poller materializes on first turn).
  - `invocation_id` concrete → continue that subagent session verbatim;
    continuation timing is notification-driven (the notification's guidance
    paragraph states whether the task is complete and what to do).
  - `target.tree_ref` set → cross-pool peer send (ADR-0019): delivers directly to
    the peer pool's tree via `target.tree_ref`; `invocation_id` is hidden from the
    sender's ack and the receiver's XML.
  - Star topology is enforced in `TopologyPolicy.check`: a subagent sender may
    only target its own parent or one of its own declared children (scope
    declaration tree edges, ticket 12); subagent→non-declared-child is rejected.
  - Tool registration is declaration-derived (SPEC §5.2): the ScopeCompiler
    injects `task` / `send_to_agent` / `send_to_peer` entries into each
    agent's compiled spec, and the TOOL-slot FW factories
    (`plugins/defaults/communication.py`) resolve them at assembly time
    reading the pool-layer `CommunicationFacilities` from the context chain.
    The legacy business-side `register_communication_tools()` call
    (`examples/bot_project/bot/service/pool/communication.py`) is gated off —
    it served the deleted roster road only.
- **The subagent reply path converges on the same tree.** `SubagentAutoSendHook`
  (`hook/builtin/`) fires on `FINALLY_GRAPH` and calls `tree.deliver(parent_sid,
  agent_result envelope)` — the same carrier as `task`/`send_to_agent`. It does not
  hand-build envelopes or call a parallel mechanism. The notification carries
  the absolute, workspace-rooted output path and ends with state-conditional
  guidance stating whether the task is complete. **Note**: this hook fires ONLY for subagent turns — peer (NORMAL) agents
  do NOT auto-notify; they must explicitly call `send_to_peer` to reply
  (ADR-0019 deferred #1). Peer agents reply via `send_to_peer`, not `task`.
- Human DM / WebUI / approval decisions enter via `pool.submit_input(session_id,
  InputMessage)`, which serializes the full `InputMessage` (via
  `BrokerInputPayload`, carrying `approval_decision` + `attachments_resolved`)
  into an `external_input` envelope on the pool's inbox. `PoolRouter` calls this
  directly — DMs no longer go through `broker.send_to`.

The old `send_message`, `send_message_async`, and `dispatch_task` tools are
removed. Do not add compatibility wrappers.

## Per-pool isolation

Each pool owns its own `InboxMQ` (own storage dir
`<workspace_data>/inbox/<pool_name>/`), `LocalAgentMessageBus`, and
`InboxPoller`. The `MessageBroker` stays workspace/bot-level for cross-pool
peer routing (ADR-0019 `tree_ref`); it no longer carries an `_inbox_wakeup`
signal — between-turn wakeup is now an in-process `asyncio.Event` on the
poller, signalled from `LocalAgentMessageBus.send`.
`session_id` is unique within a pool, so the inbox keys by bare
`session_id`; `(workspace, pool)` isolation is structural (a pool belongs to
exactly one workspace).

**Cross-pool exception (ADR-0019):** the framework has an *optional* routing
primitive — `CommunicationTarget.tree_ref` (a `SessionTreeManager | None` field).
Peer links are declared in the scope declaration (a pool's `peers` list) and
resolved by the framework at workspace materialize time
(`communication/peer_resolution.py`: `peer_links_from_declaration` extracts
the links, `resolve_peer_targets` reads the peer pool's tree reference from
the same workspace resource bundle and populates each root's per-agent
`CommunicationTargetStore`). `PeerNormalStrategy.deliver` reads `tree_ref` and
calls `tree.deliver()` on it directly — this is the ONLY cross-pool messaging
path. The framework itself has no "peer pool" concept; it only sees "the target
carries an optional tree reference; deliver there if set, else local tree".
With no peer links declared, behaviour is byte-for-byte identical to
single-pool operation. v1 restricts links to the same workspace (V5) — both
endpoints evict with the bundle as one unit, so a dangling cross-workspace
reference is not constructible.

## For AI Agents

- All multi-agent modes use `AgentPool` with resident agents; there is no
  queue-per-agent model and no per-session execution lock. Mutual exclusion
  within a session is structural — one `inflight` task per session.
- Subagents are built lazily by the poller on first turn and reused by
  `agent_name`; invocation-specific prompt parts rebuild per session.
- `InboxFlushHook` is the fold-in path (mid-turn, `role=AGENT`); it consumes
  inter-agent types only, so a human DM always starts a fresh turn.
- `SubagentAutoSendHook` always fires on `FINALLY_GRAPH` and notifies the parent
  via the same bus — it is the sole reply path (subagents have no comm tool).
- Star topology is enforced both in `_send` (subagent→parent only) and by
  `subagent_validator.py` at registration.

## Dependencies

- `modex_agent.core.agent` — `Agent[E]`, `ContentEmitter[E]`
- `modex_agent.runtime` — `AgentRuntime`, `TurnStateStore`
- `modex_agent.pipeline` — `AgentPipeline` for execution orchestration
- `modex_agent.messaging` — `MessageBroker` (cross-process wakeup only)
- `modex_agent.hook` — `InboxFlushHook`, `SubagentAutoSendHook`, `HookRunner`
