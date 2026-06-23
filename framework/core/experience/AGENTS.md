<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# experience

## Purpose

Experience layer — reusable problem-solving patterns from past sessions. Each experience is a filesystem directory containing `EXPERIENCE.md` with YAML frontmatter (name, description, tags, scenario) and markdown body. The system provides source loading, XML prompt injection, validation, metadata tracking, name synchronization, and LRU curation.

Inspired by Hermes-style experience self-learning. Experiences are injected into the system prompt as compact XML metadata; the LLM uses `experience` tool actions (read/write/edit/list/rename/delete) to manage them at runtime.

## Key Files

| File | Description |
|------|-------------|
| `manager.py` | `ExperienceManager` — facade coordinating `FileExperienceSource` + `ExperiencePromptBuilder`. Entry point for `build_prompt()` injection |
| `source.py` | `FileExperienceSource` — loads from filesystem directories, `sanitize_name()`, `list_experiences()` (metadata only), `load_experience()` (full body) |
| `builder.py` | `ExperiencePromptBuilder` — renders `<available_experiences>` XML metadata for system prompt |
| `models.py` | `ExperienceSummary` (lightweight metadata for injection), `Experience` (full with body, frontmatter, location) |
| `validation.py` | `validate_experience_md()`, `ValidationResult` — validates EXPERIENCE.md format (frontmatter, name, description, body) |
| `name_sync.py` | `auto_correct_frontmatter_name()` — ensures EXPERIENCE.md frontmatter `name` matches directory name |
| `meta.py` | `ExperienceMetaStore` ABC, `PerFileExperienceMetaStore` — per-experience metadata (use_count, view_count, timestamps, pinned). Replaces deprecated `ExperienceUsageTracker` |
| `curator.py` | `ExperienceCurator` — LRU eviction of excess non-pinned experiences when count exceeds `max_experiences` (default 20) |
| `usage.py` | `ExperienceUsageTracker` — **deprecated**, replaced by `PerFileExperienceMetaStore`. Sidecar JSON tracking |

## Architecture

```
FileExperienceSource (disk scan)
    ↓ list_experiences() → [ExperienceSummary]
ExperienceManager (facade)
    ↓ build_prompt() with max_experiences cap
ExperiencePromptBuilder
    ↓ XML metadata rendering
System Prompt Injection (<available_experiences>)
```

```
ExperienceReviewAgent (agents/experience/)
    ↓ uses Experience*Tool set (memory/tools/experience/)
EXPERIENCE.md files on disk
    ↓ name_sync auto-corrects frontmatter name → dir name
validate_experience_md (on read/write)
    ↓ metadata tracking
PerFileExperienceMetaStore (.exp.meta.json per dir)
    ↓ periodic cleanup
ExperienceCurator (LRU eviction of excess)
```

## For AI Agents

### Working In This Directory
- Experiences are identified by **directory name**, not frontmatter name. Frontmatter `name` is auto-corrected to match directory.
- `ExperienceSummary` is for prompt injection (no body); `Experience` is for full read/edit.
- `PerFileExperienceMetaStore` stores `.exp.meta.json` inside each experience directory — no central sidecar file.
- Max 20 experiences injected per prompt (configurable via `ExperienceManager.build_prompt(max_experiences=)`).
- Pinned experiences are immune to curator eviction but count toward the total.

### Experience Directory Layout
```
experiences/{pool_name}/{agent_name}/
├── my-experience/
│   ├── EXPERIENCE.md          # frontmatter + body
│   ├── .exp.meta.json         # usage stats (auto-generated)
│   └── references/            # optional attachments
└── another-pattern/
    ├── EXPERIENCE.md
    └── .exp.meta.json
```

### EXPERIENCE.md Format
```markdown
---
name: my-experience       # must match directory name
description: Short summary
tags: [tag1, tag2]
scenario: When to apply this
trigger: Conditions that activate recall
version: 1
pinned: false
---

Body content with problem-solving pattern...
```

### Integration Points
- **Pool builder**: `_build_pool_experience_manager()` in `examples/bot_project/bot/service/pool_builder.py`
- **System prompt**: injected by `framework/pipeline/context_assembler.py` via `ExperienceManager.build_prompt()`
- **Review agent**: `framework/agents/experience/review_agent.py` creates/updates experiences from conversation snapshots

### Testing
- Tests in `tests/unit/core/test_experience.py`

## Dependencies

### Internal
- `framework.core.frontmatter` — YAML frontmatter parsing
- `framework.core.skills.builder` — shares `SkillPromptBuilder` pattern
- `framework.memory.utils` — `safe_atomic_replace` for meta file writes
- `framework.utils.xml` — `xml_attr`, `xml_text` for prompt rendering

### External
- `pathvalidate` — safe filename sanitization

<!-- MANUAL: -->
