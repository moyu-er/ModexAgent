# Memory System Redesign — Config Schema & DreamEngine Triggers & Knowledge Templates

**Date**: 2026-05-27
**Status**: Draft — revised 2026-05-27 per code review
**Scope**: framework/memory, framework/ioc, examples/bot_project

**Prerequisite reading**: The cleanup/archival simplification (completed 2026-05-27) replaced
`MemoryCompressionCoordinator`, `MemoryLifecyclePolicy`, `compression/`, `compaction/`,
`retention/` with a single `cleanup_session()` function.  This spec builds on that foundation.

---

## 1. Overview

Three changes to the memory system:

1. **Config Schema Rename**: `short_term` → `session`, `long_term` split into `archive` + `knowledge`
2. **DreamEngine Dual Trigger**: Time-based + archive count thresholds (min/max)
3. **Knowledge MD Template System**: Default templates with initialization logic

---

## 2. Config Schema Changes

### 2.1 Current State (after cleanup simplification)

```python
# framework/ioc/configs/memory.py (current)
class ShortTermConfig(BaseModel):
    max_messages: int = 100
    max_tokens: int = 100000
    keep_ratio_for_messages: float = 0.4
    keep_ratio_for_token: float = 0.4

class LongTermConfig(BaseModel):
    enabled: bool = False
    init_defaults: bool = True

class MemoryConfig(BaseModel):
    short_term: ShortTermConfig
    long_term: LongTermConfig | None      # gates BOTH archive + knowledge layers
    dream_engine: DreamEngineConfig | None
```

```yaml
# examples/bot_project/config/pools/main.yml (current)
memory:
  short_term:
    max_messages: 200
    max_tokens: 100000
  long_term: {enabled: true}              # forces both archive AND knowledge to exist
```

### 2.2 Problem

`long_term.enabled` is a single flag that gates TWO independent layers (archive and
knowledge).  There is no way to enable archive without knowledge or vice versa.  The
name "long_term" is ambiguous — it does not clearly map to either the archive log or
the SOUL/USER/MEMORY.md files.

### 2.3 Proposed State

```python
class SessionConfig(BaseModel):
    """Session memory: short-term conversation buffer.

    Replaces ShortTermConfig.  These parameters are passed as ``cleanup_config``
    to ``cleanup_session()`` via ``DefaultMemorySystem``.
    """
    max_messages: int = 100
    max_tokens: int = 100000
    keep_ratio_for_messages: float = 0.4
    keep_ratio_for_token: float = 0.4

class ArchiveConfig(BaseModel):
    """Archive memory: compressed history summaries (dual-channel CONTEXT + KNOWLEDGE).

    Replaces the archive-half of LongTermConfig.  Default ``enabled=False``
    preserves the current safe default — archive is only created when the user
    explicitly opts in.
    """
    enabled: bool = False                 # safe default, matches current long_term.enabled
    max_entries: int = 1000
    retained_consumed_pairs: int = 3

class KnowledgeConfig(BaseModel):
    """Knowledge memory: persistent SOUL/USER/MEMORY files.

    Replaces the knowledge-half of LongTermConfig.  A dedicated config section
    with its own enabled flag, independent of ArchiveConfig.
    """
    enabled: bool = False                 # safe default, matches current long_term.enabled
    default_templates_dir: str = "templates/knowledge"

class DreamEngineConfig(BaseModel):
    """Offline archive-to-knowledge consolidation."""
    enabled: bool = False
    interval: int = 600                  # seconds (time-based trigger)
    min_archive_count: int = 5           # skip if fewer archives
    max_archive_count: int = 30          # trigger immediately if exceeded
    max_batch_size: int = 20             # process up to N archives per run

class MemoryConfig(BaseModel):
    session: SessionConfig
    archive: ArchiveConfig | None = None
    knowledge: KnowledgeConfig | None = None
    dream_engine: DreamEngineConfig | None = None
    retention: RetentionConfig
    pending: PendingConfig
    governance: GovernanceConfig | None = None
```

### 2.4 Design Decisions

