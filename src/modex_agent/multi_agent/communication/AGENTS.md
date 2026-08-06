<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-12 | Updated: 2026-07-12 -->

# communication

Strategy-dispatched inter-agent messaging package (ADR-0019). Replaces the
former 610-line `communication.py` monolith with a thin orchestrator +
one strategy class per routing topology.

## Purpose

Route a `send_to_agent` call from a NORMAL or SUBAGENT sender to its target
agent — intra-pool (star) or cross-pool (peer) — using a single
`TopologyPolicy.check` gate followed by a flat strategy dispatch. The service
never creates agent instances; it only constructs sessions, envelopes, and
delivery. Materialization is owned by `AgentTemplate.materialize` (invoked
lazily by the poller).

## Topology Model

The actual topology is a **per-pool star + cross-pool peer mesh**:

```
Pool A (star)                Pool B (star)
  mainA ──┬── scout             mainB ──┬── planner
          ├── worker                     ├── reviewer
          └── oracle                      └── ...

  mainA ←──── bus_ref peer edge ────→ mainB
        (NORMAL→NORMAL cross-pool)
```

- **Within a pool**: strict star — a SUBAGENT sender may only address its
  parent NORMAL (recovered from `session.parent_session_id` via
  `resolve_parent_name`). Subagent→subagent and subagent→non-parent-NORMAL
  are rejected by `TopologyPolicy.check`.
- **Across pools**: NORMAL→peer-NORMAL via `target.bus_ref` — the policy
  gate returns `None` for NORMAL senders (no constraint); `PeerNormalStrategy`
  delivers directly to the peer pool's `AgentMessageBus`.
- **NORMAL→SUBAGENT** (parent dispatch): always allowed; routed to
  `SubagentDispatchStrategy`.

`AgentCommKind` stays `NORMAL | SUBAGENT` (no `PEER` kind) — peer is a
routing path, not a topology role.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports `AgentCommunicationService` + `AgentSendResult` so existing imports (`from modex_agent.multi_agent.communication import AgentCommunicationService`) resolve unchanged |
| `service.py` | `AgentCommunicationService` — thin orchestrator. `send_async(target, content, invocation_id, context)` → `_send` → `TopologyPolicy.check` → strategy dispatch → `format_send_ack`. Owns the `_strategies: dict[SendStrategyKind, SendStrategy]` map. Never creates agent instances. |
| `topology.py` | `TopologyPolicy.check(sender_kind, target, sender_context) -> str | None` — single star-topology enforcement point. Returns error string if forbidden, `None` if allowed. Only constrains SUBAGENT senders. |
| `result.py` | `AgentSendResult` (frozen dataclass) + `format_send_ack(result) -> str`. The ack text differs for peer sends (`is_peer_send=True`) vs subagent dispatches (includes trace path + invocation_id). |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `strategies/` | `SendStrategy` ABC + `SendDeps`/`SendRequest` bundles + three concrete strategies (see `strategies/AGENTS.md`) |

## Strategy Dispatch

`_send` selects the strategy with a flat dispatch on `bus_ref` presence +
`target.kind`:

```python
if target.bus_ref is not None:
    strategy = PEER_NORMAL        # cross-pool NORMAL→NORMAL
elif target.kind == SUBAGENT:
    strategy = SUBAGENT_DISPATCH   # parent→child task dispatch
else:
    strategy = PARENT_REPLY        # subagent→parent reply (fallback for in-pool NORMAL→NORMAL)
```

Adding a future strategy = adding a `SendStrategyKind` enum value + a
`SendStrategy` subclass + one dispatch branch.

## Session Semantics

| Strategy | Session construction | invocation_id in ack | invocation_id in message | parent_session_id | message_type |
|---|---|---|---|---|---|
| SubagentDispatch | `create_with_prefix(prefix=invocation_id, parent=sender)` | surfaced | surfaced | set (sender) | `TASK_REQUEST` |
| ParentReply | reuse `parent_session_id` | hidden (None) | hidden | n/a (reuse) | `AGENT_MESSAGE` |
| PeerNormal | `create_with_prefix(prefix=sender_prefix, parent=None)` — root session | hidden (None) | hidden | not set (root) | `AGENT_MESSAGE` |

**Session Group** (ADR-0019): peer sends reuse the sender's session prefix
verbatim, so A→B creates `convA.mainB` and B→A reply lands on `convA.mainA`.
Context propagates within the session group as designed behaviour.

## For AI Agents

### Working In This Directory
- The service is a **pure router** — never add agent-instance creation logic here.
- `TopologyPolicy.check` is the **single** enforcement point — do not add
  topology checks to strategies, the service trunk, or interceptors.
- Strategy dispatch is flat on `bus_ref` + `kind` — do not introduce nested
  if-trees.
- `send_async` and `_send` require a pre-resolved `CommunicationTarget`
  (no name-string lookup). The tool (`SendToAgentTool.execute`) does the
  `store.get(name)` lookup.
- `CommunicationTarget.bus_ref` is the load-bearing field for cross-pool
  routing: `None` = local (today's behaviour), set = cross-pool.
- `AgentCommunicationService.__init__` is backward-compatible — new deps
  (`agent_bus`, `session_registry`, `target_store`, etc.) default to `None`.

### Common Patterns
- Strategies receive a frozen `SendDeps` bundle (source, broker,
  session_factory, agent_bus, session_registry, workspace_path_resolver).
- `SendStrategy.execute` is a template method: normalize → session →
  (register) → envelope → deliver → build_result. Concrete strategies
  override individual hooks.
- `AgentSendResult` carries `is_peer_send` so `format_send_ack` can emit
  the correct ack text (peer sends have no trace path).

## Dependencies

### Internal
- `modex_agent.multi_agent.strategies` — `SendStrategy` ABC + concrete strategies
- `modex_agent.multi_agent.tools` — `CommunicationTarget`, `CommunicationTargetStore`, `resolve_parent_name`
- `modex_agent.multi_agent.envelope` — `AgentMessageEnvelope`
- `modex_agent.multi_agent.message_format` — `build_dispatch_message`, `build_agent_comm_message`, `SourceLabel`, `ResultMeta`
- `modex_agent.multi_agent.bus` — `AgentMessageBus` (delivery target)
- `modex_agent.multi_agent.address` — `AgentAddress`
- `modex_agent.core.session_id` — `SessionIdFactory`, `SessionInfo`
- `modex_agent.core.agent` — `AgentCommKind`, `AgentContext`
- `modex_agent.messaging.broker` — `MessageBroker` (fallback delivery)

<!-- MANUAL -->
