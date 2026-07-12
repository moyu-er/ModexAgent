# Cross-pool Peer Communication — Optional Framework Capability

Status: ready-for-agent

Related: ADR-0019 (`docs/adr/0019-cross-pool-peer-communication.md`), ADR-0019
implementation blueprint
(`docs/adr/0019-cross-pool-peer-communication-blueprint.md`);
`CONTEXT.md` → "Communication Target", "Peer Pool", "Session Group";
ADR-0015 (unified inbox, partially superseded by store-lookup routing);
`examples/bot_project/CONTEXT.md` → "Session Record".

## Problem Statement

A ModexAgent deployment typically runs multiple pools in one workspace (e.g. a
`main` pool, a `coding` pool, a `research` pool). Each pool's main agent is an
independent specialist with its own tools, memory, and skills. Today these main
agents are completely isolated from each other — there is no way for the `main`
agent to ask the `coding` pool's main agent a question, hand off a task, or
exchange a decision. The only communication path that exists is the star
topology *within* a single pool: a main agent dispatches to its own subagents,
and subagents reply to their parent.

A user who wants their `main` agent to consult the `coding` agent on a design
question, or have the `research` agent hand a finding to the `coding` agent,
has no mechanism to do so. The agents cannot collaborate across pool
boundaries, even though they coexist in the same workspace and serve the same
user.

## Solution

Introduce **cross-pool peer communication** as an **optional framework
capability** — not a new assembly mode, not a new topology concept. The
framework gains the *ability* for a `CommunicationTarget` to carry a direct
reference to another pool's `AgentMessageBus`; the business layer declares
peer relationships in pool configuration and wires them during a
post-assembly phase. When no peers are configured, behaviour is byte-for-byte
today's.

From the user's perspective: each pool's main agent can be configured with a
list of peer pools it may communicate with. Once configured, the main agent's
`send_to_agent` tool lists peer main agents alongside its own subagents, and
the agent can message them exactly as it messages a subagent — asynchronously,
via inbox delivery, with the peer agent's poller picking up the message and
running a turn. Peer agents are equals: there is no parent-child relationship,
no invocation_id surfaced to the sending agent, and the receiving agent sees
the message as a normal inbox-delivered turn.

The communication adopts a deliberate **Session Group** semantic: when agent A
(session `convA.mainA`) sends to peer agent C, C's receiving session is
`convA.mainC` — the sender's prefix is reused. C's reply routes back to
`convA.mainA`. Context therefore propagates within the session group as a
designed property, not a defect — like multiple people in a room hearing each
other.

This is a v1: the framework is refactored to support the capability, the
business layer wires it, and the deferred items (auto-receipt notifications,
communication-log artefact, per-pair context isolation, multiple parallel
sessions per agent pair) are explicitly out of scope.

## User Stories

### Framework capability

1. As a framework consumer, I want `CommunicationTarget` to carry an optional
   `bus_ref` field pointing at another pool's `AgentMessageBus`, so that
   delivery can route to a non-local bus when the field is set.

2. As a framework consumer, I want `CommunicationTarget` to carry a `pool_name`
   field identifying the owning pool, so that target metadata is complete for
   diagnostics and logging.

3. As a framework consumer, I want `CommunicationTargetStore.add` to reject
   duplicate target names across all reachable pools at registration time, so
   that a name collision (two pools both having a `main` agent) is caught at
   wiring time, not at first send.

4. As a framework consumer, I want `bus_ref=None` to mean "route locally to
   this pool's own bus", so that the default state (no peer wiring) produces
   byte-for-byte today's behaviour.

5. As a framework consumer, I want the framework to have no concept of "peer
   pool", so that the capability is a pure data-driven routing extension with
   no new topology knowledge in the framework core.

6. As a framework consumer, I want `AgentCommKind` to remain unchanged
   (`NORMAL` / `SUBAGENT` only), so that the enum's semantics (an agent's
   topology role) is not overloaded with routing-path information.

### Framework refactor — communication.py

7. As a framework maintainer, I want `communication.py` refactored from a
   610-line monolith with a 172-line `_send` method into a strategy-dispatched
   package, so that each routing topology (subagent dispatch, parent reply,
   peer-normal) is an independently testable strategy class owning its full
   vertical slice.

8. As a framework maintainer, I want `SendStrategy` to be an ABC with
   `build_session`, `normalize_invocation_id`, `build_envelope`,
   `apply_tracker`, `deliver`, and `execute` methods, so that adding a future
   routing topology means adding a strategy class and a dispatch branch — not
   refactoring a god-method.

