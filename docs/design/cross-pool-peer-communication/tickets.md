# Tickets: Cross-pool Peer Communication

One-line summary: framework capability (optional `bus_ref` on `CommunicationTarget`) + business-layer peer wiring that lets main agents of different pools message each other via `send_to_agent`.

Reference: spec at `docs/design/cross-pool-peer-communication/PRD.md`; ADR-0019 (`docs/adr/0019-cross-pool-peer-communication.md`) + blueprint (`docs/adr/0019-cross-pool-peer-communication-blueprint.md`).

Work the **frontier**: any ticket whose blockers are all done. For a purely linear chain that means top to bottom.

## Dependency graph

```
T1 ──────────────┐
                  ├──► T3 ──┐
T2 ──────────────┤          ├──► T4 ──► T6
                  │          │
T5 ──┬────────────┘          │
     └──► T7                  │
```

Three parallel start points: **T1, T2, T5**. Critical path: T1 → T3 → T4 → T6.

---

## T1: Move MCP loader out of communication.py into tools package

**What to build:** The 125-line `_load_per_agent_mcp` function (currently at the top of `multi_agent/communication.py`, unrelated to routing) relocates to the tools package as `load_per_agent_mcp`. Its sole caller (`multi_agent/template.py`) and the three test cases in `tests/framework/tools/test_mcp_registry_wiring.py` update their import paths. The function's behaviour is byte-for-byte unchanged — this is a pure code move that clears unrelated code from the routing file before the strategy refactor.

**Blocked by:** None — can start immediately.

- [x] `_load_per_agent_mcp` relocated to `modex_agent/tools/` (as `load_per_agent_mcp`, dropping the leading underscore — it is now a public resident of its rightful module).
- [x] Import in `multi_agent/template.py` updated to the new path.
- [x] Three test cases in `tests/framework/tools/test_mcp_registry_wiring.py` updated to import from the new path.
- [x] Stale path reference in `tools/mcp_adapter.py` comment updated.
- [x] `pytest tests/framework/tools/test_mcp_registry_wiring.py -v` — all three cases pass unchanged.
- [x] `ruff check src/modex_agent/multi_agent/communication.py` — no import of the moved function remains.
- [x] `mypy src/modex_agent/tools/` — type checks clean.

---

## T2: Extend CommunicationTarget with pool_name + bus_ref, enforce store uniqueness, add store.get

**What to build:** The `CommunicationTarget` dataclass gains two backward-compatible fields: `pool_name: str = ""` (owning pool — local or peer) and `bus_ref: AgentMessageBus | None = None` (direct bus reference; `None` = route locally). `CommunicationTargetStore.add` stops silently dropping duplicates and instead raises `ValueError` with the conflicting pool names, enforcing the invariant that target names are unique across all reachable pools. The store gains a `get(name) -> CommunicationTarget | None` lookup method so the tool and service can resolve a target by name from the single routing source of truth. No existing constructor breaks — all current call sites omit the new fields and implicitly describe local-pool targets.

**Blocked by:** None — can start immediately.

