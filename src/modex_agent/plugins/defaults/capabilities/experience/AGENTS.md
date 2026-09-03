<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 | D1 Experience vertical slice (plan §10, ADR-0047) -->

# experience

## Purpose

The complete Experience vertical slice as one FW-bundled capability package
(plan §10, ADR-0047): models, filesystem source, per-experience metadata,
EXPERIENCE.md validation, LRU curator, the ExperienceCatalog deep module,
the ReAct reviewer (package-owned `importlib.resources` prompts), the
review hook, the atomic tools, the TOOL/HOOK factories, the pool-level
supply, and the config models — everything Experience owns lives here.
The generic runtime sees only the standard seams (Capability, Tool, Hook,
SystemPromptProvider, CapabilitySupply); registration flows through the
normal slot resolution (one entry: `register_experience_feature`).

## Key Files

| File | Description |
|------|-------------|
| `catalog.py` | `ExperienceCatalog` — the concrete deep module (§10.1): `render_index(limit=20)`, `execute(command)` (typed command/result union), `curate(max_entries)`. Folds the retired manager/builder/name-sync/tool-router. `ExperienceRouterTool` is the roster-facing `experience` tool. One implementation shared by prompt section, tools, reviewer, curator (§10.6) |
| `supply.py` | `ExperienceSupply` — the SINGLE lifecycle owner (§10.5): construct → start curator → allow review submissions → reject during stop → cancel+await pending reviews → stop curator. `submit_review` accepts while running, rejects while stopping; start/stop idempotent. `build_experience_supply` is the capability `supply()` construction body |
| `capability.py` | `ExperienceCapability` (five-phase protocol) + `require_experience_supply` (loud supply read). Pool-config arbitration: conflicting `max_experiences`/`curator_interval` across agents in one pool → typed `ExperienceConfigError` boot failure (§5.3 — no silent first-pick) |
| `config.py` | `ExperienceCapabilityConfig` (the `capabilities: {experience: {...}}` face), split by altitude (§10.5.1): `ExperiencePoolConfig` (pool knobs, one owner) + `ExperienceReviewConfig` (per-agent knobs). The inert `enabled` field is deleted — capability effectiveness is the only enablement |
| `reviewer.py` | `ExperienceReviewAgent(ScopedFileAgent)` — ReAct reviewer with forked parent history; prompts loaded via `importlib.resources` from `prompts/` (the private memory PromptRegistry dependency died with the move) |
| `review_hook.py` | `ExperienceReviewHook(AfterGraphHook)` — submits reviews to the supply (NEVER spawns its own tasks); cooldown/edit-detection/post-review cleanup preserved; fail-soft when no reviewer is registered (§10.6) |
| `tools.py` | The 6 atomic tools (read/write/edit/list/rename/delete) with `ExperiencePathResolver` containment + `auto_correct_frontmatter_name` (the retired name_sync, folded into the mutation path) |
| `models.py` | Frozen Pydantic values (§6.1): `ExperienceSummary`, `Experience`, `ValidationResult`, `CurationResult`, and the `ExperienceCommand` discriminated union + `ExperienceResult` |
| `metadata.py` | `ExperienceMetaStore` ABC + `PerFileExperienceMetaStore` (`.exp.meta.json` per directory); `ExperienceMetaRecord` is a frozen Pydantic value |
| `source.py` | `FileExperienceSource` — directory scan/load; `sanitize_name`, `coerce_tags` |
| `validation.py` | `validate_experience_md` — frontmatter/name/description/body/structure rules |
| `curator.py` | `ExperienceCurator` — LRU eviction of excess non-pinned experiences |
| `section.py` | `ExperienceInjectionProvider` — the `experience.injection` section (version = content hash, byte-parity with the retired special case) |
| `tool_factory.py` / `hook_factory.py` | The TOOL/HOOK slot factories (moved from defaults/tools.py + defaults/hooks.py) |
| `registration.py` | `register_experience_feature(ctx)` — the one registration entry (CAPABILITY `experience`, TOOL `experience`, HOOK `experience_review`) |
| `paths.py` | Package constants (filenames, caps, section id, trace-dir helper) |
| `prompts/` | `review_system.md` + `review_user.md` — the reviewer's prompts, shipped as package resources (wheel-tested) |

## For AI Agents

### Working In This Directory
- The facade (`__init__.py`) is import-light (§10.4): importing the package must NOT eagerly import reviewer/tools/hook implementation modules — pinned by an import smoke test (`tests/unit/plugins/experience/test_facade_and_resources.py`, subprocess + sys.modules).
- Directory name is the canonical Experience identity; frontmatter `name` auto-corrects to match.
- Review tasks are supply-owned: a hook must never `asyncio.create_task` a review itself (the retired `_pending` set could outlive teardown — the §5.3 defect).
- Config altitude (§10.5.1): pool knobs live on the supply (one per pool, conflict-checked); review knobs stay per-agent (`review_config_by_agent`).
- Missing review LLM is fail-soft: the hook skips with a warning; tool/section/storage/curator stay available; boot never aborts.
- `ExperienceCatalog`, stores, curator, and supply are regular classes (§6.1) — never frozen Pydantic.

### Testing
- `tests/unit/plugins/experience/` — package tests (catalog/render, tools, curator, metadata, source, validation, name-sync, prompts, facade/resources, config altitude).
- `tests/unit/plugins/test_experience_supply.py` — supply lifecycle, review-task ownership, config arbitration, section golden parity.
- `tests/unit/hook/test_experience_review*.py` — hook trigger gates, cooldown, fork, snapshot dir resolution (lifecycle + filesystem outcomes, §18.7).
- `tests/integration/experience/` — end-to-end (`-m integration`).

## Dependencies

### Internal
- `modex_agent.plugins.capability` — the Capability protocol + view types
- `modex_agent.plugins.abc` / `plugins.loader` — factory/slot/registration faces
- `modex_agent.core.prompt` — `SystemPromptProvider`; `core.tool_manager` — Tool bases
- `modex_agent.tools.manager` + `tools.standard` — InMemoryToolManager + file tools
- `modex_agent.agents.summarizer` — `ScopedFileAgent` base + trajectory emitter
- `modex_agent.memory.snapshot` / `memory.core.system` — snapshot formatting / MemorySystem
- `modex_agent.workspace.paths` — `WorkspacePaths.experience_dir`
- `modex_agent.utils` — frontmatter, xml, file_io
- `modex_agent.tools.presets` — `EXPERIENCE_REVIEW_HOOK_NAME`

### External
- `pydantic` — frozen value models; `pathvalidate` — name sanitization
