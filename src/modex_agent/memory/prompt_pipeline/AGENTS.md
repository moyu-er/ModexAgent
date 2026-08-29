<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-08-28 (capability-bundles doc sync, ADR-0047) -->

# prompt_pipeline

## Purpose
System prompt pipeline — an ordered, versioned collection of `SystemPromptProvider` instances. Each provider contributes one section of the system prompt (base personality, runtime metadata, workspace info, etc.). Supports caching, version-based refresh, and ordered assembly into the final system prompt string. Capability-bundle sections (ADR-0047) ride the same `SystemPromptProvider` ABC through the capability-section anchor in `memory/system.py`.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Package init |
| `providers.py` | Concrete `SystemPromptProvider` implementations (see below) |

## Providers

| Provider | Version key | Fires when | Content |
|----------|-------------|------------|---------|
| `BasePromptProvider` | `static` | Always | Static base prompt from `agents/<name>.md` (never refreshes) |
| `RuntimeProvider` | hourly | Always | Current date/hour + platform info |
| `ModelInfoProvider` | `model:{name}:{modalities}` / `model:none` | `runtime_info` present (main-agent turn) | Declares the agent's perceptual capabilities (e.g. image perception). `ModelInfo` supplied at construction from `runtime_info[RuntimeInfoKey.MODEL_INFO]` (threaded by `assemble_context` from `runtime_services.model_info`). Tools that behave differently per modality (e.g. `read`) are mentioned as examples, not bound. Emits nothing when `model_info` is `None`. |
| `SkillProvider` | skill-set hash | Skills assigned | Active skill summaries |
| `ExperienceProvider` | experience hash | Experience files exist | Relevant experience entries |
| `CoreMemoryProvider` | core memory hash | Core Memory files exist | SOUL.md / USER.md / MEMORY.md content (provider renamed from `KnowledgeProvider` per ADR-0035) |
| `ArchiveProvider` | retrieved content hash (TTL 5s) | Archive entries exist | Backend-neutral historical summaries; `context.md` paths only when file storage exposes one. Version check TTL-cached to avoid per-iteration I/O |
| `PrunedProvider` | pruned hash (TTL 5s) | Pruned catalog exists | XML catalog of cleaned-up messages. Version check TTL-cached to avoid per-iteration I/O |
| `RuntimeProvider` | hourly | Always | Runtime metadata |
| `ProviderBlocksProvider` | blocks hash | Provider blocks configured | Custom prompt blocks |
| `ProviderPrefetchProvider` | prefetch hash | Prefetch configured | Prefetched context |

The retired composite communication provider (peer reply contract, subagent consultation contract, delegation guidance — runtime tool-registration gated) migrated to the `subagents` capability package (ADR-0047): the three briefs are now the capability sections `subagents.delegation` (order=40), `subagents.consultation` (order=41), and `subagents.peer` (order=42), rendered through the capability-section anchor of `MemorySystemContextManager.load()` and gated at compile time (declared children / non-root / root-with-peers). The peer section reads the live root store at render time, so its version follows the remote-name set.

The retired tool-gated todo provider ("## Task Tracking") migrated to the
`todo` capability package — `TodoCapability.assemble` wires the byte-identical
static section through the capability-section anchor of
`MemorySystemContextManager.load()` (ADR-0047 / SPEC §8.2).

## For AI Agents

### Working In This Directory
- `SystemPromptPipeline` holds an ordered list of `SystemPromptProvider` instances
- Each provider has a `version` — the pipeline only re-fetches content when the version changes
- Providers are ordered by their position in the pipeline list (not by priority)
- `BasePromptProvider` is static (version="static") — never refreshes
- `RuntimeProvider` refreshes hourly based on the current datetime hour
- Gated providers emit empty content when their condition is unmet — no-op
  for agents that don't own the relevant data (the capability section
  providers in `plugins/defaults/capabilities/` follow the same pattern)

### Common Patterns
- Create providers, add them to a `SystemPromptPipeline` in the desired order, call `pipeline.get_content()` to get the assembled system prompt
- `_fetch_version()` returns a version string; `_fetch_content()` returns the actual prompt text
- Pipeline caches content per version — call `invalidate()` to force refresh
- New providers should follow the `SystemPromptProvider` ABC pattern: gated
  by a condition, version derived from the gating data, content empty when
  condition unmet

## Dependencies

### Internal
- `modex_agent.core.prompt` — `SystemPromptProvider`, `SystemPromptPipeline`
- `modex_agent.utils.timezone` — `get_user_timezone`

<!-- MANUAL -->
