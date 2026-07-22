# Core Memory Rename — Exhaustive Inventory

> **Status**: Planning — execute as a prerequisite task before KnowledgeBase implementation.
> **Source ADR**: [0035-core-memory-and-knowledge-base-terminology-split.md](../../adr/0035-core-memory-and-knowledge-base-terminology-split.md)
> **Strategy**: PyCharm's structural Rename (Shift+F6) on each symbol below, then grep-verify for stragglers. YAML keys and string literals need manual edits.

## Execution checklist

This is the recommended order — each step is independently testable.

### Step 1 — Enum / constant renames (foundation, run first)

These propagate to switch/case branches and string comparisons.

- [ ] `src/modex_agent/core/scope.py` line 42: `KNOWLEDGE = "knowledge"` → `CORE = "core"` (constant value also changes — used as dict key)
- [ ] `MemoryLayerName.KNOWLEDGE` enum value → `MemoryLayerName.CORE` (find class definition; update string value too)
- [ ] `ArchiveChannel.KNOWLEDGE` enum value → `ArchiveChannel.CORE` (in `archive_models.py`)
- [ ] `KnowledgeTag` StrEnum class → `CoreMemoryTag` (XML tag *values* `your_identity` / `user_profile` / `known_facts` stay — only the class name changes)

### Step 2 — Class / dataclass renames

Use PyCharm Rename Refactor (preserves call sites).

- [ ] `KnowledgeMemoryManager` (ABC, in `memory/core/layers.py`) → `CoreMemoryManager`
- [ ] `KnowledgeMemoryConfig` (in `memory/layers/config.py`) → `CoreMemoryConfig`
- [ ] `KnowledgeSearchStrategy` + `FullDumpKnowledgeStrategy` (in `memory/knowledge_search.py`) → `CoreMemorySearchStrategy` + `FullDumpCoreMemoryStrategy`
- [ ] `KnowledgeConfig` (Pydantic, in `ioc/configs/memory.py`) → `CoreMemoryConfig`
- [ ] `KnowledgeConsolidator` (in `agents/summarizer/consolidator.py`) → `CoreMemoryConsolidator`
- [ ] `LongTermMemory` (dataclass in `memory/core/models.py`) → `CoreMemoryContents`
- [ ] `KnowledgePromptBuilder` → `CoreMemoryPromptBuilder` (locate via grep)
- [ ] `KnowledgeRetentionPolicy` (in `memory/lifecycle.py`) → `CoreMemoryRetentionPolicy`
- [ ] `ScopedKnowledgeMemoryManager` (in `memory/layers/knowledge.py`) → `ScopedCoreMemoryManager`

### Step 3 — Module / file renames (PyCharm Safe Move)

- [ ] `src/modex_agent/memory/layers/knowledge.py` → `src/modex_agent/memory/layers/core.py`
- [ ] `src/modex_agent/memory/knowledge_search.py` → `src/modex_agent/memory/core_memory_search.py`
- [ ] `examples/bot_project/templates/knowledge/` → `examples/bot_project/templates/core/` (directory move — also update any code referencing the literal path)
- [ ] Update `src/modex_agent/memory/layers/__init__.py` and `src/modex_agent/memory/__init__.py` exports

### Step 4 — Field / variable renames (PyCharm Rename, verify each)

- [ ] `MemoryConfig.knowledge` field → `MemoryConfig.core` (in `ioc/configs/memory.py`)
- [ ] `MemoryConfig.knowledge_max_chars` → `MemoryConfig.core_max_chars` (line ~169)
- [ ] `archive_models.py` field renames (5 places):
  - [ ] `KNOWLEDGE_ARCHIVE_FILE_KEY = "knowledge_archive"` → `CORE_ARCHIVE_FILE_KEY = "core_archive"`
  - [ ] `KNOWLEDGE_ARCHIVE_FILENAME = "knowledge_archive.jsonl"` → `CORE_ARCHIVE_FILENAME = "core_archive.jsonl"`
  - [ ] `knowledge_consumed_archive_id` field → `core_consumed_archive_id`
  - [ ] `knowledge_messages` field → `core_messages`
  - [ ] `knowledge_transcript` field → `core_transcript`
  - [ ] `knowledge: str` field on the `documents` dataclass → `core: str`
  - [ ] `summary=self.documents.knowledge` accessor (line 98) → `summary=self.documents.core`