**Why `archive.enabled` defaults to `False`** (not `True` as previously drafted):
The current `long_term.enabled: False` default means no archive layer without
explicit configuration.  Changing this to `True` would silently enable LLM archive
generation for all agents, including subagents and new users who haven't read the
docs.  This is a breaking behavioral change that must be opt-in.

**Why `archive` and `knowledge` are separate sections** (not a single `long_term`):
These are independent concerns.  A user may want archive summaries without
knowledge consolidation (DreamEngine disabled), or knowledge files without
archive (e.g. static SOUL.md with no history).  A single flag forces both.

**Why no `scope` field in config** (scope is a layer concern, not a user config):
Layer scope (UserScope, SessionScope, etc.) is determined by the framework based on
agent role, not by user YAML.  Main agents use UserScope for archive/knowledge;
subagents use SessionScope for archive.  Users never need to override this.

### 2.5 Migration

**Backward compatibility strategy**: `short_term` and `long_term` key names are
accepted during migration with deprecation warnings logged.  The alias support
lives in a custom Pydantic field validator, not in the model definition.  This
keeps the models clean and the compatibility layer isolated.

**Files to update**:
- `framework/ioc/configs/memory.py` — new Pydantic models + alias validators
- `framework/ioc/factories/memory.py` — update `create_memory()` to read new fields
- `framework/ioc/factories/descriptors.py` — update `build_session_only_memory()`
- `examples/bot_project/bot/service/builders.py` — update `_build_memory_layer_config()`
- `examples/bot_project/config/pools/main.yml` — rename keys
- `examples/bot_project/config/pools/coding.yml` — rename keys

---

## 3. Config Merge Semantics

### 3.1 Current behavior (pool-level + agent-level)

Pool and agent configs have two levels.  The current convention is that the agent
level **overrides** the pool level.  For simple scalar fields this works correctly.
For nested objects, the previous spec proposed "full override (no deep merge)" —
this is problematic because setting `memory.session.max_messages` at the agent
level would also discard the pool-level `memory.governance` config.

### 3.2 Proposed: shallow merge one level deep

```
Pool-level memory:
  session.max_messages = 200
  governance.lossy_compaction = {tool_result_head_chars: 1200}

Agent-level memory:
  session.max_messages = 300
  (governance not specified)

Result:
  session.max_messages = 300      # agent overrides pool
  governance.lossy_compaction = {tool_result_head_chars: 1200}  # inherited from pool
```

Rule: agent-level sub-sections override pool-level sub-sections only when the
entire sub-section is present at the agent level.  Scalar fields within a
sub-section merge one level deep.

### 3.3 YAML examples

**Main agent config** (pool `main.yml`):
```yaml
memory:
  session:
    max_messages: 200
    max_tokens: 100000
    keep_ratio_for_messages: 0.4
  archive:
    enabled: true
  knowledge:
    enabled: true
    default_templates_dir: "templates/knowledge"
  dream_engine:
    enabled: true
    interval: 600
    min_archive_count: 5
    max_archive_count: 30
  governance:
    lossy_compaction:
      tool_result_head_chars: 1200
```

**Coding agent config** (pool `coding.yml`):
```yaml
memory:
  session:
    max_messages: 500
    max_tokens: 150000
    keep_ratio_for_messages: 0.3
  archive:
    enabled: true
  knowledge:
    enabled: true
    default_templates_dir: "templates/knowledge"
  governance:
    lossy_compaction:
      tool_result_head_chars: 2000
```

**Subagent config** (agent-level, no knowledge layer):
```yaml
agents:
  - name: office-expert
    role: subagent
    memory:
      session:
        max_messages: 80
      archive:
        enabled: true
      # NO knowledge section (subagents never have knowledge layer)
      # NO dream_engine section
```

---

## 4. DreamEngine Dual Trigger

### 4.1 Trigger Logic

```
DreamEngine.check_trigger(context):
    archive_count = count_unprocessed_knowledge_entries(context)

    if archive_count < min_archive_count:
        return SKIP  # not enough data to consolidate

    if archive_count > max_archive_count:
        return TRIGGER  # prevent archive bloat

    # Between min and max: check time-based trigger
    time_since_last = now - last_consolidation_time
    if time_since_last >= interval:
        return TRIGGER

    return WAIT
```

