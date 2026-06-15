<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-10 -->

# skills

Skill loading, filtering, caching, and progressive prompt building. Skills are markdown documents (with optional YAML frontmatter) that inject domain knowledge into agent prompts.

## Key Files

| File | Description |
|------|-------------|
| `models.py` | `Skill`, `SkillSummary`, `SkillMetadata`, `SkillResource`, `ResolutionContext` — data structures |
| `source.py` | `SkillSource` ABC — `list_skills()`, `load_skill(name)`; `FileSkillSource` (filesystem, flat/directory layouts); `InlineSkillSource` (in-memory); `CompositeSkillSource` (multi-source with last_wins/error/first_wins merge) |
| `manager.py` | `SkillManager` — facade coordinating source, filter, cache, builder; runtime overrides via `register_skill()`/`unregister_skill()`. `get_skill()` triggers `DirectorySkillCache` freshness check so add/remove is live-detected. |
| `builder.py` | `SkillPromptBuilder` ABC — `InlineBuilder` (full content), `ProgressiveBuilder` (XML directory, degrades to inline if no read_file tool), `HybridBuilder` (inline `always=True` skills + directory for rest). Also exports `build_skill_command_xml()` shared helper used by both `SkillCommandHandler` and business-layer skill registries. |
| `filter.py` | `SkillFilter` ABC — `AlwaysFilter`, `AllowListFilter`, `DenyListFilter`, `CompositeFilter` (sequential chain); `SkillWhitelistFilter` (wrapper delegating to SkillManager) |
| `cache.py` | `SkillCache` ABC; `DirectorySkillCache` — per-directory name-set change detection, partial prompt rebuild on stale directories |

## Data Flow

1. `SkillSource.list_skills()` → `list[SkillSummary]` (lightweight discovery)
2. `SkillSource.load_skill(name)` → `Skill` (full content hydration)
3. `SkillFilter.filter(skills)` → filtered subset
4. `SkillPromptBuilder.build(skills)` → prompt string for LLM
5. `SkillManager` orchestrates 1–4; `DirectorySkillCache` adds stale-detection and partial rebuilds

## Design Rules

- `SkillMetadata.from_dict()` handles three formats: flat YAML frontmatter, nested `requires:` block, and nanobot/openclaw JSON-in-YAML `metadata:` blocks.
- `ProgressiveBuilder` auto-downgrades to `InlineBuilder` when no file-reading tool is available.
- `DirectorySkillCache` uses `scandir` only (no file reads) for change detection — reloads from source only when name-sets differ.
- Runtime overrides (`register_skill`) take precedence over source-loaded skills in `SkillManager`.

## Dependencies

- `pathvalidate` — filename sanitization in `DirectorySkillCache`
- `pyyaml` (optional) — frontmatter parsing in `FileSkillSource`
