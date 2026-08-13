# Graph Instance Re-invocation via Version Chain + IORecord Version-scoping + Spec Immutability

## Context

The graph scheduling subsystem (`modex_graph` + `GraphOrchestrator`) models graph execution as a two-level version chain: `GraphInstanceStore` holds per-instance versions, `NodeStateStore` holds per-node versions. `GraphIORecordStore` holds input/output records. The original design assumed one **Graph Instance** per spec execution — `runGraph` always created a new instance. The WebUI's `GraphConversation` component treated each run as a chat bubble, creating a new instance every time the user sent input.

This created an information-architecture mismatch: users expected "send input to a graph" to work like a conversation (continue the same instance), but the system created a new instance each time. The WebUI showed chat bubbles instead of a topology view, and there was no way to continue a completed/failed instance with new input.

A second, related mismatch was discovered during the WebUI redesign: `GraphInstance` stores only a `spec_id` foreign key — no spec content snapshot. `GraphSpecStore.save` used `ON CONFLICT (name, version) DO UPDATE`, meaning spec edits **overwrote** the existing spec record in place. When `run_instance` re-invoked an instance, it called `self._load_spec(latest.spec_id)` which returned the *modified* spec, not the spec the instance was originally created with. This caused `node_id_map` mismatches (the instance's frozen `{name: node_id}` map no longer matched the recompiled graph's node IDs) — a correctness bug, not just a display issue.

## Decision

Three convergent changes:

### 1. Graph Instance is reusable (version chain re-invocation)

A completed/failed instance can be re-invoked by calling `GraphInstanceStore.begin_invocation(gid)`, which creates a new version (N+1) on the same `graph_instance_id`. The new version executes the graph from `entry_node` as a fresh execution. The old version's records (graph + node) remain as history; the version number ordering (`ORDER BY version DESC LIMIT 1`) naturally makes `load_latest` return the new version — no explicit soft-delete field is needed.

`bootstrap` removes the `has_any_invocation` gate for re-invocation: when seeds is empty (no CRASHED/RUNNING nodes, no PENDING delivers), bootstrap checks the instance status — if RUNNING (`begin_invocation` just set it), return `[entry_node]` unconditionally; if terminal, return `[]`; if Null store, fall back to the `has_any_invocation` check.

### 2. GraphIORecord version-scoped

`GraphIORecord` gains a `version: int` field aligned with `GraphMetadata.version`. `run_instance` creates a new `GraphIORecord` on each `begin_invocation` (not just on `create_instance`). `get_by_instance` is replaced by `list_by_instance` (returns all versions) or `get_latest_by_instance` (returns the active version). This makes each invocation's input/output independently queryable — the "conversation history" of a graph instance.

### 3. Spec is immutable (content-addressed by spec_id)

**A spec record is never overwritten.** Each save with changed content creates a new row with a new Snowflake `spec_id`. `GraphInstance.spec_id` is therefore a content snapshot reference — it points to the exact spec the instance was created with, for the lifetime of the instance. Re-invocation (`run_instance`) calls `self._load_spec(latest.spec_id)` and gets the original spec, not a modified version.

`GraphSpecStore` changes:

- **`save(spec)` → always INSERT** a new row with a new Snowflake `spec_id`. The `ON CONFLICT (name, version) DO UPDATE` is removed. The `UNIQUE (name, version)` constraint is removed from the `graph_specs` table — multiple rows with the same `(name, version)` are allowed, distinguished by `spec_id` (Snowflake, time-ordered).
- **`save_if_changed(spec)` → content-deduplicated save** (new method). Compares the spec's JSON serialization against the latest existing row for the same `name` (via `MAX(spec_id) WHERE name = ?`). If identical, returns the existing `spec_id` (idempotent — no new row). If different or no prior row exists, INSERTs a new row and returns the new `spec_id`. This method is used by both the startup `GraphSpecLoader` (YAML files → store, idempotent on unchanged files) and the WebUI `PUT /specs/{id}` handler (user edits → new `spec_id` when content changes).
- **`list_records()` → latest per name.** Returns only the newest `spec_id` for each `name`: `SELECT ... WHERE spec_id IN (SELECT MAX(spec_id) FROM graph_specs GROUP BY name)`. Snowflake IDs are time-ordered, so `MAX(spec_id)` is the most recently inserted row for that name. Historical specs (older `spec_id` values for the same name) are accessible only via `get_by_id(spec_id)` / `load_by_id(spec_id)` — they do not appear in the list. This is the "one spec per name" view the WebUI spec list presents.
- **`version` field becomes a display label**, not a uniqueness key. Users may set any version string in YAML; it carries no enforcement. The Snowflake `spec_id` is the sole identity.

