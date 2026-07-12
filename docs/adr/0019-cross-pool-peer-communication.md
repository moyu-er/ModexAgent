# Cross-pool peer communication (optional framework capability)

Status: accepted (2026-07-12) — implemented in commit a5a78407, all tests green

## Context

ADR-0015 defined the inter-agent messaging model for **one pool**: a star
topology where a main (NORMAL) agent dispatches to subagents (SUBAGENT), and
subagents reply to their parent. Pools are fully isolated — each owns its bus,
inbox, and poller. There is no path for a main agent in pool A to send a message
to a main agent in pool B.

The bot reference project (`examples/bot_project/`) deploys multiple pools
(`main`, `coding`, `research`, …) per workspace. A natural requirement is that
the main agents of these pools communicate as peers — asking questions, handing
off work, exchanging decisions — without losing the per-pool isolation that
makes each pool's inbox/memory/turn-execution independent.

The design constraint is strict: **the framework must not gain a dependency on
peer-pool topology**. Cross-pool communication is an opt-in capability that the
business layer wires; with no wiring, behaviour is byte-for-byte today's.

## Decision

Three pieces, each strictly layered:

### 1. Framework gains a capability, not a concept

The framework does **not** learn the word "peer". It gains:

- A `pool_name: str` field and a `bus_ref: AgentMessageBus | None` field on the
  existing `CommunicationTarget` dataclass (`multi_agent/tools.py`). `bus_ref`
  is `None` for local targets — the default — and a direct object reference to
  another pool's bus for cross-pool targets.
- A `PeerNormalStrategy` (one of three `SendStrategy` subclasses, see §2) whose
  `deliver()` method reads `target.bus_ref` and calls `bus.send()` on it when
  set, falling back to the local bus when `None`. The strategy does not know it
  is doing "cross-pool" routing — it is delivering to "the bus the target
  points at".
- A uniqueness invariant in `CommunicationTargetStore.add`: duplicate `name`
  across all reachable pools is rejected at registration. This is the load-bearing
  correctness property — see §"Why store-lookup routing".

No new ABC, no new transport, no new config field, no new `AgentCommKind`. The
default state (`bus_ref=None` everywhere) produces today's behaviour exactly.

### 2. `communication.py` is refactored into a strategy-dispatched service

The current `AgentCommunicationService._send` (172 lines, three implicit
strategies tangled in `if target_kind == SUBAGENT / else` branches) is
refactored. The file `communication.py` becomes a package:

```
multi_agent/communication/
├── __init__.py          # public re-exports
├── service.py           # AgentCommunicationService — thin orchestrator (~80 lines)
├── topology.py          # TopologyPolicy — the single policy gate
├── result.py            # AgentSendResult + ack formatting
└── strategies/
    ├── __init__.py
    ├── base.py          # SendStrategy ABC
    ├── subagent_dispatch.py   # NORMAL→SUBAGENT (TASK_REQUEST, same pool)
    ├── parent_reply.py        # SUBAGENT→NORMAL parent (AGENT_MESSAGE, same pool)
    └── peer_normal.py         # NORMAL→peer-NORMAL (AGENT_MESSAGE, cross-pool)
```

`_send` becomes:

```
1. resolve target from CommunicationTargetStore (store-lookup, not two registries)
2. TopologyPolicy.check(sender, target) — single enforcement point
3. strategy = strategies[target routing kind]
4. return strategy.execute(req)
```

The MCP loader `_load_per_agent_mcp` (currently a 125-line function sitting at
the top of `communication.py`, called only from `template.py:361`) moves to
`tools/mcp_loader.py` — it has nothing to do with routing.

### 3. The business layer declares and wires peers

Peer relationship is declared in each pool's `pool.yml`:

```yaml
# pools/main/pool.yml (business config, NOT framework PoolConfig)
peers: [coding, research]
```

The framework's `ioc/configs/pool.py::PoolConfig` is **unchanged**. The `peers`
field lives on the business-layer `PoolTree` / `MainAgentNode` structures in
`bot/config/pool_store.py`.

Peer wiring happens in a **post-assembly phase** (Phase 2), after every pool's
`create_pool` has completed:

```
Phase 1 (per-pool create_pool):
  - Each pool's CommunicationTargetStore is populated with its own subagent
    targets (insertion order: subagents first).

Phase 2 (post-assembly, all pools built):
  - Business assembly iterates each pool's peers list.
  - For each peer pool name, looks up the peer PoolInstance, reads its
    main_agent_name + agent_bus.
  - Constructs CommunicationTarget(name=peer_main_name, kind=NORMAL,
    pool_name=peer_pool, bus_ref=peer_agent_bus).
  - Calls target_store.add(target) on the local pool.
```