- [ ] Rename all `*knowledge*` local variables and parameters across the codebase (PyCharm structural Find + Replace with scope = "in code")

### Step 5 — String literals & comments

PyCharm's refactor will NOT touch these. Manual edits required.

#### Docstrings & comments

- [ ] `ioc/configs/memory.py` line 58: docstring `"Long-term knowledge files (SOUL.md / USER.md / MEMORY.md)."` → `"Core memory files (SOUL.md / USER.md / MEMORY.md)."`
- [ ] `ioc/configs/memory.py` line 66: docstring `"Offline archive-to-knowledge consolidation."` → `"Offline archive-to-core-memory consolidation."`
- [ ] `ioc/configs/memory.py` line 113: comment `"# trigger knowledge update when this many undigested"` → `"# trigger core memory update when this many undigested"`
- [ ] `ioc/configs/memory.py` line 181: docstring `"- archive/knowledge: off"` → `"- archive/core: off"`
- [ ] `ioc/configs/memory.py` line 222: comment `"Migrate long_term → archive + knowledge"` — **leave as-is** (historical context)
- [ ] `ioc/configs/memory.py` line 225: warning string `"MemoryConfig.long_term is deprecated, use archive and knowledge instead"` — **leave as-is** (historical, only fires for very old configs)
- [ ] All other docstring occurrences of "knowledge" inside `src/modex_agent/` (run `grep -r "knowledge" --include="*.py" src/modex_agent` after Step 4 to enumerate)
- [ ] All `logger.info` / `logger.debug` messages mentioning "knowledge" → "core memory"

#### Persistent state file names (BREAKING — these are file paths on disk)

- [ ] `"knowledge_archive"` file key (became `core_archive` in Step 4) — **impact**: existing archive files on disk named `knowledge_archive.jsonl` will be orphaned. Document in migration guide; provide a one-shot rename helper for users:
  ```bash
  # User-side migration (document in CHANGELOG)
  mv <workspace>/memory/<pool>/archive/knowledge_archive.jsonl <workspace>/memory/<pool>/archive/core_archive.jsonl
  ```
  (Or write a small Python migration helper in `examples/bot_project/` if user wants.)

### Step 6 — Test updates

PyCharm refactor will rename test symbols automatically. Manual review:

- [ ] `tests/unit/memory/test_knowledge_layer.py` → `tests/unit/memory/test_core_memory_layer.py` (file rename)
- [ ] `tests/unit/memory/test_knowledge_templates.py` → `tests/unit/memory/test_core_memory_templates.py`
- [ ] `tests/unit/memory/test_knowledge_directory_injection.py` → `tests/unit/memory/test_core_memory_directory_injection.py`
- [ ] `tests/unit/memory/test_overwrite_knowledge_update.py` → `tests/unit/memory/test_overwrite_core_memory_update.py`
- [ ] `tests/unit/memory/core/test_layers.py` — verify all `*Knowledge*` symbol references updated
- [ ] `tests/unit/memory/test_layer_factory_build.py` — verify
- [ ] `tests/unit/memory/test_layer_factory.py` — verify
- [ ] `tests/unit/ioc/test_memory_factory.py` — verify
- [ ] `tests/unit/ioc/test_memory_config_migration.py` — verify; the `long_term → archive + knowledge` migration test stays unchanged (historical), but any `knowledge` field assertions need updating to `core`
- [ ] `tests/integration/memory/test_phase1_integration.py` — verify
- [ ] All tests under `examples/bot_project/tests/` that reference `knowledge` symbols or YAML keys
- [ ] Run `pytest tests/unit/memory/ -v` and `pytest tests/unit/ioc/ -v` after the rename to catch stragglers
- [ ] Run `pytest tests/architecture/` — the architecture guard tests may reference symbol names