`GraphSpecEditor` (WebUI): after `PUT /specs/{id}` returns a potentially new `spec_id`, the frontend navigates to `/graphs/{new_spec_id}` so the URL always reflects the current spec. The old `spec_id` remains accessible via its instances.

`handle_put_spec` (REST route): removes the `if spec.name != record.name or spec.version != record.version: error` immutability check. Name and version are display labels, freely editable. The route calls `save_if_changed` and returns the (possibly new) `spec_id` in the response.

## Why

### Why spec immutability (change 3)

The original design assumed `spec_id` pointed to immutable content — `run_instance`'s `self._load_spec(latest.spec_id)` was written under that assumption. But `GraphSpecStore.save`'s `ON CONFLICT DO UPDATE` violated it: spec edits overwrote the row in place, so `spec_id` became a mutable reference. This caused two concrete bugs:

1. **Re-invocation correctness bug**: `run_instance` recompiles from the (now modified) spec, producing new `node_id` values. But `GraphMetadata.node_id_map` was frozen at `create_instance` time with the original node IDs. Line 318 (`node.node_id = latest.node_id_map[node.name]`) either KeyErrors (if a node was removed) or maps to wrong IDs (if nodes were added/reordered).
2. **Instance detail topology mismatch**: the WebUI's instance detail view loads the spec via `getSpec(instance.spec_id)` to render the topology. If the spec was modified, the displayed topology does not match the one the instance actually executed.

Spec immutability fixes both: `spec_id` is a content snapshot reference, `run_instance` loads the original spec, and the WebUI renders the correct topology. No schema change to `GraphMetadata` or `GraphInstance` is needed — the existing `spec_id` FK naturally becomes a snapshot pointer.

### Why `save_if_changed` (not always INSERT)

Startup `GraphSpecLoader` loads YAML files into the store on every boot. Without content dedup, each boot would create a new `spec_id` for every spec, orphaning all prior instances. `save_if_changed` makes startup idempotent: unchanged YAML → same `spec_id`, instances survive restarts. The WebUI save path also benefits: if the user saves without changes (e.g., formatting only), no new `spec_id` is created.

### Why remove `UNIQUE(name, version)`

With spec immutability, the same `(name, version)` pair naturally has multiple rows over time (one per content change). Enforcing uniqueness would require version bumping on every edit — friction that adds no value when `spec_id` (Snowflake, time-ordered) is the sole identity. Removing the constraint lets `save_if_changed` create new rows without collision. The `version` field remains as a user-facing label (like a git tag), not a uniqueness key.

## Considered Options

### Re-invocation mechanism (changes 1–2)

- **`NodeStateStore.clear()` on re-invocation.** Deletes all node states for the instance before re-execution. Simpler (no schema change), but destroys node-level history (timeline, crashed node records). Rejected because the WebUI's instance detail view shows per-invocation node execution traces, and clearing them would make historical debugging impossible. The version chain already preserves history implicitly — old versions stay in the table, `load_latest` returns the newest.

- **Version-scoped `NodeStateStore` (add `instance_version` column).** Node states keyed by `(graph_instance_id, node_id, node_version, instance_version)`. Queries filter by instance version. Rejected because it requires every `NodeStateStore` query to carry an `instance_version` parameter — a pervasive API change across `bootstrap`, `Node.run()`, `persistence_coordinator`, and all three store implementations. The existing version chain (`MAX(version)+1`) already provides isolation — re-invocation creates new versions that naturally supersede old ones via ordering.

- **`superseded` field on `graph_instances` and `node_states`.** An explicit boolean column set to 1 when a new version supersedes the old. Queries filter `WHERE superseded = 0`. **Deferred** — the version number ordering already provides the same semantics for correctness. Revisit when version chains grow to thousands per instance; at that point add `superseded` + partial index `WHERE superseded = 0` on both tables as a pure query optimization.

- **`bootstrap` entry_node fallback in `ParallelScheduler`.** Mirror `LinearScheduler`'s `seeds[0] if seeds else entry_node` fallback. Rejected because it treats the symptom (empty seeds) not the cause (bootstrap not returning `[entry_node]` for re-invocation).

### Spec snapshot mechanism (change 3)

- **Path B: instance stores spec snapshot field.** `GraphMetadata` gains `spec_yaml: str` (or `spec_snapshot: GraphSpec`). `create_instance` stores the spec content. `run_instance` loads from metadata, not from `spec_store`. Rejected because it requires a `GraphMetadata` schema change (new column in `graph_instances` table), a new `run_instance` code path, and duplicates spec content across every instance. Path A (spec immutability) achieves the same result with zero `GraphMetadata`/`GraphInstance`/`GraphInstanceStore` changes — `spec_id` FK naturally becomes a snapshot pointer when the spec row is immutable. Path A is strictly more convergent: the change is isolated to `GraphSpecStore`, and every consumer of `spec_id` (orchestrator, recovery, WebUI) gets correct behavior with no code change.

