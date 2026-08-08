# ADR-0036: Node Identity and Static Graph I/O Breaking Changes

Status: accepted (2026-08-08). Implementation is staged across the static
graph scheduling waves; each decision becomes active only when its owning
wave lands.

## Context

`modex_graph` originally used `Node.name` for both human-readable topology
and persistent identity. Names are graph-local and can be reused in another
graph or subgraph, so they cannot safely key invocation state, deliveries,
recovery metadata, or agent sessions. The engine also models START and END
as sentinels rather than executable nodes, which leaves graph input and
result aggregation outside the normal node lifecycle.

Static graph scheduling needs stable machine identity, persisted deliveries
to terminal nodes, and typed graph I/O. These requirements span the decisions
in design tickets
[01](../design/static-graph-scheduling/issues/01-deliver-vs-state-boundary.md),
[03](../design/static-graph-scheduling/issues/03-bot-assembly-topology.md),
[05](../design/static-graph-scheduling/issues/05-agent-node-redesign.md), and
[11](../design/static-graph-scheduling/issues/11-graph-io-mechanism.md).
They are intentionally recorded together because partially adopting them
would preserve conflicting name-based and identity-based paths.

## Decision

The static graph scheduling implementation makes these ten coordinated
breaking changes:

1. `Node` has a machine-facing `node_id: str` in addition to its
   human-facing `name`. IDs use the shared `modex_graph.utils.generate_id`
   utility and the `node_` plus 26-character sortable-body format.
2. Both construction seams assign IDs: `NodeRegistry.create` always creates
   one, while `Graph.add_node` creates one only when the node does not already
   have one. Recovery may replace the generated value with the persisted ID.
3. `GraphMetadata` persists an immutable `node_id_map` from node name to node
   ID so a recovered graph reuses the identities from its original instance.
4. `NodeStateStore`, `DeliverStore`, and `GraphPersistenceCoordinator`
   persistence operations use node IDs rather than node names. `Node.run`
   passes `self.node_id` to those operations while lifecycle hooks continue
   to receive `self.name` for readable diagnostics.
5. Topology and scheduler dispatch continue to use graph-local names for edge
   validation. The scheduler converts a target name to its node ID at the
   persistence boundary, and persisted `IntegratedPayload.source_node`
   identifies the source by node ID.
6. SQLite node and delivery state gain node-ID columns and node-ID-based
   uniqueness and indexes. Node names remain readable labels, not persistent
   keys.
7. START and END become registered `Node` instances with their own node IDs.
   `GraphSpecCompiler` creates defaults when a spec omits them and permits
   applications to supply specialized subclasses.
8. Deliveries to END are persisted and consumed normally. Both schedulers run
   START and END through the same invocation lifecycle as every other node;
   the prior END short-circuit is removed.
9. Static graph input and node output use a frozen, strict `GraphPayload`.
   This type is scoped to static graph `StartNode`, `EndNode`, and agent-node
   I/O; the general `Node.deliver` and built-in/ReAct payload contracts remain
   open for their existing typed content.
10. `GraphContext` carries optional `GraphPayload` user input, static graphs
    use `DefaultGraphState` for aggregated END results, and graph completion
    is reported through the structured `GraphOutputAdapter` surface.

## Consequences

- Persistence can distinguish same-named nodes across graph instances and
  preserve identity through crash recovery.
- Declarative and imperative graph construction converge on one ID format
  without changing the `NodeFactory` ABC.
- START and END participate in persistence, delivery, hooks, and recovery;
  schedulers no longer need terminal-node special cases.
- Existing persistence schemas and callers using names as store keys must
  migrate together. There is no compatibility shim or dual-key period.
- Existing persisted development databases must be rebuilt or migrated with
  the node-ID columns and metadata map before the new store contracts run.
- Static graph I/O gains an evolvable schema, while ReAct and generic built-in
  nodes avoid an unrelated global payload migration.
- The implementation must land in dependency order: identity generation and
  injection, store/coordinator contracts, schema and recovery metadata, then
  executable START/END and typed graph I/O.
