# Memory Redesign Spec — Review Corrections

**Date**: 2026-05-27
**Related**: `docs/superpowers/specs/2026-05-27-memory-redesign-design.md`

## 1. Prerequisite: What Changed Before This Spec

The original spec was written before the cleanup/archival simplification (also
completed 2026-05-27).  That simplification made the following changes that the
original spec was unaware of:

| Removed | Replaced by |
|---------|-------------|
| `MemoryCompressionCoordinator` + all `compression/` code | `cleanup_session()` in `framework/memory/cleanup.py` |
| `MemoryLifecyclePolicy` callback injection | Direct call from `ScopedMessageHistory.append/extend` |
| `compaction/` directory (policy, boundary) | Inline `_compute_boundary` in `cleanup.py` |
| `retention/` directory (policy, default, config) | Inline priority logic in `cleanup.py` |
| `auto_compact` config flag | Removed entirely — archive behavior controlled by `archive is not None` |
| `MEMORY_OPERATION` from `InterceptorScope` | Removed — memory is not an interceptor concern |
| `compression_coordinator` from `DefaultMemorySystem` | Replaced by `archive_strategy` + `cleanup_config` |
| `lifecycle_policy` from `create_memory_system()` | Replaced by `cleanup_config` + `maintenance_policy` |
| `_auto_compact_task` in bot project | Renamed to `_maintenance_task` |
| `DefaultMemoryLifecyclePolicy` | Deleted completely |

The original spec referenced many of these deleted components.  The corrected spec
now accurately reflects the current codebase.

## 2. Correction: ArchiveConfig.enabled defaults to False

**Original spec**: `enabled: bool = True`

**Corrected**: `enabled: bool = False`

**Reason**: The current default `LongTermConfig.enabled = False` means no archive
layer is created unless the user explicitly opts in with `long_term: {enabled:
true}`.  Changing to `True` would silently enable LLM archive generation for
every agent — including subagents and new projects with empty configs.  This is
unacceptable for cost and latency.  The default must remain opt-in.

## 3. Correction: KnowledgeConfig Contradiction Resolved

**Original spec**: Two contradictory statements:
- Line 82: `knowledge: KnowledgeConfig | None = None  # main agents only`
- Section 5.5 table: "main agent default: UserScope"

**Corrected**: Both `archive` and `knowledge` default to `None`/disabled in
`MemoryConfig`.  Whether knowledge is enabled for a specific agent is determined
by the factory logic (`create_memory()` or `build_session_only_memory()`), which
respects:
- Config value if explicitly set
- Role-based default: enabled for main agents with `knowledge.enabled: true`,
  disabled for subagents regardless of config

The factory, not the config model, enforces the "subagent has no knowledge" rule.

## 4. Correction: Config Merge Semantics

**Original spec**: "Agent-level memory config fully overrides pool-level memory
config (no deep merge)"

**Corrected**: One-level shallow merge.  Scalar fields inherit from pool level
when not specified at agent level.  This prevents the surprise of setting
`memory.session.max_messages` at agent level and silently losing pool-level
`governance` config.

**Example of why full override fails**: If pool level has detailed `governance`
config (200 characters of YAML) and the agent only overrides `max_messages`,
full override would discard the entire governance pipeline for that agent.

## 5. Correction: Phase Order Reversed

**Original spec**: Phase 1 = config rename, Phase 2 = DreamEngine triggers

**Corrected**: Phase 1 = DreamEngine triggers + template system (no rename),
Phase 2 = single atomic config rename.

**Reason**: Doing the config rename first requires maintaining backward
compatibility aliases across all subsequent phases.  The spec proposed "2 minor
versions" of dual-key support.  This creates maintenance burden for every piece
of code that reads config.  Instead, implement all functional changes on the old
schema, then rename everything in one batch.

## 6. Correction: Scope Is Not a User Config Field

**Original spec**: `scope: str = "user"` in ArchiveConfig and KnowledgeConfig,
with a `_resolve_scope()` function to map strings to ABCs.

**Corrected**: Scope is determined by the framework based on agent role, not by
user YAML.  The scope-to-ABC mapping (`"user"` → `UserScope()`) would need to
know about every scope subclass, creating a maintenance burden.  Instead:

```python
# In factory code, not user config:
if agent_role == "main":
    archive_scope = UserScope()
    knowledge_scope = UserScope()
else:  # subagent
    archive_scope = SessionScope()
    knowledge_scope = None  # disabled
```

## 7. Correction: Per-Agent vs Per-User Knowledge Storage

**Original spec**: Directory layout shows `data/memory/{agent}/SOUL.md` per agent,
but also says scope is `UserScope` (shared across agents).

**Corrected**: Clarified in Section 6.2.  `UserScope` determines storage
resolution, not file isolation.  Multiple agents under the same user share a
`UserScope` storage but write to different file keys.  SOUL.md is per-agent
(agent-specific personality), USER.md is shared (same user across agents),
MEMORY.md varies by agent domain but may overlap.

## 8. Updated Data Flow Diagram

**Original spec**: Referenced `MemoryLifecyclePolicy.on_messages_added()`,
`MemoryCompressionCoordinator.maybe_compress()`, and
`DefaultMemoryLifecyclePolicy`.

**Corrected**: Now shows the actual flow:
```
ScopedMessageHistory.append()
  └─ cleanup_session()           # direct call, no callback/interceptor
       ├─ sanitize → boundary → commit  (always)
       └─ archive (optional, when archive exists)
            └─ archive_strategy.generate()

Background:
  _maintenance_loop (asyncio.Task)
    └─ DefaultMemoryMaintenancePolicy.scan_once()
         └─ DreamEngine trigger + knowledge consolidation
```