- **Keep `ON CONFLICT DO UPDATE` + add `spec_version` to `GraphMetadata`.** Track which version of a mutable spec the instance was created with. Rejected — this couples instance lifecycle to spec versioning, requires schema change, and still needs spec history retention (can't overwrite if old versions are referenced). Spec immutability (Path A) subsumes this: `spec_id` is the version, no separate field needed.

## Consequences

- `bootstrap` no longer applies the `has_any_invocation` gate for the re-invocation path — when seeds is empty and instance status is RUNNING, it returns `[entry_node]` unconditionally. For terminal status (recovery), it returns `[]`. For Null stores (no instance metadata), the `has_any_invocation` fallback remains.
- `LinearScheduler`'s `seeds[0] if seeds else entry_node` fallback (line 85) becomes redundant for the re-invocation case (bootstrap now returns `[entry_node]`). It remains as a safety net.
- The `GraphIORecordStore` ABC gains `list_by_instance` as the primary query (already exists); `get_by_instance` (returns single, `LIMIT 1`) is deprecated or removed.
- No schema changes to `graph_instances` or `node_states` — the existing version chain (`MAX(version)+1`, `ORDER BY version DESC LIMIT 1`) is the superseding mechanism, uniformly at both layers.
- **`graph_specs` table**: `UNIQUE (name, version)` constraint removed. Multiple rows with the same `(name, version)` are allowed; `spec_id` (Snowflake, time-ordered) is the sole primary key and identity. The `idx_graph_specs_name` index remains. The `trg_graph_specs_auto_updated_at` trigger is removed (immutable rows are never UPDATEd — `updated_at` equals `created_at`).
- **`GraphSpecStore.save`** always INSERTs; `save_if_changed` is the content-deduplicated variant used by startup loader and WebUI save.
- **`GraphSpecStore.list_records`** returns only the latest `spec_id` per `name` (MAX(spec_id) GROUP BY name). Historical specs are accessible only via `get_by_id` / `load_by_id` — they do not appear in the list. The WebUI spec list naturally shows "one spec per name".
- **`GraphInstance.spec_id`** is a content snapshot reference. Re-invocation, crash recovery, and WebUI topology rendering all load the original spec via `spec_id`. No `spec_yaml` or `spec_snapshot` field is added to `GraphMetadata`.
- **`GraphSpecEditor`** (WebUI): after saving, if `spec_id` changed, the frontend navigates to the new `spec_id`. The old `spec_id` remains accessible via its instances.
- The WebUI's `GraphConversation` component is replaced by a spec-detail view (topology preview + instance list + new-instance composer) and an instance-detail view (conversation flow + continue-invocation composer + topology popover). The old `runGraph`/`getRuns` API names are supplemented by `invokeInstance`/`getInvocations` to align with the **Graph Invocation** term. See `docs/design/graph-visualization-redesign/PRD.md` for the full WebUI information architecture.

## Deferred (not in this ADR's scope)

- **`superseded` field as query optimization.** A `superseded INTEGER NOT NULL DEFAULT 0` column on `graph_instances` and `node_states`, set to 1 by `begin_invocation` on the prior version. All "load active" queries filter `WHERE superseded = 0`. Not needed now — current scale (tens to hundreds of versions) has no performance issue. Revisit when version chains reach thousands per instance.

- **Running-state dirty data cleanup.** When the process is killed mid-execution, the instance status stays `running` but no execution is active. A periodic cleanup task should mark stale `running` instances (heartbeat timeout) as `crashed`, making them re-invokable. Not addressed now.

- **`rebuild_main_state` on re-invocation.** `bootstrap` step 1 calls `coordinator.rebuild_main_state()` which restores `ctx.state` from the newest COMPLETED node record's `state_json`. On re-invocation, this restores the prior invocation's final state. Whether re-invocation should carry prior state forward or start fresh is an implementation detail to verify.

- **Orphaned spec GC.** Historical spec rows (no instance references them, and they are not the latest for their name) accumulate over time. A cleanup mechanism (reference counting or periodic sweep) is not addressed now — current scale (tens of specs) has no storage issue. Revisit when spec edit history grows large.

- **`list_records` performance with many historical rows.** `MAX(spec_id) GROUP BY name` is O(log n) per name via the `idx_graph_specs_name` index. At current scale (tens of specs per name) this is negligible. If a single name accumulates thousands of historical rows (frequent editing), consider a `latest_spec_id` materialized view or a `is_latest` flag. Not needed now.