9. As a framework maintainer, I want `TopologyPolicy.check` to be the single
   enforcement point for star-topology rules, so that the send trunk never
   re-checks topology and the policy is testable in isolation.

10. As a framework maintainer, I want `_load_per_agent_mcp` moved out of
    `communication.py` into the tools package, so that the routing file no
    longer carries 125 lines of MCP server loading code unrelated to routing.

11. As a framework maintainer, I want the existing `subagent_dispatch` and
    `parent_reply` strategies to preserve today's behaviour byte-for-byte, so
    that the refactor is a no-op for existing callers.

12. As a framework maintainer, I want the `AgentCommunicationService.__init__`
    signature to remain backward-compatible (new dependencies optional with
    `None` defaults), so that existing wiring code does not break.

### Framework — peer-normal strategy

13. As a peer-sending main agent, I want my `send_to_agent` call to deliver the
    message to the peer pool's bus (not my own), so that the peer pool's
    `InboxPoller` picks it up and starts a turn.

14. As a peer-sending main agent, I want the receiving session in the peer pool
    to be constructed with my session's prefix verbatim (`create_with_prefix`),
    so that a reply from the peer agent routes back to my session via the same
    prefix.

15. As a peer-sending main agent, I want the receiving peer session to be a
    **root session** (`parent_session_id=null`), so that peer agents are equals
    — no parent-child relationship, no cascade-deletion coupling.

16. As a peer-sending main agent, I want no `invocation_id` to appear in my
    tool ack or in the XML the peer agent receives, so that I am never aware
    of the invocation_id mechanism for peer communication.

17. As a peer-sending main agent, I want my `invocation_id` (my session prefix)
    to be used internally for envelope bookkeeping and tracker recording, so
    that the communication is observable in the tracker without surfacing the
    id to the LLM.

18. As a peer-sending main agent, I want the peer-normal strategy to fall back
    to the local bus when `target.bus_ref` is `None`, so that a misconfigured
    target degrades to local delivery rather than crashing.

19. As a framework consumer, I want the peer-normal strategy to use
    `AGENT_MESSAGE` as the message type (same as parent-reply), so that the
    `InboxFlushHook` fold-in path naturally consumes peer messages mid-turn.

### Framework — receiving side

20. As a peer-receiving pool's `InboxPoller`, I want to handle a session id
    that appears in my inbox without a prior registry entry, so that the
    peer-normal first-turn case (session created by sender, not yet registered
    locally) works without special configuration.

21. As a peer-receiving pool's `InboxPoller`, I want to register the unseen
    session id in my local `SessionRegistry` on first encounter, so that
    subsequent dispatch lookups succeed.

22. As a peer-receiving pool's `InboxPoller`, I want the main agent instance
    to already exist (eager-registered at boot), so that no materialization is
    needed — only the session record is new.

### Framework — tool surface

23. As a main agent's LLM, I want `send_to_agent` to accept a peer agent's
    name exactly as it accepts a subagent's name, so that I use the same tool
    call for both intra-pool and cross-pool communication.

24. As a main agent's LLM, I want peer agents to appear in the
    `Available targets` list of the tool description, so that I know which
    peer agents I can message.

25. As a main agent's LLM, I want peer agents to appear in the tool's
    `target_agent` enum, so that the schema constrains me to valid targets.

26. As a main agent's LLM, I want peer main-agent targets to appear *after*
    my own subagent targets in the description list, so that my closer
    collaborators (subagents) are visually prioritized.

### Business layer — configuration

27. As a bot operator, I want to declare peer relationships in each pool's
    `pool.yml` via a top-level `peers` key listing peer pool directory names,
    so that I can configure cross-pool communication without editing a
    separate file.

28. As a bot operator, I want `peers: [coding, research]` in `main/pool.yml`
    to mean "this pool's main agent can send to the `coding` and `research`
    pools' main agents", so that the configuration is readable and obvious.

29. As a bot operator, I want the peer relationship to be bidirectional by
    invariant — declaring B as a peer of A must also declare A as a peer of B
    — so that half-edges are impossible.

30. As a bot operator, I want `PoolStore` to validate that every peer name in
    a pool's `peers` list (a) exists as a pool directory and (b) reciprocally
    declares this pool in its own `peers` list, so that hand-edited YAML
    errors are caught at config load, not at boot.

