# Memory retention thresholds stay per-scope polymorphic

The memory maintenance layer keeps `ArchiveRetentionPolicy` and
`KnowledgeRetentionPolicy` as ABCs (with their `Default*` implementations)
rather than collapsing them into a single flat `MemoryMaintenanceConfig`.
`DefaultMemoryMaintenancePolicy.scan_once` calls
`self._archive_retention.get_max_entries(ctx)` — retention thresholds vary by
`MemoryContext`/scope, so a context-free flat config would silently drop that
capability. Only the dead ABCs (`MemoryMaintenancePolicy`, the always-False
`SessionRetentionPolicy`) are deleted; the two real seams stay.

## Considered Options

1. **Keep the two retention seams (chosen).** Retention logic remains a
   polymorphic seam keyed on `MemoryContext`. Preserves per-scope thresholds;
   costs four small classes.

2. **Collapse all four retention ABCs into a flat config.** Simpler surface,
   but `scan_once` would lose its `ctx`-keyed threshold lookup. Rejected: it
   silently regresses a real capability, and the "one real adapter per ABC"
   argument is weak today but the *per-scope variation* is the real reason to
   keep them — not adapter count.

## Consequences

- A future "collapse the retention ABCs" cleanup pass must preserve the
  per-`MemoryContext` threshold lookup, not flatten it to module-level scalars.
- Note: `framework/multi_agent/pool.SessionRetentionPolicy` is an unrelated
  class (subagent task-session cleanup) and is not affected by this decision.