The "subagent targets first, peer main targets second" ordering is an invariant
of Phase 2 — enforced by assembly ordering, not by a priority field.

Peer relationship is **bidirectional by invariant**: the frontend and the
`PoolStore` validator enforce that adding A→B also adds B→A, and removing one
side removes the other. A stale half-edge is a configuration error.

## Considered Options (rejected)

### Rejected: `PeerPoolRouter` ABC as a framework port

An earlier iteration proposed an ABC `PeerPoolRouter` with `resolve_peer_target`
and `deliver_to_peer` methods, injected as `peer_router: PeerPoolRouter | None =
None` into `AgentCommunicationService`.

Rejected because once `CommunicationTarget` carries `bus_ref`, the ABC
re-expresses data the store already holds. Two sources of truth (store for
display, router for dispatch) would drift. The store-as-routing-source design
collapses both into one.

### Rejected: pair-scoped session prefix

To avoid "context pollution" in A→B→C chains, an iteration proposed minting a
stable pair prefix per `(sender_pool, receiver_pool)` pair, maintained by a
workspace-level `PairPrefixRegistry`.

Rejected because it breaks bidirectional continuity: C→A's reply would land on
a session with no relationship to A's user session. The user explicitly chose
the session-group model: shared prefix as designed behaviour, accepting that
peer communication context propagates within the group (analogous to people in
a room hearing each other). This is a documented v1 trade-off, not a defect.

### Rejected: per-message fresh invocation_id

Minting a fresh invocation_id per peer send (like subagent dispatch) would
isolate each exchange but loses reply routing entirely: C→A would have no way
to find A's canonical session. Rejected for the same reason as pair-scoped
prefix — reply continuity requires shared session identity.

### Rejected: new `AgentCommKind.PEER` value

Adding a `PEER` kind to the `AgentCommKind` enum would overload the enum's
meaning: it currently describes an agent's **topology role** (main vs
subagent), not a **message routing path**. A peer pool's main agent is still a
NORMAL agent. Rejected to keep the enum's semantics clean.

## Consequences

### Positive

- **Framework stays clean.** Zero new concepts; one new optional field on an
  existing dataclass; one new strategy in a refactored dispatch. Default
  behaviour unchanged.
- **Single routing source of truth.** The store holds topology (display),
  routing (`bus_ref`), and identity (`pool_name`) in one place. The LLM sees
  it, the tool sees it, the service sees it.
- **Name uniqueness enforced.** The "two pools both have a `main` agent"
  collision is caught at Phase 2 registration, not at first send.
- **`communication.py` gets the refactor it has long needed.** The 172-line
  `_send` method with three tangled branches becomes three strategy classes,
  each owning its full vertical slice (session construction, invocation_id
  semantics, envelope shape, tracker interaction, delivery, result).

### Negative

- **Session-group context propagation is v1 semantics.** Agent A communicating
  with B then C will see B↔C context when later talking to C directly. This is
  accepted as designed behaviour (multi-party room model), but users who expect
  pair-isolated conversations will be surprised. Documented in CONTEXT.md.
- **No auto-receipt in v1.** When peer agent C finishes a turn triggered by A's
  message, it does not automatically notify A. C must explicitly call
  `send_to_agent("main", reply)`. This is a real usability limitation.
- **One session per (prefix, agent) pair.** v1 does not support multiple
  parallel conversations between the same pair of agents. A second conversation
  between A and C reuses the existing `convA.mainC` session.

### Deferred to later revisions

The following are explicitly out of scope for v1 and will be designed separately
when needed:

1. **Generalized `AutoSendHook` (NORMAL peer reply).** The current
   `SubagentAutoSendHook` fires only on `comm_kind=SUBAGENT`. Extending it to
   fire on NORMAL peer contexts (auto-forwarding a result to the originating
   peer) requires knowing *which* peer triggered the turn — which requires a
   per-turn routing signal that v1 does not have. Blocked on item 2.
2. **Communication-log artefact.** A per-session structured record of
   "who-said-what / who-triggered-this-turn", persisted and available to the
   hook for routing and to the agent for multi-peer conversation recall. This
   is what unblocks the generalized hook, per-peer context isolation, and a
   "call history" view in the UI.
3. **Per-pair context fork.** Adapting `ContextForkBuilder` (currently
   subagent-only) to fork a slice of A's user-session context into the
   `convA.mainC` peer session, so A can answer C with user-conversation
   awareness without polluting the user session.
4. **Multiple parallel sessions per agent pair.** v1's "one session per
   (prefix, agent)" constraint would be lifted by allowing the sender to mint
   an additional prefix segment, analogous to subagent invocation_id.
