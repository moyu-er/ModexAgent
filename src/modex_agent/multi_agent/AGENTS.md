<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-02 -->

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
decisions (pure-router service, `WorkspacePathResolver`, `ContextForkBuilder`,
ack paths, subagent→parent via the same bus) stand.

## How a message reaches a turn

There is exactly **one** consumer per session at a time, enforced structurally
(not by a lock):

```
writer (agent / human DM / approval)
   └─ bus.send(session_id, envelope)            # PERSISTS only — no in-process wakeup
        ↓
   inbox[session_id]  (per-pool, on-disk, FIFO + dedup)
        ↓
InboxPoller tick (~200ms, sole between-turn driver)
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
  turn's history as a separate `role=AGENT` record. This is where multi-message
  batching lives (mid-turn), not between turns. It does NOT consume
  `external_input` — a human DM is a separate turn (P6).
- **Materialize-on-first-turn**: a subagent instance is built lazily by the
  poller's `_materialize_then_turn` when it finds an idle+pending session with
  no live instance. `send` never creates an instance.

## Construction — Design B (unified config, separated by layer)

| | normal (main) | subagent |
|---|---|---|
| built by | business `_register_main_agent` (`pool_builder`) | framework `AgentTemplate.materialize` (called by the poller) |
| tools | factory defaults (`create_pool`'s `_build_tools` + `send_to_agent` + terminal + `extra_tools`) | template-built (preset + per-agent MCP) |
| memory | workspace memory (pool default context manager) | session-only (`build_session_only_memory`) |
| timing | eager at boot | lazy, on first turn |
| `comm_kind` | `NORMAL` (set explicitly by business) | `SUBAGENT` (set inside `materialize`) |

Normals are registered via `pool.register_resident(descriptor, instance)`.
Subagents are registered the same way at the end of `materialize`; the pool
keys instances by `agent_name`, so one instance per agent type is reused across
invocations of that type. (Invocation-specific system-prompt parts — APPEND
parent prompt, FORK context — are NOT baked into the instance; they are rebuilt
per invocation by pipeline providers, so reuse is safe.)

## Key Files

| File | Description |
|------|-------------|
| `pool.py` | `AgentPool` — resident-agent registry, the poll-driven inbox surface (`submit_input`, `consume_inbox`, `sessions_with_pending`, `dispatch_envelope`, `recover_parent_session`), session/task eviction. `input_message_from_dispatch_envelope` reconstructs the full `InputMessage` (content + `approval_decision` + `attachments_resolved`) from a broker envelope. |
| `inbox_poller.py` | `InboxPoller` — the sole between-turn driver (one per pool, ~200ms tick). Owns `inflight: dict[sid, Task]` single-flight + `reconcile_inflight`; delegates per-envelope turn execution to `pool.dispatch_envelope`. |
| `bus.py` | `AgentMessageBus` ABC + `LocalAgentMessageBus` — persist + (cross-process only) broker `_inbox_wakeup`. `consume(only_types=)` for fold-in filtering; `sessions_with_pending()` for poller enumeration. No in-process signal/event. |
| `communication/` (package) | `AgentCommunicationService` — pure router. Strategy-dispatched (ADR-0019): `_send` resolves target → `TopologyPolicy.check` → one of three `SendStrategy` subclasses (`SubagentDispatchStrategy`, `ParentReplyStrategy`, `PeerNormalStrategy`) handles the full vertical slice (session construction, invocation_id semantics, envelope shape, delivery, result). See `communication/AGENTS.md` for the strategy contract. |
| `comm_kind.py` | `AgentCommKind` — `NORMAL` / `SUBAGENT` topology kind. |
| `tools.py` | `SendToAgentTool` (the single LLM-facing comm tool), `CommunicationTargetStore`, `CommunicationTarget` (carries `pool_name` + `bus_ref` for cross-pool routing per ADR-0019). |
| `template.py` | `AgentTemplate` — subagent preset + the **only** construction path (`materialize`). Builds the tool manager, session-only memory, subagent hooks; wires per-invocation APPEND/FORK prompt providers. |
| `template_registry.py` | `AgentTemplateRegistry` — scans/loads per-pool subagent templates (`config/pools/<pool>/templates/*.yml`). |
| `materialize_deps.py` | `AgentMaterializeDeps` — frozen value object of construction deps (factory, broker, pool, path resolver, fork builder, …); replaces ~30 scattered ctor params. |
| `context_fork.py` | `ContextForkBuilder` — builds the FORK context XML from parent message history (pure computation, T18). `build()` queries the parent session's `MessageStore`, applies lossy compaction, returns the XML string. No fork files written to disk; `register_for_cleanup`/`cleanup` are retained as no-ops for caller compatibility. |
| `workspace_paths.py` | `WorkspacePathResolver` — resolves `runtime_dir` / `memory_dir` / `output_path(session_id)` / `trace_dir(session_id)` / `pruned_manager` from the active workspace's pool_data. |
| `router.py` | `DefaultMeshRouter` — session identity resolved via `InputMessage.session` (no string parsing). |
| `envelope.py` | `AgentMessageEnvelope` — source, target, session id, agent_session_id, invocation id, message_type, payload. |
| `descriptor.py` | `AgentDescriptor`, `AgentInstance`, `AgentLLMConfig` — agent metadata + `comm_kind`. |
| `factory.py` | Agent instance factory — assembles `AgentInstance` via `create_agent()`. `DefaultAgentFactory` builds react agents (provider + tools + skill + TurnContextBuilder + ApprovalResumer/ApprovalRenderer + ReActTurnRunner + hooks + pipeline). `ExternalCodingAwareFactory` (in `examples/bot_project/bot/service/external_coding_strategy.py`) overrides `create_agent` to build only 6 objects (ExternalCodingAgent + broker I/O + emitter + ExternalTurnRunner + pipeline, no hooks/provider/tools) — external_coding pools boot without `model.yml`. `_get_builder` dispatch (runtime agent-construction, not assembly branching) is retained per ADR-0025 D5 deviations. |
| `subagent_validator.py` | Framework-layer star-topology enforcement at registration. |
| `address.py` / `message_xml.py` / `state.py` / `registry.py` | Agent addressing dataclasses, agent-message XML, state enums, registry ABC. |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `inbox/` | Inbox subsystem — `InboxMQ` ABC (`InboxServer` is a deprecated alias) + `LocalFileInboxMQ` / `InMemoryInboxServer`, `InboxProducer` / `InboxConsumer` (local-cache dedup). `DeliveredIdTracker` is deprecated (merged into `InboxMQ` internal in T11). Pure MQ: persist + atomic FIFO consume (with `only_types` filter and `sessions_with_pending`); no orchestration. SQLite adapter: `SqliteInboxMQ` (`modex_agent.persistence.adapters`). |
| `communication/` | Strategy-dispatched inter-agent messaging package (ADR-0019). `service.py` (thin orchestrator) → `TopologyPolicy.check` → `SendStrategy.execute` template method. Three concrete strategies: `SubagentDispatchStrategy` (NORMAL→SUBAGENT), `ParentReplyStrategy` (SUBAGENT→parent), `PeerNormalStrategy` (NORMAL→peer-NORMAL cross-pool via `target.bus_ref`). `result.py` holds `AgentSendResult` + `format_send_ack`. |

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
- `send_to_agent(target_agent, content, invocation_id?)` is the single
  LLM-facing comm tool. `SendToAgentTool.execute` resolves the target by name
  from `CommunicationTargetStore` and dispatches via
  `AgentCommunicationService.send_async`. The service runs `TopologyPolicy.check`
  (single star-topology enforcement point) then delegates to one of three
  `SendStrategy` subclasses based on the target's routing kind. It never creates
  an agent instance — the poller materializes lazily.
  - `invocation_id` empty → mint a new subagent task session (cold-start; the
    poller materializes on first turn).
  - `invocation_id` concrete → continue that subagent session verbatim.
  - `target.bus_ref` set → cross-pool peer send (ADR-0019): delivers directly to
    the peer pool's `AgentMessageBus`; `invocation_id` is hidden from the
    sender's ack and the receiver's XML.
  - Star topology is enforced in `TopologyPolicy.check`: a subagent sender may
    only target its own parent; subagent→subagent is rejected.
- **The subagent reply path converges on the same bus.** `SubagentAutoSendHook`
  (`hook/builtin/`) fires on `FINALLY_TURN` and calls `bus.send(parent_sid,
  agent_result envelope)` — the same carrier as `send_to_agent`. It does not
  hand-build envelopes or call a parallel mechanism. The notification carries
  absolute, workspace-rooted trace/output paths (parity with the `send_to_agent`
  ack). **Note**: this hook fires ONLY for subagent turns — peer (NORMAL) agents
  do NOT auto-notify; they must explicitly call `send_to_agent` to reply
  (ADR-0019 deferred #1).
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
`InboxPoller`. The `MessageBroker` stays workspace/bot-level as the
cross-process `_inbox_wakeup` fallback (single-process deployments are
poller-only). `session_id` is unique within a pool, so the inbox keys by bare
`session_id`; `(workspace, pool)` isolation is structural (a pool belongs to
exactly one workspace).

**Cross-pool exception (ADR-0019):** the framework gains an *optional* routing
primitive — `CommunicationTarget.bus_ref` (an `AgentMessageBus | None` field).
When the business layer wires peer pools, it populates each pool's
`CommunicationTargetStore` with peer main-agent entries whose `bus_ref` points
at the peer pool's bus. `PeerNormalStrategy.deliver` reads `bus_ref` and calls
`bus.send()` on it directly — this is the ONLY cross-pool messaging path. The
framework itself has no "peer pool" concept; it only sees "the target carries an
optional bus reference; deliver there if set, else local bus". With no peer
wiring configured, behaviour is byte-for-byte identical to single-pool
operation. Peer wiring is performed in a post-assembly phase (Phase 2) by the
business layer (`examples/bot_project/bot/workspace/wiring.py`), after all pools
are built.

## For AI Agents

- All multi-agent modes use `AgentPool` with resident agents; there is no
  queue-per-agent model and no per-session execution lock. Mutual exclusion
  within a session is structural — one `inflight` task per session.
- Subagents are built lazily by the poller on first turn and reused by
  `agent_name`; invocation-specific prompt parts rebuild per session.
- `InboxFlushHook` is the fold-in path (mid-turn, `role=AGENT`); it consumes
  inter-agent types only, so a human DM always starts a fresh turn.
- `SubagentAutoSendHook` always fires on `FINALLY_TURN` and notifies the parent
  via the same bus — it is the sole reply path (subagents have no comm tool).
- Star topology is enforced both in `_send` (subagent→parent only) and by
  `subagent_validator.py` at registration.

## Dependencies

- `modex_agent.core.agent` — `Agent[E]`, `ContentEmitter[E]`
- `modex_agent.runtime` — `AgentRuntime`, `TurnStateStore`
- `modex_agent.pipeline` — `AgentPipeline` for execution orchestration
- `modex_agent.messaging` — `MessageBroker` (cross-process wakeup only)
- `modex_agent.hook` — `InboxFlushHook`, `SubagentAutoSendHook`, `HookRunner`