- [x] `CommunicationTarget` has `pool_name: str = ""` and `bus_ref: AgentMessageBus | None = None` fields with defaults that preserve every existing constructor call.
- [x] `CommunicationTargetStore.add` raises `ValueError` on duplicate name, listing both existing and new `pool_name` values in the message.
- [x] `CommunicationTargetStore.get(name) -> CommunicationTarget | None` method added (trivial dict lookup).
- [x] `bus_ref` type annotation uses `TYPE_CHECKING` guard to avoid circular import at runtime.
- [x] Existing unit tests `tests/unit/multi_agent/test_communication_target_store.py` pass unchanged (they don't add duplicate names) — partial caveat: two duplicate-add regression tests (`test_add_duplicate_is_noop` here, `test_duplicate_add_does_not_change_description` in `test_send_to_agent_tools.py`) tested the silent-drop behavior that T2 explicitly flips; they were replaced by `test_communication_target_store_rejects_duplicate_name` (here) and `test_duplicate_add_raises_value_error` (in `test_send_to_agent_tools.py`). All other pre-existing tests are unchanged and green.
- [x] New unit test: `test_communication_target_store_rejects_duplicate_name` — two targets with same name, assert `ValueError` with both pool names in message.
- [x] New unit test: `test_communication_target_get_returns_target_or_none` — existing name returns the target, unknown name returns `None`.
- [x] Audit `_build_communication` in `examples/bot_project/bot/service/pool_builder.py` — confirmed no subagent name collides with a profile name today: `pool.list_profiles()` after main-agent `register_resident` returns only the main agent (filtered by `p.name != main_agent_name`); template `agent_name`s are derived from unique filenames (`pools/{coding,default}/templates/*.yml` — `office-expert`, `context-builder`, `oracle`, `reviewer`, `delegate`, `worker`, `planner`, `scout`). No latent collision.
- [x] `mypy src/modex_agent/multi_agent/tools.py` — clean for T2 additions. (One pre-existing `no-redef` on line 388 unrelated to this change is already present on `develop_gyt`.)

---

## T3: Refactor communication.py into a strategy-dispatched package

**What to build:** The 610-line monolith `communication.py` (with its 172-line `_send` god-method containing three tangled routing branches) becomes a `communication/` package with a thin orchestrator and one strategy class per routing topology. `TopologyPolicy.check` is the single enforcement point for star-topology rules. `subagent_dispatch` (NORMAL→SUBAGENT) and `parent_reply` (SUBAGENT→parent NORMAL) strategies are extracted verbatim from the current `_send` branches — behaviour is preserved byte-for-byte, proven by existing regression tests. `SendToAgentTool.execute` looks up the target via `store.get(name)` and passes the full `CommunicationTarget` object to the service (instead of a name string). The service's `__init__.py` re-exports the public API so every existing import resolves unchanged.

**Blocked by:** T1 (move MCP loader first — clears the file for clean refactoring).

- [x] `multi_agent/communication.py` replaced by `multi_agent/communication/` package with: `__init__.py` (re-exports), `service.py` (thin orchestrator), `topology.py` (`TopologyPolicy.check`), `result.py` (`AgentSendResult` + ack formatting), `strategies/base.py` (`SendStrategy` ABC + `SendRequest` / `SendDeps` bundles), `strategies/subagent_dispatch.py`, `strategies/parent_reply.py`.
- [x] `TopologyPolicy.check(sender_kind, target, context) -> str | None` is the single enforcement point — no topology checks remain in the send trunk.
- [x] `AgentCommunicationService._send` reduced to: store-lookup → topology gate → strategy dispatch → `strategy.execute(request)`.
- [x] Strategy selection is a flat dispatch on `bus_ref` presence + `target.kind`, not a nested if-tree.
- [x] `subagent_dispatch` strategy preserves today's behaviour: fresh-uuid session prefix, `TASK_REQUEST` type, invocation_id surfaced in ack, `record_send` tracker, trace/output paths.
- [x] `parent_reply` strategy preserves today's behaviour: parent session reuse, `AGENT_MESSAGE` type, `acknowledge` + `acknowledge_received` bracket close.
- [x] `SendToAgentTool.execute` calls `store.get(name)` and passes `CommunicationTarget` to `service.send_async(target=target, ...)`.
- [x] `AgentCommunicationService.__init__` signature backward-compatible — existing wiring in `pool_builder.py::_build_communication` works unchanged.
- [x] `from modex_agent.multi_agent.communication import AgentCommunicationService, AgentSendResult` resolves unchanged (re-export).
- [x] `pytest tests/integration/multi_agent/test_pool_communication.py -v` — passes unchanged (behaviour preservation).
- [x] `pytest tests/unit/multi_agent/test_communication_service.py -v` — passes unchanged.
- [x] `pytest tests/unit/multi_agent/test_send_to_agent_tools.py -v` — passes (minimal adaptation for target-object passing is acceptable).
- [x] New unit tests: `tests/unit/multi_agent/communication/strategies/test_subagent_dispatch.py` and `test_parent_reply.py` — each strategy tested in isolation with mocked `SendDeps`, verifying observable outputs (envelope shape, session shape, result fields).
- [x] `mypy src/modex_agent/multi_agent/communication/` — type checks clean.

---

## T4: Add PeerNormalStrategy with cross-bus delivery and InboxPoller unseen-session handling

**What to build:** The first end-to-end vertical slice: pool A's main agent sends a message to pool B's main agent via `send_to_agent`, B's `InboxPoller` picks it up and starts a turn, B replies, and the reply lands back in A's inbox. The new `PeerNormalStrategy` builds a session reusing the sender's prefix (root session, no `parent_session_id`), hides `invocation_id` from the sender's ack and the receiver's XML, uses `AGENT_MESSAGE` type, and delivers to `target.bus_ref` when set (falling back to local bus when `None`). The `InboxPoller` gains a generic branch: when it finds a pending session id not in its local registry, it registers it before dispatching (the main agent instance already exists from eager boot registration — only the session record is new).

**Blocked by:** T2 (needs `pool_name` + `bus_ref` fields + `store.get`), T3 (needs the strategy package + `SendStrategy` ABC).

- [x] `PeerNormalStrategy` implements `SendStrategy`: `build_session` uses `create_with_prefix(prefix=sender_session_prefix, agent_name=target.name)` with no `parent_session_id`; `normalize_invocation_id` returns sender prefix internally but `AgentSendResult.invocation_id` is `None`; `build_envelope` uses `AGENT_MESSAGE` type with `invocation_id` on envelope but `build_agent_message` called with `invocation_id=None`; `apply_tracker` calls `record_send` (not `acknowledge`); `deliver` reads `target.bus_ref or local_bus` and calls `bus.send(...)`.
- [x] Service strategy dispatch selects `peer_normal` when `target.bus_ref is not None`.
- [x] `InboxPoller` registers an unseen session id in its local `SessionRegistry` before dispatching (generic branch — handles any unseen session, not just peer-originated).
- [x] Integration test `tests/integration/multi_agent/test_cross_pool_peer.py` (pattern: `test_multi_pool_isolation.py` two-pool setup):
  - Two pools with separate bus/inbox/poller; resident main agents eager-registered.
  - Pool A's store has a target with `bus_ref` pointing at pool B's bus.
  - Send from A → envelope lands in B's inbox (not A's); session id in B is `{A_prefix}.{B_main_name}`.
  - A's ack has `invocation_id=None`.
  - Send from B → A (reply) → lands in A's inbox on the same prefix.
