# 13 — Peer link scope-path resolution (SG1 closure)

**What to build:** Peer links resolve through the scope path: at materialize/assembly time, a root's `send_to_peer` registration resolves the peer pool's bus/tree references from the owning workspace's resource bundle — the framework-native migration of today's business Phase-2 peer wiring. Same-workspace validation (V5) is live, the ADR-0019 bidirectional invariant is enforced as today, and the receiving peer session remains a root session (root-to-root semantics unchanged). v1 restricts links to the same workspace; the declaration shape carries no workspace hard-coding (future cross-workspace bridge = validation relaxation + plugin, per N5).

**Blocked by:** 07 (pivot pool runs the declaration path).

**Status:** closed (resolved 2026-08-21)

- [x] Declared links produce working `send_to_peer` tools whose targets resolve via the scope path from the workspace resource bundle
- [x] BIZ Phase-2 peer wiring code deleted (FW-ized); the deletion ledger names it
- [x] review ↔ default peer pair (shipped config) behaves identically to today: message round-trip, root sessions, bidirectional registration
- [x] V5 live: same-workspace constraint enforced; cross-workspace link fails startup
- [x] Pool-rooted declarations (no workspace layer) declaring peers fail startup with a clear message (v1 rule)
- [x] Peer e2e tests (ADR-0019 suite) green

## Resolution notes (2026-08-21)

- **(a)** The FW resolution service lives at
  `modex_agent.multi_agent.communication.peer_resolution`:
  `peer_links_from_declaration(spec)` extracts per-pool links over the
  scope path (each link carries the peer pool's root-agent face —
  description + execution strategy); `resolve_peer_targets(pools, links)`
  resolves, at workspace materialize time (after every pool of the bundle
  is built), the peer NORMAL target into each sender's per-agent
  `CommunicationTargetStore` with `tree_ref` read from the peer's
  `PoolInstance` **in the same bundle**. Runtime facts (booted root name,
  tree reference) come from the bundle; declaration facts (description,
  strategy) come from the link. The ticket-07 TOOL factory
  (`SendToPeerToolFactory`) reads the store unchanged. 9 FW unit tests.
- **(b)** BIZ Phase-2 peer wiring deleted: the resources.py loop that
  re-read pool.yml and hand-built peer `CommunicationTarget`s (with the
  Phase-2 `PoolStore` re-construction) is gone. Honest dual-boot
  assessment: the legacy pools (coder/review — review peers with default,
  a declared pool) still need peer supply, so per convergence rule 1 both
  roads feed the SAME service — declaration pools over the scope path,
  legacy pools with their frozen pool.yml peers (`PeerLink` records built
  in resources.py, ~20 lines that die with ticket 17). Not two parallel
  implementations: one resolution entry point, two link sources. Grep
  proof in evidence (no peer-target construction remains outside the FW
  service; `CommunicationTarget(` survivors are the declared-road
  subagent child entries in factory.py and the legacy `_build_communication`
  pool-level store population — both pre-existing, both on their own
  tickets' ledgers).
- **(c)** `test_peer_resolution_cross_road_round_trip`
  (examples/bot_project/tests/integration/test_peer_pool_assembly.py):
  a REAL `BotService` workspace build with default on the declaration
  road and review on the legacy road — bidirectional registration
  (each store holds the peer's NORMAL target whose `tree_ref` IS the
  peer pool's own tree manager), default→review delivery lands on a ROOT
  session (`parent_session_id=None`) reusing the sender's prefix, and
  review→default reply lands back in default's inbox on the same prefix
  (the session group). Prefix reuse / root-to-root semantics untouched
  (`PeerNormalStrategy` and `peer_normal.py:32-39` zero-diff).
- **(d)** V5 live at boot: `test_boot_v5_cross_workspace_peer_fails_startup`
  (a peer naming a pool the workspace does not host — the v1
  cross-workspace shape — aborts `boot_scope_declaration` with the V5
  rule + same-workspace guidance). The pure-function halves were already
  covered (tests/unit/scope/test_validator.py); these pin the boot
  wiring. The FW service itself is also loud at resolution time (sender
  or peer absent from the bundle → ValueError naming the v1 invariant).
- **(e)** `test_boot_v5_pool_as_root_peer_fails_startup`: a pool-as-root
  declaration carrying peers aborts startup with the v1 rule spelled out
  (the same-workspace premise is undefined without a workspace layer).
- **(f)** ADR-0019 e2e suite green: `tests/integration -m integration`
  93 passed (cross-pool peer round-trip, session-tree integration). The
  07 split-brain manifest stays green WITHOUT golden refresh — the
  declared road's peer-entry products are field-identical to the frozen
  old-road baseline; the drivers now exercise the production FW service
  on both roads.