31. As a bot operator, I want the framework's `PoolConfig`
    (`ioc/configs/pool.py`) to remain unchanged, so that the peer
    configuration is purely a business-layer concern and the framework has no
    dependency on it.

32. As a bot operator, I want `PoolTree` (the business-layer config structure)
    to carry the `peers` field, so that the peer list is available at
    assembly time.

### Business layer — assembly

33. As a bot assembly phase, I want peer target population to run as a
    **Phase 2 post-assembly step** (after every pool's `create_pool` has
    completed), so that all peer pool bus references are available for
    cross-pool wiring.

34. As a bot assembly phase, I want to read each pool's `peers` list, look up
    each peer pool's `PoolInstance` for its `main_agent_name` and `agent_bus`,
    and construct a `CommunicationTarget(name=peer_main_name, kind=NORMAL,
    pool_name=peer_pool, bus_ref=peer_agent_bus)` entry in the local pool's
    `CommunicationTargetStore`, so that the framework's store-as-routing-source
    design is fully populated.

35. As a bot assembly phase, I want subagent targets (added in Phase 1 by
    `_build_communication`) to remain ahead of peer main-agent targets (added
    in Phase 2) in insertion order, so that the "subagents first, peers
    second" display priority invariant holds.

36. As a bot assembly phase, I want `PoolInstance` to expose `agent_bus` and
    `target_store` as readable fields, so that Phase 2 wiring can access them.

### Business layer — frontend

37. As a WebUI user, I want to add a peer relationship between pool A and pool
    B with a single action that adds both sides simultaneously, so that I
    cannot accidentally create a half-edge.

38. As a WebUI user, I want removing a peer relationship to remove both sides
    simultaneously, so that bidirectionality is always maintained.