- [x] New unit test `tests/unit/multi_agent/communication/strategies/test_peer_normal.py`: session built with sender prefix; invocation_id is None on result; envelope invocation_id is sender prefix; `deliver` calls `bus_ref` when set; `deliver` falls back to local bus when `bus_ref` is None; tracker `record_send` (not acknowledge).
- [x] `pytest tests/integration/multi_agent/ -v -m integration` — all existing pool communication + isolation tests still pass.

---

## T5: PoolTree peers field + PoolStore bidirectional validation

**What to build:** A pool's `pool.yml` can declare a top-level `peers: [coding, research]` key listing peer pool directory names. The business-layer `PoolTree` (in `bot/config/pool_store.py`) gains a `peers: list[str]` field parsed from this key. The framework's `PoolConfig` (`ioc/configs/pool.py`) remains unchanged — peers is purely a business-layer concern. `PoolStore` validates at write time: every peer name must exist as a pool directory; every peer relationship must be bidirectional (A lists B ⟺ B lists A). Half-edges and dangling references raise `PoolValidationError`.

**Blocked by:** None — can start immediately (pure business config, no framework dependency).

- [x] `PoolTree` (or the appropriate business config structure) gains `peers: list[str] = []` field.
- [x] `pool.yml` top-level `peers:` key parsed into `PoolTree.peers`.
- [x] Framework `PoolConfig` (`ioc/configs/pool.py`) unchanged — verify no diff.
- [x] `PoolStore.write_pool` (or a dedicated validator) enforces: peer name exists as a pool; bidirectional invariant (A lists B ⟺ B lists A). Violations raise `PoolValidationError`.
- [x] Extended tests in `examples/bot_project/tests/bot/config/test_pool_store.py` (or a new `test_peer_pool_config.py`):
  - [x] `pool.yml` with `peers: [coding, research]` parses into `PoolTree.peers`.
  - [x] Half-edge (A lists B, B does not list A) → `PoolValidationError`.
  - [x] Dangling peer name (references non-existent pool) → `PoolValidationError`.
  - [x] Round-trip: write A with `peers: [B]`, write B with `peers: [A]`, read both back, verify both peers lists correct.

