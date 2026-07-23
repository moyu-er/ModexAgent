# ADR-0019 Implementation Blueprint — Cross-pool peer communication

Reference: [ADR-0019](./0019-cross-pool-peer-communication.md)

This blueprint splits work into **framework** (`src/modex_agent/`) and
**business** (`examples/bot_project/`) changes. Framework changes ship first
and are independently testable; business changes consume the framework's new
capability.

---

## Part F — Framework (`src/modex_agent/`)

### F0. Scope guard

The framework does **not** gain:
- Any new "peer pool" concept, type, or config field
- Any change to `AgentCommKind`, `PoolConfig`, `AgentDescriptor`
- Any new ABC for peer routing
- Any change to default behaviour when `bus_ref=None`

The framework **does** gain:
- Two optional fields on an existing dataclass
- A refactor of one file into a package
- A new strategy class in the new package
- One new invariant on an existing store

### F1. Migrate `_load_per_agent_mcp` out of `communication.py`

**Why first:** Pure code move; unblocks the communication refactor by removing
125 lines of unrelated MCP loading code from the routing file.

**Changes:**
- New file: `src/modex_agent/tools/mcp_loader.py`
  - Move `_load_per_agent_mcp` (communication.py:46-170) verbatim into it.
  - Make it `mcp_loader.load_per_agent_mcp` (drop the leading underscore — it
    is now a public resident of its rightful module).
- Update import in `src/modex_agent/multi_agent/template.py:361`:
  - `from modex_agent.multi_agent.communication import _load_per_agent_mcp`
    → `from modex_agent.tools.mcp_loader import load_per_agent_mcp`
- Update test imports: `tests/framework/tools/test_mcp_registry_wiring.py`
  references `_load_per_agent_mcp` from `multi_agent.communication` — update to
  new path.
- Update doc reference in `src/modex_agent/tools/mcp_adapter.py:166` which
  quotes the old path in a comment.

**Verification:**
- `pytest tests/framework/tools/test_mcp_registry_wiring.py -v` — all three
  test cases (`test_load_per_agent_mcp_uses_registry_when_provided`,
  `test_load_per_agent_mcp_registry_acquire_failure_is_fail_soft`,
  `test_load_per_agent_mcp_falls_back_without_registry`) pass unchanged.
- `ruff check src/modex_agent/multi_agent/communication.py` — no import of the
  moved function remains.

### F2. Extend `CommunicationTarget` dataclass

**File:** `src/modex_agent/multi_agent/tools.py`

**Change:**
```python
@dataclass(frozen=True)
class CommunicationTarget:
    name: str
    kind: AgentCommKind
    description: str = ""
    pool_name: str = ""              # 🆕 owning pool name (local pool or peer pool)
    bus_ref: "AgentMessageBus | None" = None  # 🆕 direct bus reference; None = local
```

`pool_name` defaults to `""` (not `None`) for backward compatibility — existing
call sites that construct `CommunicationTarget(name=..., kind=..., description=...)`
keep working; they implicitly describe local-pool targets.

`bus_ref` is `TYPE_CHECKING`-guarded to avoid a circular import at runtime
(`AgentMessageBus` is in the same package; the import is for type hints only).

**Verification:**
- No call site changes required for existing constructors — they all omit the
  new fields and get the defaults.
- `mypy src/modex_agent/multi_agent/tools.py` — type checks.

### F3. Enforce name uniqueness in `CommunicationTargetStore.add`

**File:** `src/modex_agent/multi_agent/tools.py`

**Current** (`tools.py:139-147`):
```python
def add(self, target: CommunicationTarget) -> None:
    if self._for_subagent:
        return
    if target.name not in self._targets:
        self._targets[target.name] = target
        self._description = None
```

**Change:** The current code silently drops duplicates (`if target.name not in
self._targets`). This is wrong for the new invariant — two pools with same
agent name must be caught. Raise on duplicate:

```python
def add(self, target: CommunicationTarget) -> None:
    if self._for_subagent:
        return
    if target.name in self._targets:
        existing = self._targets[target.name]
        raise ValueError(
            f"Duplicate communication target name {target.name!r}: "
            f"existing pool={existing.pool_name!r}, "
            f"new pool={target.pool_name!r}. "
            "Target names MUST be unique across all reachable pools."
        )
    self._targets[target.name] = target
    self._description = None
```