### 4.2 Default Values

| Parameter | Default | Rationale |
|-----------|---------|-----------|
| `min_archive_count` | 5 | Prevent consolidation when there's too little data |
| `max_archive_count` | 30 | Force consolidation before archive log grows unbounded |
| `interval` | 600 | 10 min between scans — adequate for background processing |
| `max_batch_size` | 20 | Existing value in `DreamEngine.__init__` |

### 4.3 Integration Points

**Where triggers are checked**:

1. **Periodic scan** (`DefaultMemoryMaintenancePolicy.scan_once()`)
   - Already runs every 300s in bot project (`_maintenance_loop`)
   - Dual trigger check added here: time-based + archive count thresholds
   - This is the PRIMARY trigger path

2. **After archive write** (`cleanup_session()` → archive step)
   - Fire-and-forget: if `archive_count > max_archive_count`, signal that
     consolidation is needed, but do NOT block the conversation loop
   - The next `scan_once()` run picks up the signal

3. **Manual trigger** (future: slash command `/dream`)

**Execution model**: Consolidation runs synchronously inside `scan_once()`.  This
is acceptable because `scan_once` is already in a background `asyncio.Task`.
If consolidation takes longer than `scan_interval`, the next scan cycle is simply
delayed (no concurrent runs — a simple boolean guard prevents overlap).

### 4.4 Data Flow (updated for cleanup simplification)

```
1. User sends message
   └─> SessionMemory.add_messages()

2. ScopedMessageHistory.append() / extend()
   └─> cleanup_session()                    # direct call, no callback/interceptor

3. cleanup_session() checks triggers:
   ├─ message_count > max_messages → TRIGGER
   └─ estimated_tokens > max_tokens → TRIGGER

4. Cleanup (always executes):
   ├─ Sanitize tool chains
   ├─ Compute keep/prune boundary
   └─ session.replace_messages_if_revision(keep)

5. Archive (optional, archive is not None):
   ├─ pruned_messages → archive_strategy.generate()
   └─ archive.append_bundle(context, writes)

6. Periodic maintenance (background asyncio.Task, _maintenance_loop):
   └─ DefaultMemoryMaintenancePolicy.scan_once()
      ├─ Archive retention enforcement (max_entries, max_age_days)
      ├─ Knowledge eviction (stale MEMORY.md)
      └─ DreamEngine trigger check (NEW — dual trigger)
         ├─ archive_count < min → SKIP
         ├─ archive_count > max → TRIGGER immediately
         └─ time_since_last >= interval → TRIGGER

7. DreamEngine consolidation:
   ├─ Read unprocessed KNOWLEDGE entries
   ├─ Batch: summarizer.analyze() → summarizer.summarize()
   ├─ Apply MemoryUpdate to SOUL/USER/MEMORY
   └─ Advance cursor + prune consumed pairs
```

---

## 5. Knowledge MD Template System

### 5.1 Directory Structure

```
examples/bot_project/
├── templates/
│   └── knowledge/
│       ├── SOUL.md          # Default personality template
│       ├── USER.md          # Default user profile template
│       └── MEMORY.md        # Default memory template
└── data/
    └── memory/
        ├── main/            # Per-agent knowledge storage
        │   ├── SOUL.md
        │   ├── USER.md
        │   └── MEMORY.md
        └── coding/
            ├── SOUL.md
            ├── USER.md
            └── MEMORY.md
```

### 5.2 Two Sets of Files

| Set | Location | Purpose | Mutability |
|-----|----------|---------|------------|
| **Templates** | `templates/knowledge/` | Read-only defaults | Version controlled, manually edited |
| **Active** | `data/memory/{agent}/` | Maintained by DreamEngine | Auto-updated, user-editable |

### 5.3 Initialization Logic

Called in `KnowledgeMemoryManager.ensure_defaults()` on first retrieval:

```python
async def ensure_defaults(self, context, default_templates_dir=None):
    for logical_key, filename in self._config.default_files.items():
        existing = await self.get_file(context, filename)
        if existing is not None and existing.strip():
            continue  # Already populated, don't overwrite

        content = ""
        if default_templates_dir:
            template_path = Path(default_templates_dir) / filename
            if template_path.exists():
                content = template_path.read_text(encoding="utf-8")

        await storage.set(filename, content)
```

---

## 6. Layer Defaults Per Agent Role

### 6.1 Default Scope Rules

Scope is determined by the framework, not by user config:

| Layer | Main Agent | Subagent | Rationale |
|-------|-----------|----------|-----------|
| `session` | `SessionScope` | `SessionScope` | Always per-session |
| `archive` | `UserScope` | `SessionScope` | Main shares history; subagent is isolated |
| `knowledge` | `UserScope` | N/A (disabled) | Main shares knowledge; subagent has none |

Subagent knowledge is always disabled — `build_session_only_memory()` sets
`knowledge=None` regardless of config.  The factory enforces this, not the config.

### 6.2 Knowledge Files Per-Agent vs Per-User

Knowledge MD files are stored per-agent to avoid conflicts between agents with
different roles/domains (e.g. "coding" agent's MEMORY.md contains code patterns,
"creative" agent's MEMORY.md contains design preferences).  They are organized
under `data/memory/{agent_name}/` subdirectories.

This means multiple agents under the same user share a `UserScope` but write to
different files.  The scope determines storage resolution, not file content.
Each agent gets its own SOUL.md (personality/role-specific), but USER.md could
be shared if the agents share a user-scoped knowledge storage and the same
filename.  This is acceptable because:
- SOUL.md is per-agent personality — naturally different per agent
- USER.md is user profile — naturally shared
- MEMORY.md is domain knowledge — varies by agent role

---

## 7. Implementation Phases

Phases ordered to minimize compatibility debt:

### Phase 1: Dual Trigger + Template System (NO config rename)

1. Add `min_archive_count`, `max_archive_count`, `max_batch_size` to existing `DreamEngineConfig`
2. Add dual trigger check to `DreamEngine.run()` and `DefaultMemoryMaintenancePolicy.scan_once()`
3. Add `default_templates_dir` to existing `LongTermConfig`
4. Add template initialization to `KnowledgeMemoryManager.ensure_defaults()`
5. Add templates to `examples/bot_project/templates/knowledge/`

**No config key rename yet** — so no backward compatibility burden.

### Phase 2: Config Schema Rename (batch change)

1. Rename all config classes: `ShortTermConfig` → `SessionConfig`, split `LongTermConfig` → `ArchiveConfig` + `KnowledgeConfig`
2. Update `MemoryConfig` fields
3. Update `create_memory()` and `build_session_only_memory()` factories
4. Update all YAML pool configs
5. Add deprecation alias validator (accept old keys, warn)
6. Update all tests

**One atomic change** — no prolonged period of dual-key maintenance.

### Phase 3: Deprecation Removal (2 minor versions later)

Remove alias validators, drop support for old key names.

---

## 8. Testing Strategy

### Unit Tests

1. **Config migration**: test that `short_term`/`long_term` aliases work during migration, emit warnings
2. **DreamEngine triggers**: test skip (below min), trigger (above max), time-based fallback
3. **Knowledge templates**: test copy on missing, no-op on existing, empty fallback

### Integration Tests

1. **End-to-end**: session cleanup → archive write → maintenance scan → DreamEngine trigger → knowledge update
2. **Subagent isolation**: verify subagent has no knowledge layer, archive uses SessionScope

---

## 9. Success Criteria

- [ ] Archive + Knowledge config split from single `long_term` flag
- [ ] DreamEngine dual trigger (time + archive count)
- [ ] Knowledge MD template system
- [ ] `archive.enabled` defaults to `False` (safe default)
- [ ] `knowledge.enabled` defaults to `False` (safe default)
- [ ] Subagents isolated from knowledge layer (factory-enforced)
- [ ] Scope determined by framework, not user config
- [ ] Config rename in single atomic phase, no prolonged dual-key support
- [ ] Deep merge for nested config fields
- [ ] All tests passing