---

## T6: Phase 2 post-assembly peer target population

**What to build:** A workspace with configured peers boots end-to-end. After all pools' `create_pool` has completed (Phase 1), a Phase 2 post-assembly step iterates each pool's `peers` list, reads the peer pool's `main_agent_name` + `agent_bus` from its `PoolInstance`, constructs `CommunicationTarget(name=peer_main_name, kind=NORMAL, pool_name=peer_pool, bus_ref=peer_agent_bus)` entries, and populates each local pool's `CommunicationTargetStore`. Subagent targets (added in Phase 1) remain ahead of peer main-agent targets in insertion order. `PoolInstance` exposes `agent_bus` + `target_store` as readable fields for Phase 2 access.

**Blocked by:** T4 (framework peer routing capability must exist), T5 (peers config must be parseable).

- [x] `PoolInstance` dataclass exposes `agent_bus: AgentMessageBus` and `target_store: CommunicationTargetStore` as readable fields, populated during `create_pool`.
- [x] Phase 2 post-assembly runs after all Phase 1 `create_pool` calls complete — iterates each pool's `peers` list, constructs peer `CommunicationTarget` entries with `bus_ref`, calls `local_store.add(target)`.
- [x] Insertion-order invariant: subagent targets (Phase 1) ahead of peer targets (Phase 2) in `store.list()`.
- [x] Integration test `examples/bot_project/tests/integration/test_peer_pool_assembly.py`:
  - [x] Workspace with two pools, each listing the other in `peers`.
  - [x] Run Phase 1 + Phase 2 assembly.
  - [x] Pool A's store contains target named B's main agent with `bus_ref` pointing at B's bus.
  - [x] Pool B's store contains reciprocal target.
  - [x] Subagent targets ahead of peer targets in `store.list()` order.
  - [x] Duplicate-name collision (two pools with same main agent name) → `ValueError` during Phase 2.
- [x] `pytest examples/bot_project/tests -q` — all existing bot tests pass.

---

## T7: WebUI peers bidirectional edge sync

**What to build:** A WebUI user adds or removes a peer relationship between two pools in a single atomic action — both pools' `peers` lists update simultaneously. The backend writes both `pool.yml` files transactionally (both before commit). The UI shows the peer pool's main agent name (resolved from the peer's `main_agent_name`) so the user understands what agent name their agent will see. Removing a peer edge removes both sides together.

**Blocked by:** T5 (needs `peers` field + `PoolStore` validation to exist).

- [x] WebUI pool config UI supports adding a peer relationship between pool A and pool B in a single action that writes both `pool.yml` files.
- [x] Removing a peer relationship removes both sides atomically.
- [x] Peer config UI displays the peer pool's main agent name (resolved from peer's `main_agent_name`), not just the pool directory name.
- [x] Frontend optimistic update: adding A→B immediately shows B→A in B's peer list.
- [x] Backend write is transactional — both files written before commit, or neither.
- [x] Manual or automated verification: add peer edge via UI → both `pool.yml` files updated → restart → both pools' `send_to_agent` tools list each other's main agent.