**Risk:** Existing call sites in `_build_communication` (`pool_builder.py:1080-
1096`) add subagent targets from `pool.list_profiles()` + `templates`. These
are constructed without `pool_name`, so the field is `""`. If a pool happens to
have a subagent with the same name as a profile entry, this would now raise
instead of silently dropping. Audit the existing assembly to confirm there is
no duplicate today — if there is, the duplicate was already a latent bug.

**Verification:**
- Existing unit tests `tests/unit/multi_agent/test_communication_*` pass
  (they don't add duplicate names).
- New unit test: `test_communication_target_store_rejects_duplicate_name`
  in `tests/unit/multi_agent/test_tools.py` — construct two targets with the
  same `name`, assert `ValueError`.

### F4. Refactor `communication.py` into a strategy-dispatched package

This is the largest framework change. Break into sub-steps.

#### F4a. Create package skeleton

**New files:**
```
src/modex_agent/multi_agent/communication/
├── __init__.py
├── service.py
├── topology.py
├── result.py
└── strategies/
    ├── __init__.py
    ├── base.py
    ├── subagent_dispatch.py
    ├── parent_reply.py
    └── peer_normal.py
src/modex_agent/multi_agent/communication.py  → DELETED (replaced by package)
```

`__init__.py` re-exports the public API that existing callers import:
```python
from modex_agent.multi_agent.communication.service import AgentCommunicationService
from modex_agent.multi_agent.communication.result import AgentSendResult
```

This preserves every existing import (`from modex_agent.multi_agent.communication
import AgentCommunicationService, AgentSendResult`) — no caller changes.

#### F4b. `result.py` — value object + ack formatting

Move `AgentSendResult` (communication.py:173-186) here verbatim. Move the ack
text builder from `send_async` (communication.py:411-436) into a standalone
function `format_send_ack(result: AgentSendResult) -> str` on this module, so
`service.py` stays thin.

#### F4c. `topology.py` — single policy gate

Move `_star_topology_error` (communication.py:320-354) here as a class:

```python
class TopologyPolicy:
    """Star-topology + peer-policy gate. Single enforcement point."""

    @staticmethod
    def check(
        sender_kind: AgentCommKind,
        target: CommunicationTarget,
        sender_context: AgentContext,
    ) -> str | None:
        """Return error string if forbidden, None if allowed."""
        # Subagent senders may only address their parent.
        if sender_kind != AgentCommKind.SUBAGENT:
            return None  # NORMAL senders (incl. peer) are unrestricted
        if target.kind == AgentCommKind.SUBAGENT:
            return "Subagents can only reply to normal agents; ..."
        parent_name = resolve_parent_name(sender_context)
        if parent_name is not None and target.name != parent_name:
            return f"Subagents can only address the agent that assigned their task ..."
        return None
```

No new restrictions vs today — NORMAL→peer-NORMAL passes through the
`sender_kind != SUBAGENT` early return, exactly as today's NORMAL→NORMAL would.

#### F4d. `strategies/base.py` — the SendStrategy ABC

```python
class SendStrategy(ABC):
    """Handles one send to one target topology."""

    def __init__(self, deps: SendDeps) -> None:
        self._deps = deps

    @abstractmethod
    async def execute(self, req: SendRequest) -> AgentSendResult: ...

    @abstractmethod
    def build_session(self, req: SendRequest) -> SessionInfo: ...

    @abstractmethod
    def normalize_invocation_id(self, req: SendRequest) -> str | None: ...

    @abstractmethod
    def build_envelope(self, req: SendRequest, session: SessionInfo) -> AgentMessageEnvelope: ...

    @abstractmethod
    def apply_tracker(self, req: SendRequest, env: AgentMessageEnvelope) -> None: ...

    @abstractmethod
    async def deliver(self, env: AgentMessageEnvelope, target: CommunicationTarget) -> str | None: ...
```

`SendRequest` and `SendDeps` are frozen dataclasses bundling the inputs the
strategies need (target, content, invocation_id, context, session_factory,
session_registry, comm_tracker, local_bus). Bundling avoids passing 10
parameters per strategy method.

#### F4e. `strategies/subagent_dispatch.py` — Strategy #1

Extract from current `_send` lines 484-532. Behaviour preserved verbatim:
- `build_session`: `create_with_prefix(prefix=minted_uuid, parent_session_id=sender_sid)`
- `normalize_invocation_id`: mint uuid if None, else use as-is
- `build_envelope`: `TASK_REQUEST`, `invocation_id` in payload
- `apply_tracker`: `record_send`
- `deliver`: `self._deps.local_bus.send(env.agent_session_id, env)`
- `execute`: orchestrates the above, returns `AgentSendResult` with trace/output paths

#### F4f. `strategies/parent_reply.py` — Strategy #2

Extract from current `_send` lines 542-610, subagent→parent branch. Behaviour
preserved verbatim:
- `build_session`: `SessionInfo.from_str(context.session.parent_session_id)` when
  parent is set; else fallback `create(external_id=...)`
- `normalize_invocation_id`: None (NORMAL targets ignore invocation_id)
- `build_envelope`: `AGENT_MESSAGE`, `invocation_id=sender_sid.session_id_prefix`
- `apply_tracker`: `acknowledge` + `acknowledge_received` (close send bracket)
- `deliver`: `self._deps.local_bus.send(...)`

#### F4g. `strategies/peer_normal.py` — Strategy #3 (NEW)

The new strategy. Behaviour:
- `build_session`: `create_with_prefix(prefix=sender_sid.session_id_prefix,
  agent_name=target.name)` — reuse sender's prefix, no parent_session_id (root).
- `normalize_invocation_id`: returns `sender_sid.session_id_prefix` internally
  for envelope/tracker use, but the value is NOT surfaced in the ack or XML.
  The `invocation_id` field on `AgentSendResult` is `None`.
- `build_envelope`: `AGENT_MESSAGE` message type, `invocation_id` set on the
  envelope (for tracker/session bookkeeping) but `build_agent_message` is called
  with `invocation_id=None` so the XML the receiving agent sees has no id.
- `apply_tracker`: `record_send` (no acknowledge — peer has no send/ack bracket
  in v1; that comes with the deferred communication-log).
- `deliver`: **key difference** — reads `target.bus_ref`:
  ```python
  async def deliver(self, env, target):
      bus = target.bus_ref or self._deps.local_bus
      await bus.send(env.agent_session_id, env)
      return None
  ```
- `execute`: returns `AgentSendResult` with `invocation_id=None`, no
  trace/output paths (peer normals don't have subagent-style trace dirs).

#### F4h. `service.py` — thin orchestrator

```python
class AgentCommunicationService:
    def __init__(self, ...same params as today...):
        ...store deps...
        self._strategies = {
            "subagent_dispatch": SubagentDispatchStrategy(deps),
            "parent_reply": ParentReplyStrategy(deps),
            "peer_normal": PeerNormalStrategy(deps),
        }

    async def _send(self, *, target: CommunicationTarget, content, invocation_id,
                    context: AgentContext) -> AgentSendResult | None:
        # 1. Topology gate
        err = TopologyPolicy.check(context.comm_kind, target, context)
        if err is not None:
            return AgentSendResult.error(target.name, target.kind, err)

        # 2. Pick strategy by target.bus_ref + target.kind
        if target.bus_ref is not None:
            strategy = self._strategies["peer_normal"]
        elif target.kind == AgentCommKind.SUBAGENT:
            strategy = self._strategies["subagent_dispatch"]
        else:
            strategy = self._strategies["parent_reply"]

        req = SendRequest(target=target, content=content,
                          invocation_id=invocation_id, context=context)
        return await strategy.execute(req)
```

The strategy selection is deliberately a flat dispatch on `bus_ref` presence +
`kind`, not a nested if-tree. Adding a future strategy means adding a branch
and a strategy class — not refactoring `_send`.

#### F4i. Update `SendToAgentTool.execute` to pass `CommunicationTarget`

**File:** `src/modex_agent/multi_agent/tools.py`

The tool currently looks up the target name in the store and passes the name
string to `service.send_async(target_agent=name, ...)`. Change to pass the full
`CommunicationTarget` object:

```python
async def execute(self, **kwargs):
    name = str(kwargs.get("target_agent", ""))
    target = self._store.get(name)  # 🆕 returns CommunicationTarget or None
    if target is None:
        return f"Error: '{name}' is not a valid communication target. ..."
    return await self._service.send_async(target=target, content=..., context=...)
```

This requires adding a `get(name) -> CommunicationTarget | None` method to
`CommunicationTargetStore` (trivial — it's `self._targets.get(name)`).

#### F4j. Update `InboxPoller` to handle unseen peer sessions

**File:** `src/modex_agent/multi_agent/inbox_poller.py`

When a peer message arrives, the target pool's poller sees a session id
(`convA.mainC`) that is not in its session registry. Currently the poller's
`_materialize_then_turn` path handles "no instance" — for peer sessions the
instance IS already registered (main agent is eager), but the session record is
missing.

Add a branch in the poller's turn-start path (specifically wherever it looks
up the session before dispatching):

```python
# Pseudo-code — actual placement TBD by reading inbox_poller.py
if not await self._session_registry.has(sid):
    # Peer-normal first turn: register the session, instance is already live.
    await self._session_registry.register(SessionInfo.from_str(sid))
# Proceed to dispatch — instance lookup will succeed for main agents.
```

This branch is generic — it does not know about "peer pools". It handles any
case where a session id appears in the inbox without a prior registry entry,
which is exactly the peer-normal first-turn case.

**Verification (whole F4):**
- `pytest tests/unit/multi_agent/ -v` — all existing tests pass (behaviour
  preserved by construction; strategies are extractions).
- `pytest tests/integration/multi_agent/ -v -m integration` — pool
  communication, multi-pool isolation tests pass.
- New unit tests per strategy: `tests/unit/multi_agent/communication/
  strategies/test_subagent_dispatch.py`, `test_parent_reply.py`,
  `test_peer_normal.py` — each strategy tested in isolation with mocked deps.
- New integration test: `tests/integration/multi_agent/test_cross_pool_peer.py`
  — two pools wired with peer targets, A→C send lands in C's inbox, C's poller
  starts a turn, C→A reply lands back in A's inbox.

---

## Part B — Business (`examples/bot_project/`)

### B1. Extend `PoolTree` with `peers` field

**File:** `examples/bot_project/bot/config/pool_store.py`

Add `peers: list[str] = Field(default_factory=list)` to `PoolTree` (or
`MainAgentNode`, whichever holds the pool-level config — verify by reading the
current shape). Parse from `pool.yml` top-level `peers:` key.

### B2. `PoolStore` validation: bidirectional invariant

**File:** `examples/bot_project/bot/config/pool_store.py`

In `PoolStore.write_pool` or a dedicated validator:
- For each pool P with `peers: [Q, R]`, verify Q and R exist.
- Verify Q's `peers` contains P, and R's `peers` contains P.
- If any half-edge is missing, raise `PoolValidationError`.

The frontend (B6) maintains this interactively; this validator is the backend
safety net for hand-edited YAML.

### B3. Expose `agent_bus` + `target_store` on `PoolInstance`

**File:** `examples/bot_project/bot/service/pool_instance.py`

Add fields to the `PoolInstance` dataclass so Phase 2 assembly can read them:

```python
@dataclass
class PoolInstance:
    ...existing fields...
    agent_bus: AgentMessageBus          # 🆕 expose for peer wiring
    target_store: CommunicationTargetStore  # 🆕 expose for peer wiring
```

`create_pool` already builds these — wire them into the returned `PoolInstance`.

### B4. Phase 2 post-assembly peer target population

**File:** `examples/bot_project/bot/workspace/wiring.py` (or wherever
`_build_resources` orchestrates per-workspace pool creation — verify location).

After all pools in a workspace are built:

```python
# Phase 2: cross-pool peer wiring
for pool_name, instance in resources.pools.items():
    pool_tree = pool_store.read_pool(pool_name)
    for peer_pool_name in pool_tree.peers:
        peer_instance = resources.pools[peer_pool_name]
        target = CommunicationTarget(
            name=peer_instance.main_agent_name,
            kind=AgentCommKind.NORMAL,
            pool_name=peer_pool_name,
            bus_ref=peer_instance.agent_bus,
            description=f"Peer pool {peer_pool_name}'s main agent",
        )
        instance.target_store.add(target)
```

Ordering invariant: Phase 2 runs strictly after all Phase 1 `create_pool` calls
have completed. Subagent targets (added in Phase 1 by `_build_communication`)
are therefore ahead of peer targets in insertion order.

### B5. Frontend: peers双向边同步

**File:** `examples/bot_project/webui/` (React components + API endpoints)

When the user adds peer B to pool A's peers list in the UI:
- Frontend immediately adds A to B's peers list (optimistic UI).
- Backend `POST /api/pools/{name}` writes both pool trees atomically.
- When removing, both sides removed together.

API endpoint shape is to-be-designed by reading the current pool config API.
No framework impact.

### B6. Documentation updates

**Files:**
- `examples/bot_project/AGENTS.md` — add a section on peer pool communication.
- `examples/bot_project/README.md` — brief usage example.
- `examples/bot_project/config/pools/main/pool.yml` — add a commented-out
  `peers:` example.

---

## Implementation Order

Strict sequential, each step independently verifiable:

1. **F1** — move `_load_per_agent_mcp`. Verify tests.
2. **F2** — extend `CommunicationTarget`. Verify types.
3. **F3** — enforce uniqueness. Verify tests + audit existing assembly for
   accidental duplicates.
4. **F4a-F4c** — create package skeleton, `result.py`, `topology.py`. Verify
   imports resolve, no behaviour change yet (service still has old `_send`).
5. **F4d-F4f** — extract subagent_dispatch + parent_reply strategies. Verify
   existing tests pass (behaviour preservation).
6. **F4g** — add `peer_normal` strategy. Unit test in isolation.
7. **F4h** — wire service to strategy dispatch. Verify existing tests pass.
8. **F4i** — update `SendToAgentTool` to pass `CommunicationTarget`.
9. **F4j** — update `InboxPoller` for unseen sessions.
10. **B1-B3** — business-layer data structures + `PoolInstance` exposure.
11. **B4** — Phase 2 peer wiring. Integration test.
12. **B5** — frontend.
13. **B6** — docs.

Steps 1-9 are framework-only and shippable independently — a framework user
could wire peers via their own assembly without the bot-project changes.
Steps 10-13 are the reference business implementation consuming the capability.

---

## Test Strategy

### Framework unit tests (new)

- `tests/unit/multi_agent/communication/strategies/test_subagent_dispatch.py`
- `tests/unit/multi_agent/communication/strategies/test_parent_reply.py`
- `tests/unit/multi_agent/communication/strategies/test_peer_normal.py`
  - Tests: session built with sender prefix, invocation_id is None on result,
    envelope invocation_id is sender prefix, deliver calls bus_ref when set,
    deliver falls back to local bus when bus_ref is None.
- `tests/unit/multi_agent/test_tools.py`
  - `test_communication_target_store_rejects_duplicate_name`
  - `test_communication_target_get_returns_target_or_none`

### Framework integration tests (new)

- `tests/integration/multi_agent/test_cross_pool_peer.py`
  - Two `AgentPool` instances with separate buses.
  - Pool A's store has a target pointing at pool B's bus.
  - Send from A → lands in B's inbox → B's poller starts turn → B replies →
    lands in A's inbox.
  - Verify session id is `convA.mainB` in pool B.
  - Verify A's ack has `invocation_id=None`.

### Business integration tests (new)

- `examples/bot_project/tests/integration/test_peer_pool_assembly.py`
  - Workspace with two pools configured as peers.
  - Boot the workspace stack.
  - Verify both pools' stores contain each other's main agent as a peer target.
  - Verify subagent targets are ahead of peer targets in store order.

### Existing tests (regression)

- `tests/unit/multi_agent/` — all pass unchanged (strategies preserve behaviour).
- `tests/integration/multi_agent/` — all pass unchanged.
- `tests/framework/tools/test_mcp_registry_wiring.py` — passes after F1 import
  path update.