### Step 7 — Business-layer example updates

`examples/bot_project/` is a full application. Path X means it must be migrated too.

- [ ] `examples/bot_project/bot/config/memory_defaults.py` — update `knowledge:` references to `core:`
- [ ] `examples/bot_project/tests/unit/config/test_target_state_memory_experience.py` — update
- [ ] `examples/bot_project/tests/test_ensure_long_term_defaults.py` — verify (the filename says "long_term" but contents likely reference current API)
- [ ] `examples/bot_project/templates/core/` (after directory move) — verify templates still load
- [ ] `examples/bot_project/templates/AGENTS.md` — update narrative references
- [ ] `examples/bot_project/AGENTS.md` — update narrative references

### Step 8 — Documentation updates

- [ ] **Root `CONTEXT.md`** — add `**Core Memory**` and `**KnowledgeBase**` terms (already patched separately, see ADR-0035)
- [ ] **Root `AGENTS.md`** — replace "knowledge layer" with "core memory layer"; update memory rules section
- [ ] **`src/modex_agent/memory/README.md`** — full terminology pass (this is the canonical memory doc; references `KnowledgeSearchStrategy` as extension point — update to `CoreMemorySearchStrategy`)
- [ ] **`src/modex_agent/memory/AGENTS.md`** — full terminology pass
- [ ] **`src/modex_agent/memory/layers/AGENTS.md`** — full pass
- [ ] **`src/modex_agent/memory/core/AGENTS.md`** — full pass (careful: `core/` directory is the *memory core ABCs*, not "core memory" — disambiguate in prose to avoid confusion)
- [ ] **`src/modex_agent/memory/prompts/AGENTS.md`** — update
- [ ] **`src/modex_agent/memory/consolidation/AGENTS.md`** — update
- [ ] **`src/modex_agent/agents/summarizer/AGENTS.md`** — update
- [ ] **`src/modex_agent/agents/AGENTS.md`** — update
- [ ] **`src/modex_agent/AGENTS.md`** — module table mentions "session/archive/knowledge" → "session/archive/core"; the "Three-layer memory" phrasing stays (it's still three layers)
- [ ] **`src/modex_agent/pipeline/AGENTS.md`** — update
- [ ] **ADR reviews** — only ADRs that *describe* the layer as a design decision need updating; ADRs that *mention* the layer incidentally can stay as historical record:
  - [ ] `docs/adr/0002-keep-per-scope-memory-retention-seams.md` — likely describes the layer; update terminology in prose
  - [ ] `docs/adr/0023-hybrid-persistence-sqlite-plus-file.md` — same
  - [ ] `docs/adr/0033-generalized-graph-engine.md` — incidentally mentions; add note pointing to ADR-0035
  - [ ] `docs/adr/0034-graph-engine-phase-c-preliminaries.md` — verify
  - [ ] All ADRs written *after* ADR-0035 should use the new terminology
- [ ] **`docs/design/`** — design docs that reference the layer
  - [ ] `docs/design/generalized-graph-engine/issues/06-summarizer-agent-removal.md` — update
  - [ ] `docs/design/hybrid-persistence/SCHEMA-DESIGN.md` — verify (may reference `MemoryLayerName.KNOWLEDGE`)
  - [ ] `docs/design/hybrid-persistence/PRD.md` — verify
  - [ ] `docs/design/hybrid-persistence/tickets.md` — verify
  - [ ] `docs/design/config-ux-overhaul/PRD.md` — verify
  - [ ] `docs/design/external-coding-agent-integration/deferred.md` — verify
  - [ ] `docs/design/cross-pool-peer-communication/PRD.md` — verify
  - [ ] `docs/design/cross-pool-peer-communication/tickets.md` — verify
  - [ ] `docs/design/session-gc/PRD.md` — verify
  - [ ] `docs/design/session-gc/PLAN.md` — verify

### Step 9 — YAML config migration guide

For users who have existing configs:

- [ ] Add a section to root `CHANGELOG.md` (create if absent) titled `## Breaking — Core Memory rename (ADR-0035)`:
  ```markdown
  ## Breaking — Core Memory rename (ADR-0035)

  The `memory.knowledge:` YAML section is renamed to `memory.core:`.

  **Before:**
  ```yaml
  memory:
    knowledge:
      enabled: true
  ```

  **After:**
  ```yaml
  memory:
    core:
      enabled: true
  ```

  To migrate: rename the YAML key in your config. No data migration needed
  for SOUL.md / USER.md / MEMORY.md — file names are unchanged. If you use
  archive with the `KNOWLEDGE` channel, see `docs/design/core-memory-rename/`
  for the file rename helper.
  ```
- [ ] Update `examples/bot_project/.env.example` and any sample YAML configs

### Step 10 — Architecture guard test

The repo has `tests/architecture/` tests. Verify (and update if needed):

- [ ] Any test enforcing `MemoryLayerName.KNOWLEDGE` enum value
- [ ] Any test enforcing import paths to `memory.knowledge*`
- [ ] Any test enforcing `KnowledgeMemoryManager` symbol presence

### Step 11 — Final verification

- [ ] `grep -r "Knowledge" --include="*.py" src/ tests/ examples/` → returns **zero matches** (or only legitimate historical references in deprecated code paths)
- [ ] `grep -r "knowledge" --include="*.yml" examples/` → returns **zero matches** in active configs (legacy `long_term` migration paths excluded)
- [ ] `grep -r "knowledge" --include="*.md" docs/ src/ examples/` → returns **zero matches** outside ADRs written before 0035 (those stay as historical record)
- [ ] `ruff check src/modex_agent tests/ examples/bot_project` passes
- [ ] `mypy src/modex_agent` passes
- [ ] `pytest tests/unit/ -v` — all green
- [ ] `pytest tests/architecture/ -v` — all green
- [ ] `pytest tests/integration/ -v -m integration` — all green
- [ ] `pytest examples/bot_project/tests/` — all green

## Risk notes

- **`memory/core/` directory ambiguity**: this directory holds the *memory subsystem's core ABCs* (system.py, layers.py, models.py), not "core memory" the layer. After the rename, the directory `memory/layers/core.py` (the layer impl) will sit alongside `memory/core/` (the abc package). Disambiguate in `memory/core/AGENTS.md` by adding a sentence: "This directory is the *subsystem core ABCs* — `layers/core.py` is the *Core Memory layer implementation*."
- **`LongTermMemory` → `CoreMemoryContents` rename may collide** with the existing `MemoryContents` type if one exists. Grep for `MemoryContents` first.
- **String literal `knowledge_archive` on disk**: users have existing archive files with this name. Document the file rename in CHANGELOG.
- **Tests using `examples/bot_project/` may reference the layer by string** in fixtures. Run them post-rename.

## Files touched (top-level summary from grep)

| Category | File count |
|---|---|
| `src/modex_agent/` Python sources | 25 |
| `tests/` | 12 |
| `examples/bot_project/` Python | 2 |
| `docs/adr/` ADRs (only 0002, 0033 + add 0035) | 3 |
| `docs/design/` design docs | ~10 |
| `*.md` narrative docs | 30 |
| YAML configs | 0 (no active YAML configs in repo reference `knowledge:`; this is user-side) |
| Template files (`SOUL.md` etc.) | 0 (filename unchanged; contents unchanged) |
| **Total estimated file touchpoints** | ~80 |

## Out of scope (deliberately)

- Renaming XML tag values (`<your_identity>` etc.) — agent-facing prompts, leave as-is
- Renaming `SOUL.md` / `USER.md` / `MEMORY.md` file names — user workspace data, leave as-is
- Pre-existing `long_term` migration logic in `MemoryConfig.__init__` — historical, unrelated to this rename
- The new `KnowledgeBase` module itself — that's the *next* task, scoped by forthcoming ADR-0036

## Handoff

After execution, verify the architecture guard tests pass and all unit tests are green. Then proceed to ADR-0036 (KnowledgeBase design).
