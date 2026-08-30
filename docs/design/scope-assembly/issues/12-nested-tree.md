# 12 — Nested tree activation

**What to build:** A three-level declared tree (main → sub → subsub) works end to end from pure config: the mid-level agent gets `task` listing its declared children; the leaf gets `send_to_agent` to its parent; dispatch flows through the tree edges at any depth with the session tree recording actual dispatches (invocation branching) as always. The dispatch source-type restriction relaxes: any agent with declared children can dispatch (not just main). V6 guards the whole thing at startup. Subagent-of-subagent becomes config, not a latent capability.

**Blocked by:** 07 (pivot pool runs the declaration path).

**Status:** closed (resolved 2026-08-21)

- [x] Three-level declared tree in a test pool: main dispatches mid, mid dispatches leaf, results flow back up both levels
- [x] Mid-level agent's `task` tool lists exactly its direct children (not grandchildren); leaf has no `task` at all (not empty-enabled)
- [x] Session tree records the full chain with invocation branching at every level; session-id format unchanged
- [x] Dispatch source types accept non-main dispatchers (restriction relaxed; same-pool NORMAL peer removal does not regress this)
- [x] V6 negative test: declaring children under an agent whose effective toolset lost `task` fails startup
- [x] E2E integration test: 3-level tree + peer link to another pool's root + graph referencing the leaf — all in one config

**Resolution notes (2026-08-21):**
The dispatch-source relaxation lives in `TopologyPolicy.check` (topology.py): a
SUBAGENT sender may now address its own **declared children**
(`declared_children` — derived from the sender's per-agent
`CommunicationTargetStore`, SPEC §5.2's derived-children carrier) in addition to
its parent; subagent→undeclared-subagent and subagent→non-parent-NORMAL stay
rejected (star gate body intact). The mid-level per-agent store is built at
materialization (`AgentTemplate.children` → `_comm_facilities`), seeded by
`declared_pool_build` (root's store lists `root_children` only — never
grandchildren). Two residuals from lane-09 were resolved as the E2E's
prerequisites, per plan: the graph configurator gates relaxed to the
session-binding graph signal (`is_node_execution`, SPEC §4 axis 3 — graph
metadata per turn, agent-kind-independent), and the graph turn-config trio
(binding store + resolver + 6 configurators) sank into the shared
`wire_graph_turn_config` called by BOTH `_wire_main_pipeline` and
`AgentTemplate.materialize`. E2E:
`examples/bot_project/tests/integration/test_nested_tree_e2e.py` over
`fixtures/scope/nested-tree-e2e.yml` (one config: 3-level tree + bidirectional
peer link + graph referencing the never-dispatched lazy leaf).