39. As a WebUI user, I want the peer configuration UI to show the peer pool's
    main agent name (resolved from the peer's `main_agent_name`), so that I
    understand what agent name my agent will see.

### Observability and correctness

40. As a framework consumer, I want the `CommunicationTracker` to record a
    peer-normal send via `record_send` (not `acknowledge`), so that the
    sideband send/ack bracket matching is not corrupted by peer messages that
    have no bracket semantics in v1.

41. As a framework consumer, I want a peer-normal send that targets a name not
    in the store to return a clear "not a valid communication target" error,
    so that the LLM is told it picked a wrong name rather than getting a
    silent failure.

## Implementation Decisions

### Architectural layering (strict)

The framework (`src/modex_agent/`) gains a **capability** (optional fields +
a new strategy), not a **concept** (no new "peer pool" type, no new config
field on framework `PoolConfig`, no new `AgentCommKind`). The business layer
(`examples/bot_project/`) declares peer relationships and wires them. This
separation is a hard invariant: the framework must not import, parse, or
depend on any business-layer peer configuration.

### Framework changes

**`CommunicationTarget` dataclass extension.** Two new fields with defaults
that preserve backward compatibility:

```python
@dataclass(frozen=True)
class CommunicationTarget:
    name: str
    kind: AgentCommKind
    description: str = ""
    pool_name: str = ""                        # owning pool (local or peer)
    bus_ref: "AgentMessageBus | None" = None   # direct bus ref; None = local
```

The `pool_name` default is `""` (not `None`) so existing constructors that
omit it describe local-pool targets implicitly. `bus_ref` is the
load-bearing field: `None` means local routing (today's behaviour), a set
reference means cross-pool routing.

**Store uniqueness invariant.** `CommunicationTargetStore.add` rejects
duplicate names:

```python
if target.name in self._targets:
    existing = self._targets[target.name]
    raise ValueError(
        f"Duplicate communication target name {target.name!r}: "
        f"existing pool={existing.pool_name!r}, new pool={target.pool_name!r}. "
        "Target names MUST be unique across all reachable pools."
    )
```

This replaces the current silent-drop-on-duplicate. The invariant is the
correctness foundation for store-lookup routing: if two targets shared a name,
the LLM's `target_agent` parameter would be ambiguous.

**Store gains a `get(name) -> CommunicationTarget | None` method.** The tool
and the service look up targets by name via this method; the store is the
single routing source of truth.

**`communication.py` → `communication/` package.** The monolith is refactored
into:

- `service.py` — thin orchestrator: topology check → strategy dispatch.
- `topology.py` — `TopologyPolicy.check`, the single enforcement point.
- `result.py` — `AgentSendResult` + ack text formatting.
- `strategies/base.py` — `SendStrategy` ABC + `SendRequest` / `SendDeps`
  bundles.
- `strategies/subagent_dispatch.py` — NORMAL→SUBAGENT (extracted from
  current `_send` lines 484-532).
- `strategies/parent_reply.py` — SUBAGENT→NORMAL parent (extracted from
  current `_send` lines 542-610, subagent branch).
- `strategies/peer_normal.py` — NORMAL→peer-NORMAL (new).

`__init__.py` re-exports `AgentCommunicationService` and `AgentSendResult` so
every existing import (`from modex_agent.multi_agent.communication import
AgentCommunicationService`) resolves unchanged.

**`_load_per_agent_mcp` migration.** The 125-line MCP loading function
moves to the tools package. Its sole caller (`template.py`) updates its
import; three test cases update theirs.

**Strategy dispatch.** `_send` becomes:

1. Look up `CommunicationTarget` from the store by name (the tool does this
   and passes the target object).
2. `TopologyPolicy.check(sender_kind, target, context)` — single gate.
3. Select strategy: `peer_normal` if `target.bus_ref` is set; else
   `subagent_dispatch` if `target.kind == SUBAGENT`; else `parent_reply`.
4. `strategy.execute(request)` → `AgentSendResult`.

Strategy selection is a flat dispatch on `bus_ref` presence + `kind`, not a
nested if-tree. Adding a future strategy = adding a branch + a class.

**`PeerNormalStrategy` behaviour.**

- `build_session`: `create_with_prefix(prefix=sender_session_prefix,
  agent_name=target.name)` — prefix reused, no `parent_session_id` (root).
- `normalize_invocation_id`: returns sender prefix internally; the
  `AgentSendResult.invocation_id` field is `None` (hidden from sender).
- `build_envelope`: `AGENT_MESSAGE` type; `invocation_id` set on the envelope
  for tracker bookkeeping; `build_agent_message` called with
  `invocation_id=None` so the XML the receiver sees has no id.
- `apply_tracker`: `record_send` (no `acknowledge` — no bracket in v1).
- `deliver`: `bus = target.bus_ref or local_bus; bus.send(...)`.

**`InboxPoller` — unseen session handling.** When the poller finds a pending
session id not in its local `SessionRegistry`, it registers it before
dispatching. The main agent instance already exists (eager-registered), so no
materialization is needed — only the session record is new. This branch is
generic (handles any unseen session, not just peer-originated ones).

**`SendToAgentTool` updated to pass `CommunicationTarget`.** The tool's
`execute` looks up the target via `store.get(name)` and passes the full
target object to `service.send_async(target=target, ...)`. The service
receives a pre-resolved target, not a name to resolve.

### Business layer changes

**`PoolTree` extension.** The business-layer `PoolTree` (or
`MainAgentNode`) gains a `peers: list[str]` field parsed from `pool.yml`'s
top-level `peers` key. The framework's `PoolConfig` is **unchanged**.

**`PoolStore` validation.** A validator (in `write_pool` or a dedicated
method) enforces: every peer name exists as a pool; every peer relationship
is bidirectional (A lists B ⟺ B lists A). Violations raise
`PoolValidationError`.

**`PoolInstance` exposure.** The dataclass exposes `agent_bus` and
`target_store` as readable fields, populated during `create_pool`.

**Phase 2 post-assembly.** After all pools in a workspace are built, the
assembly iterates each pool's `peers` list, reads the peer pool's
`main_agent_name` + `agent_bus`, constructs `CommunicationTarget` entries
with `bus_ref=peer_agent_bus`, and calls `target_store.add(target)` on the
local pool. This phase runs strictly after all Phase 1 `create_pool` calls,
preserving the "subagents first, peers second" insertion-order invariant.

**Frontend bidirectional sync.** Adding/removing a peer edge in the WebUI
updates both pools' `peers` lists atomically. The backend write is
transactional (both `pool.yml` files written before commit).

### Session semantics

- **Session Group** model: sender's prefix reused as receiver's prefix. A→C
  creates `convA.mainC`; C→A reply lands on `convA.mainA`. Context
  propagates within the group as designed behaviour.
- Peer sessions are **root sessions** (`parent_session_id=null`).
- The sender never sees an `invocation_id` in ack or XML.
- One session per `(prefix, agent)` pair — no parallel conversations in v1.

### Deferred items (explicitly out of scope for this spec)

1. **Generalized `AutoSendHook`** (NORMAL peer reply) — blocked on the
   communication-log artefact (needs per-turn routing signal to know which
   peer triggered the turn).
2. **Communication-log artefact** — a per-session structured record of
   "who-said-what / who-triggered-this-turn" for routing, recall, and UI.
3. **Per-pair context fork** — adapting `ContextForkBuilder` to fork a slice
   of A's user-session context into the peer session.
4. **Multiple parallel sessions per agent pair** — allowing the sender to
   mint an additional prefix segment, analogous to subagent invocation_id.

## Testing Decisions

### Test philosophy

Tests verify **external behaviour** (what the system does), not
implementation details (how it does it). The strategy refactor must preserve
behaviour — existing tests are the regression net. New tests cover new
behaviour (peer routing) and new invariants (name uniqueness, bidirectional
config). No test should assert on a strategy class name, a dispatch branch
identifier, or an internal data structure shape.

### Seams (four, all extending existing patterns)

**Seam 1 — Framework cross-pool integration (new file, existing pattern).**

Pattern source: `tests/integration/multi_agent/test_multi_pool_isolation.py`
already builds two real `AgentPool` instances with separate buses/pollers and
proves isolation. This is the natural extension point.

New test: `tests/integration/multi_agent/test_cross_pool_peer.py`.
- Build two pools (A + B), each with its own bus/inbox/poller (same pattern
  as `test_multi_pool_isolation.py`).
- Register a resident main agent in each (eager, matching business wiring).
- Add a `CommunicationTarget(name=B_main, kind=NORMAL, pool_name="B",
  bus_ref=B_bus)` to pool A's store.
- Send from A → assert envelope lands in B's inbox (not A's).
- Assert session id in B is `{A_prefix}.{B_main_name}`.
- Assert A's ack has `invocation_id=None`.
- Send from B → A (reply) → assert it lands in A's inbox on the same prefix.

This is the **highest seam** — it exercises the full chain (store-lookup →
strategy dispatch → cross-bus delivery → peer poller → peer turn) without
mocking any internal. One file, one new seam for the feature's core path.

**Seam 2 — Framework regression (existing files, unchanged).**

- `tests/integration/multi_agent/test_pool_communication.py` — main↔subagent
  e2e via `AgentCommunicationService`. Must pass unchanged after the strategy
  refactor. Verifies subagent_dispatch + parent_reply preserve behaviour.
- `tests/unit/multi_agent/test_communication_service.py` — service-level unit
  tests. Must pass unchanged.
- `tests/unit/multi_agent/test_send_to_agent_tools.py` — tool-level tests.
  Must pass unchanged (or with minimal adaptation for the
  `target`-object-passing change in `execute`).
- `tests/unit/multi_agent/test_communication_target_store.py` — store tests.
  Must pass; one new test case for duplicate rejection.

**Seam 3 — Bot config validation (existing pattern, extended).**

Pattern source: `examples/bot_project/tests/bot/config/test_pool_store.py`
already validates pool YAML parsing, naming rules, and structural constraints.

Extended tests in the same file (or a new `test_peer_pool_config.py`):
- `pool.yml` with `peers: [coding, research]` parses into `PoolTree.peers`.
- `PoolStore.write_pool` rejects a half-edge (A lists B, B does not list A).
- `PoolStore.write_pool` rejects a peer name that does not exist as a pool.
- Round-trip: write pool A with `peers: [B]`, write pool B with
  `peers: [A]`, read both back, verify both peers lists are correct.

**Seam 4 — Bot assembly integration (existing pattern, extended).**

Pattern source: `examples/bot_project/tests/service/test_config_wiring.py`
and `test_webui_service_workspace_wiring.py` cover assembly correctness.

New test: `examples/bot_project/tests/integration/
test_peer_pool_assembly.py`.
- Configure a workspace with two pools, each listing the other in `peers`.
- Run the workspace assembly (Phase 1 + Phase 2).
- Assert pool A's `CommunicationTargetStore` contains a target named
  B's main agent name with `bus_ref` pointing at B's bus.
- Assert pool B's store contains the reciprocal target.
- Assert subagent targets are ahead of peer targets in `store.list()` order.
- Assert a duplicate-name collision (two pools with same main agent name)
  raises `ValueError` during Phase 2.

### Prior art

All four seams follow patterns already established in the repo:
- Two-pool integration: `test_multi_pool_isolation.py` (the capstone of
  ADR-0015's poll-driven redesign).
- Single-pool e2e: `test_pool_communication.py`.
- Config validation: `test_pool_store.py` (629 lines of structural
  validation patterns).
- Assembly correctness: `test_config_wiring.py`.

### Unit tests for strategies (new, supporting)

Per-strategy unit tests in
`tests/unit/multi_agent/communication/strategies/`:
- `test_subagent_dispatch.py` — session prefix minting, TASK_REQUEST type,
  invocation_id surfaced in ack, tracker record_send, trace/output paths.
- `test_parent_reply.py` — parent session reuse, AGENT_MESSAGE type,
  tracker acknowledge/acknowledge_received bracket close.
- `test_peer_normal.py` — sender prefix reuse, root session
  (`parent_session_id=null`), AGENT_MESSAGE type, invocation_id hidden from
  result, `deliver` calls `bus_ref` when set, `deliver` falls back to local
  bus when `None`, tracker record_send (not acknowledge).

These use mocked `SendDeps` (mock bus, mock session factory, mock tracker)
and verify the strategy's observable outputs (envelope shape, session shape,
result fields), not internal method call counts.

## Out of Scope

1. **Generalized `AutoSendHook` for NORMAL peer contexts.** v1 has no
   auto-receipt: when a peer agent finishes a turn triggered by a peer
   message, it does not automatically notify the sender. The sender must
   explicitly call `send_to_agent` to reply. Implementing auto-receipt
   requires knowing *which* peer triggered the turn, which requires the
   communication-log artefact (also deferred).

2. **Communication-log artefact.** A per-session structured record of all
   peer communications (who-said-what, who-triggered-this-turn), persisted
   and available to the hook for routing and to the agent for multi-peer
   conversation recall. This is what unblocks auto-receipt, per-peer context
   isolation, and a "call history" UI view.

3. **Per-pair context fork.** Adapting `ContextForkBuilder` (currently
   subagent-only) to fork a slice of A's user-session context into the
   `convA.mainC` peer session, so A can answer C with user-conversation
   awareness without polluting the user session.

4. **Multiple parallel sessions per agent pair.** v1 supports one session per
   `(prefix, agent)` pair. A second conversation between the same two agents
   reuses the existing session. Lifting this requires allowing the sender to
   mint an additional prefix segment (analogous to subagent invocation_id).

5. **Session-group context isolation.** The v1 Session Group semantic (shared
   prefix → context propagates within the group) is designed behaviour. A
   future revision may offer opt-in isolation per peer pair, but v1 does not.

6. **Changes to `AgentCommKind`.** The enum remains `NORMAL | SUBAGENT`. No
   `PEER` kind is added — peer agents are NORMAL agents.

7. **Changes to framework `PoolConfig`.** The framework's `PoolConfig` is
   unchanged. The `peers` field lives on the business-layer `PoolTree`.

8. **New framework ABC for peer routing.** No `PeerPoolRouter` ABC. The store
   is the single routing source of truth.

## Further Notes

### ADR references

- **ADR-0019** (`docs/adr/0019-cross-pool-peer-communication.md`) — the
  architectural decision, including four rejected alternatives
  (`PeerPoolRouter` ABC, pair-scoped prefix, per-message fresh
  invocation_id, `AgentCommKind.PEER` enum value) with rejection rationale.
- **ADR-0019 blueprint**
  (`docs/adr/0019-cross-pool-peer-communication-blueprint.md`) — the
  step-by-step implementation plan with framework/business layer split,
  implementation order, and verification points.
- **ADR-0015** (unified inbox) — partially superseded: the
  `AgentCommunicationService._resolve_target` seam shifts to store-lookup
  routing. ADR-0015's D4 `AgentTarget` class hierarchy (never implemented)
  is formally superseded by the strategy dispatch.

### Glossary references

`CONTEXT.md` defines: **Communication Target**, **Peer Pool**, **Session
Group**, and updates **Target resolution** + the session-id-format ambiguity
note to reflect ADR-0019.

### Framework/business boundary invariant

The single most important architectural constraint: the framework gains a
routing *capability* (optional `bus_ref` field + a strategy that reads it),
not a peer-pool *concept*. Default state (`bus_ref=None` everywhere) is
byte-for-byte today's behaviour. Any framework user can wire cross-pool
communication via their own assembly without the bot-project changes. The
bot-project changes are the reference business implementation consuming the
capability.
