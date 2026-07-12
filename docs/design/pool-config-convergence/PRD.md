# Pool Config Convergence and Framework Promotion

Status: completed

Related: ADR-0020 (`docs/adr/0020-pool-config-convergence-and-framework-promotion.md`);
ADR-0019 (`docs/adr/0019-cross-pool-peer-communication.md` — peer wiring depends on
`PoolSpec.peers`); ADR-0015 (`docs/adr/0015-unified-inbox-driven-agent-messaging.md` —
`AgentTemplate.materialize` is the subagent construction path);
`CONTEXT.md` → "PoolSpec", "MainAgentSpec", "SubagentSpec", "AgentTemplate",
"PoolAssemblyDeps", "PoolInstance", "Module Optionality".

## Problem Statement

A framework developer who wants to use ModexAgent's multi-agent pool capability
in their own application must reinvent the entire pool configuration layer from
scratch. Today the reusable framework (`src/modex_agent/`) has the pool
**runtime** (`AgentPool`, `InboxPoller`, `AgentMessageBus`, `AgentTemplate`,
`communication/`) but not the pool **configuration layer** — the models that
describe a pool on disk, the store that reads and writes `pool.yml`, the router
that dispatches sessions to pools, and the deployment holder that owns live
connections. All of these live in the reference bot (`examples/bot_project/`),
even though they carry zero business-specific logic.

Worse, the configuration layer is split across two locations and six models
with overlapping fields. The framework's `PoolConfig` (mutable runtime config)
and `AgentConfig` (per-agent config with three dead fields) duplicate what the
bot's `PoolTree` / `MainAgentNode` / `SubagentNode` (frozen wire models) already
express. `AgentTemplate` (runtime dataclass) re-declares all ten of
`SubagentNode`'s fields. `AgentTemplateRegistry` re-parses the same
`templates/*.yml` files that `PoolStore` already reads, using ~100 lines of
manual YAML parsing instead of Pydantic validation. And `AppConfig.from_yaml()`
reaches into `config/pools/` — the framework reads the business layer's
configuration files, then mutates `PoolConfig.memory` at runtime to patch in
`max_context_tokens`.

A developer who wants to use the pool capability cannot simply import a
framework module, load a pool spec, and assemble a pool. They must either copy
the bot's configuration layer or work around the framework's own `PoolConfig`
which is neither a clean runtime input nor a clean wire model.

## Solution

Converge the pool configuration layer into the framework as a cohesive,
optional module: `multi_agent/pool_config/`. This is not a simple file move —
it is a data model convergence that deletes two intermediate config models
(`PoolConfig`, `AgentConfig`), renames three wire models to remove false
tree-structure implications, collapses `AgentTemplate` from 14 fields to 3, and
introduces a single runtime-dependency value object (`PoolAssemblyDeps`) that
replaces the scattered runtime params previously held by the deleted models.

From the developer's perspective: they call `PoolStore` to load a `PoolSpec`
from disk, construct a `PoolAssemblyDeps` with baked defaults (memory, media,
experience), and pass both to `create_pool()`. The framework no longer reads
their configuration files. `AppConfig` is pure framework config — no `pools`
field, no `default_pool`, no `config/pools/` scanning. If they never configure
pools, the `multi_agent/` package is dead code (Tier 1 optional: config-level
decoupling).

## User Stories

### Framework developers (consumers of the pool capability)

1. As a framework developer, I want to import pool configuration models
   (`PoolSpec`, `MainAgentSpec`, `SubagentSpec`) from the framework package, so
   that I do not need to copy wire models from the reference bot.

2. As a framework developer, I want `PoolStore` available in the framework, so
   that I can read and write `pool.yml` + `templates/*.yml` without
   re-implementing YAML parsing, validation, or atomic writes.

3. As a framework developer, I want `PoolRouter` and `PoolSessionStore`
   available in the framework, so that I can dispatch sessions to pools without
   writing my own routing logic.

4. As a framework developer, I want `PoolInstance` available in the framework,
   so that I have a typed deployment holder for live pool connections.

5. As a framework developer, I want to construct a pool by passing a `PoolSpec`
   (disk data) and a `PoolAssemblyDeps` (runtime deps) to the assembly
   function, so that the boundary between persisted configuration and injected
   runtime dependencies is explicit and type-safe.

6. As a framework developer, I want `AppConfig` to not contain pool definitions,
   so that the framework does not read my application's configuration files.

7. As a framework developer, I want the `multi_agent/` package to be
   structurally optional — if I never configure pools, no pool runtime starts
   — so that I can use the framework for single-agent scenarios without
   pulling in pool machinery.

8. As a framework developer, I want `AgentTemplateRegistry` to load subagent
   templates from `PoolStore` (the single YAML reader), so that there is one
   parsing path and one validation rule set, not two.

9. As a framework developer, I want `AgentTemplate` to wrap a `SubagentSpec`
   rather than re-declaring its fields, so that the disk schema and the
   runtime template cannot drift out of sync.

10. As a framework developer, I want `PoolRouter` to accept
    `dict[str, PoolInstance]` (typed) rather than `dict[str, Any]`, so that
    type checkers catch routing mistakes at compile time.

11. As a framework developer, I want `PoolInstance` to carry `media` and
    `subagent_count` directly rather than holding a full config object, so
    that the deployment holder exposes only what consumers actually read.

12. As a framework developer, I want `ExperienceConfig` and `MediaConfig` to
    live in the pool config module, so that all pool-related types are in one
    place.

### Bot maintainers (reference project)

13. As a bot maintainer, I want the bot's `pool_config_controller.py` to import
    `PoolStore` from the framework, so that the WebUI REST layer is a thin
    mapper over the framework's store.

14. As a bot maintainer, I want `create_pool()` to accept `PoolSpec` +
    `PoolAssemblyDeps`, so that the factory signature reflects the
    disk-vs-runtime boundary.

15. As a bot maintainer, I want `wiring.py` to construct `PoolAssemblyDeps`
    with `max_context_tokens` injected at construction time, so that runtime
    mutation of config objects is eliminated.

16. As a bot maintainer, I want `BackgroundTaskRunner` to accept
    `dict[str, PoolAssemblyDeps]`, so that it reads `dream_engine` and
    `experience` from the deps value object rather than from a `PoolConfig`.

17. As a bot maintainer, I want `_wire_pool_to_resources` to receive
    `PoolAssemblyDeps`, so that the experience review hook reads
    `deps.experience` rather than `main_cfg.experience`.

18. As a bot maintainer, I want `web_ui_service.py` to build the agent→pool
    mapping from `PoolStore` + `PoolSpec`, so that it does not depend on
    `AppConfig.pools` or independently construct `AgentTemplateRegistry`.

19. As a bot maintainer, I want `core.py` to stop mutating
    `pool_cfg.memory.session.max_context_tokens`, so that config objects are
    never mutated after construction.

20. As a bot maintainer, I want `core.py:_system_prompt_for` to read
    `PoolSpec.main` instead of `pool_cfg.agents`, so that the main-agent
    lookup is direct rather than a filtered search.

### Framework architects (design integrity)

21. As a framework architect, I want `SubagentSpec` to NOT carry `approval` or
    `experience` fields, so that the disk model reflects the actual capability
    boundary (subagents never have approval or experience review).

22. As a framework architect, I want `AgentTemplate` to access disk fields via
    `self.spec.*` (not property shims), so that there is no backward-compat
    layer between the template and the spec.

23. As a framework architect, I want all renames to be one-shot with no
    re-export shims, so that the old import paths fail loudly rather than
    silently working.

24. As a framework architect, I want `MultiAgentConfig` to retain
    `session_retention` but lose `default_pool`, so that the framework
    configures runtime behavior without owning business decisions.

25. As a framework architect, I want the `pipeline/` → `multi_agent/` runtime
    imports to remain (not addressed in this spec), so that the scope stays
    bounded to config-layer convergence.

## Implementation Decisions

### Deleted models

- **`PoolConfig`** (`ioc/configs/pool.py`, 55 lines) — mutable runtime config;
  replaced by `PoolSpec` (disk) + `PoolAssemblyDeps` (runtime).
- **`AgentConfig`** (`ioc/configs/agent.py`, 104 lines) — 3 of 12 fields dead
  in pool path (`llm`, `safety`, `hooks`); live fields covered by
  `MainAgentSpec`; `ExperienceConfig` extracted to standalone file.

### Renamed models (bot → framework, same fields, new names)

| Old name | New name | Rationale |
|----------|----------|-----------|
| `PoolTree` | `PoolSpec` | Not a tree — flat list of main + subagents |
| `MainAgentNode` | `MainAgentSpec` | "Node" falsely implies tree structure |
| `SubagentNode` | `SubagentSpec` | Same |

### New model: `PoolAssemblyDeps`

Frozen Pydantic value object replacing the runtime params previously held by
`PoolConfig` / `AgentConfig`. Type shape from the ADR-0020 design session:

```python
class PoolAssemblyDeps(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    memory: MemoryConfig | None = None
    media: MediaConfig = Field(default_factory=MediaConfig)
    experience: ExperienceConfig | None = None
```

`memory` replaces BOTH `PoolConfig.memory` and `AgentConfig.memory` — they were
the same baked `main_agent_memory()` default; the two-field split was a
historical accident (`AgentConfig.memory` was always `None` on the main-agent
path because `AgentDescriptor.memory_config` was never read by the runtime).

### `AgentTemplate` convergence

14 fields → 3 fields. Type shape:

```python
@dataclass
class AgentTemplate:
    spec: SubagentSpec
    memory: MemoryConfig | None = None    # baked subagent_memory()
    skills: SkillsConfig | None = None    # disk-managed
```

All field accesses in `materialize()` go through `self.spec.*` (e.g.
`self.spec.agent_name`, `self.spec.max_steps`, `self.spec.tool_preset`).
`self.memory` and `self.skills` remain direct (runtime-only, not on spec).
No property shims, no backward-compat layer.

Deleted from `AgentTemplate`: `approval` (subagents never have approval — dead
field), `experience` (subagents never have experience review — dead field),
and 10 fields that duplicated `SubagentSpec` (`agent_name`, `description`,
`max_steps`, `tool_preset`, `tool_supplements`, `context_mode`,
`system_prompt_mode`, `fork_max_messages`, `mcp`).

External access sites (~8) change from `template.agent_name` to
`template.spec.agent_name`.

### `SubagentSpec` field set (no approval/experience)

```python
class SubagentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    agent_name: str
    description: str = ""
    max_steps: int = 80
    tool_preset: ToolPreset = ToolPreset.READ_WRITE
    tool_supplements: list[str] = Field(default_factory=list)
    context_mode: ContextMode = ContextMode.FRESH
    mcp: list[str] = Field(default_factory=list)
    system_prompt_mode: SystemPromptMode = SystemPromptMode.REPLACE
    fork_max_messages: int = Field(default=DEFAULT_FORK_MAX_MESSAGES, ge=1, le=MAX_FORK_MAX_MESSAGES)
```

Today's `AgentTemplateRegistry._load()` parses `approval` and `experience`
from `templates/*.yml` — these are dead (never read by `materialize()`). The
convergence deletes this parsing. Additionally, `PoolStore._read_subagents()`
is switched from field-by-field construction to `SubagentSpec.model_validate(raw)`
so that Pydantic's `extra="forbid"` actually rejects unknown keys in the YAML
dict (field-by-field construction bypasses validation of the raw dict). If a
template YAML file contains an `approval:` or `experience:` block,
`model_validate` raises a validation error. The lenient `or` fallback patterns
(e.g. `raw.get("tool_preset") or ToolPreset.READ_WRITE`) are replaced by
Pydantic field defaults. This is intentional — "不留后路".

### `AgentTemplateRegistry` convergence

Constructor changes from `(project_dir: Path, default_subagent_memory)` to
`(pool_store: PoolStore, default_subagent_memory)`. The `_load()` method no
longer scans `config/pools/*/templates/*.yml` and manually parses each file
(~100 lines). Instead it calls `pool_store.list_pool_names()` →
`pool_store.read_pool(name)` → iterates `tree.subagents` to construct
`AgentTemplate(spec=sub_spec, memory=default_subagent_memory)`. The
`_ACCEPTED_KEYS` frozenset and all manual enum-parsing/fallback logic are
deleted — Pydantic validation handles this.

### `PoolInstance` convergence

Loses `config: PoolConfig`. Gains `media: MediaConfig` +
`subagent_count: int` — the only two values actually read from config
(`web_ui_service.py:620` reads `.config.media`; `core.py:388` reads
`.config.agents` to count subagents). Stays `@dataclass` (rule-12: runtime
object with connections, not a Pydantic model).

### `PoolRouter` type convergence

`pools: dict[str, Any]` → `pools: dict[str, PoolInstance]`. The `Any` was
necessary when `PoolInstance` lived in the bot; now that both are in the
framework, the type can be explicit.

### `AppConfig` changes

- Delete `pools: dict[str, PoolConfig]` field
- Delete `default_pool` from `MultiAgentConfig`
- Delete `_MAIN_AGENT_YAML_FIELDS` constant
- Delete `_validate_pool_name()` (moves to `PoolStore`'s validation)
- Delete the pool.yml loading block in `from_yaml()` (~40 lines)
- `MultiAgentConfig.session_retention` stays (configures runtime session
  retention policy, not pool definitions)

The business layer (`BotService`) holds `dict[str, PoolSpec]` + `default_pool:
str` directly, loaded via `PoolStore`.

### `max_context_tokens` injection

Today `core.py:231-233` mutates `pool_cfg.memory.session.max_context_tokens`
at runtime. Convergence: the business layer constructs `PoolAssemblyDeps`
with `max_context_tokens` already injected via `model_copy`:

```python
def _main_agent_memory(max_context_tokens: int) -> MemoryConfig:
    cfg = main_agent_memory()
    return cfg.model_copy(update={
        "session": cfg.session.model_copy(update={"max_context_tokens": max_context_tokens})
    })
```

No runtime mutation of config objects. Frozen `model_copy` replaces mutable
assignment.

### `BackgroundTaskRunner` signature change

`pools_config: dict[str, PoolConfig]` → `assembly_deps: dict[str,
PoolAssemblyDeps]`. Internal reads: `pool_cfg.memory.dream_engine` →
`deps.memory.dream_engine`; `main_cfg.experience` → `deps.experience`.

### `build_pool_data()` signature change

`pool_cfg: PoolConfig` + `memory_cfg_factory: Callable[[PoolConfig],
MemoryConfig]` → `pool_spec: PoolSpec` + `assembly_deps: PoolAssemblyDeps`.
Internal reads: `_main_agent_name(pool_cfg)` → `pool_spec.main.agent_name`
direct; `_build_experience_manager(pool_cfg, ...)` → reads
`assembly_deps.experience`; `memory_cfg_factory(pool_cfg)` deleted →
`assembly_deps.memory` direct (no factory needed — the factory was a lambda
reading `pool_cfg.memory`, which is now `assembly_deps.memory` directly).

### `_wire_pool_to_resources` signature change

`pool_cfg: PoolConfig` → `deps: PoolAssemblyDeps`. Internal reads:
`main_cfg.experience` → `deps.experience`.

### `web_ui_service.py` agent→pool mapping

Replaces `AppConfig.pools` + `pool_cfg.agents` iteration with
`PoolStore.list_pool_names()` + `PoolSpec` reads. The redundant
`AgentTemplateRegistry` construction (lines 691-700) is deleted — subagent
names come from `PoolSpec.subagents`.

### `core.py` changes

- `_system_prompt_for`: `self._app_config.pools[name]` → reads from
  `PoolSpec` held by `BotService`; `pool_cfg.agents` filter → `pool_spec.main`
  direct access.
- `_load_model_config`: the `max_context_tokens` mutation loop is deleted
  (moved to `wiring.py` deps construction).

### File moves (no re-export shims)

| Object | From | To |
|--------|------|-----|
| `PoolSpec` + wire models | `bot/config/pool_payloads.py` | `multi_agent/pool_config/specs.py` |
| `PoolStore` | `bot/config/pool_store.py` | `multi_agent/pool_config/store.py` |
| `PoolAssemblyDeps` (new) | — | `multi_agent/pool_config/deps.py` |
| `ExperienceConfig` | `ioc/configs/agent.py` | `multi_agent/pool_config/experience.py` |
| `MediaConfig` | `ioc/configs/pool.py` | `multi_agent/pool_config/media.py` |
| `PoolRouter` + `PoolSessionStore` | `bot/service/pool_router.py` | `multi_agent/pool_router.py` |
| `PoolInstance` | `bot/service/pool_instance.py` | `multi_agent/pool_instance.py` |

Old import paths fail immediately — no `from bot.config.pool_payloads import
PoolTree` shim. All consumers updated to new paths in one pass.

### Files staying in bot (import path + signature changes only)

- `pool_builder.py` — `create_pool(pool_spec, assembly_deps, ...)` signature
- `pool_config_controller.py` — import path change, peer logic unchanged
- `wiring.py` — constructs `assembly_deps`, threads to consumers
- `background.py` — signature change
- `pool_data.py` — `build_pool_data()` signature change (`pool_cfg` + factory →
  `pool_spec` + `assembly_deps`)
- `core.py` — deletes `max_context_tokens` mutation, reads `PoolSpec`
- `web_ui_service.py` — reads `PoolStore`/`PoolSpec` for mapping
- `resolve_pool.py` — unchanged

### Net line count

~320 lines deleted (2 deleted models + AppConfig pool loading + AgentTemplate
field dedup + AgentTemplateRegistry manual parsing). ~10 lines added
(PoolAssemblyDeps + AgentTemplate property-less spec access). Net ~310 lines
reduced.

## Testing Decisions

### Testing philosophy

This is a behavior-preserving convergence. The external behavior (pool
creation, agent execution, message routing, subagent materialization, peer
communication) does not change. Tests should verify "nothing broke", not "new
behavior works". The highest possible seam is preferred — if an integration
test passes, the internal convergence is correct.

### Test seam 1: Disk round-trip (PoolStore)

**Existing seam**: `examples/bot_project/tests/bot/config/test_pool_store.py`

Migrate to use renamed types (`PoolSpec`, `MainAgentSpec`, `SubagentSpec`).
Verify: `pool.yml` + `templates/*.yml` round-trip; peer bidirectional
validation; rename/delete preserve non-editable fields; `extra="forbid"`
rejects unknown keys (including `approval`/`experience` on `SubagentSpec`).

No new test file needed. The existing test file proves the disk I/O layer is
correct after the move.

### Test seam 2: Assembly end-to-end (create_pool + materialize)

**Existing seam**: `tests/integration/multi_agent/` +
`examples/bot_project/tests/integration/`

Verify the full data flow: `PoolSpec` + `PoolAssemblyDeps` → `create_pool()` →
`PoolInstance` with correct main agent, subagent templates, memory, media,
experience hook. `AgentTemplateRegistry(PoolStore)` loads templates;
`AgentTemplate.spec.*` is accessible in `materialize()`;
`BackgroundTaskRunner(assembly_deps)` reads `dream_engine` + `experience`;
`_wire_pool_to_resources(deps)` wires the experience hook.

This is the highest-value seam — if integration tests pass, the convergence
is correct. No new test file; existing integration tests migrated to new
signatures.

### Test seam 3: AppConfig shape (framework config boundary)

**Existing seam**: `tests/unit/ioc/test_app_config.py`

Assert: `AppConfig` has no `pools` field; `MultiAgentConfig` has no
`default_pool`; `from_yaml()` does not read `config/pools/`. Pure structural
assertion — `assert not hasattr(AppConfig.model_fields, 'pools')` style.

### What NOT to test

- Do not test `AgentTemplate` field access mechanics (property vs direct) —
  that's an implementation detail.
- Do not test import path changes in isolation — the integration tests cover
  this transitively.
- Do not test `PoolInstance.media` / `subagent_count` in isolation — they are
  read by `web_ui_service.py` and `core.py`, covered by bot integration tests.

### Prior art

- `test_pool_store.py` — Pydantic model round-trip + validation patterns
- `tests/integration/multi_agent/test_multi_pool_isolation.py` — multi-pool
  assembly + routing
- `tests/unit/multi_agent/test_template.py` — `AgentTemplate` construction +
  field access (migrate `t.agent_name` → `t.spec.agent_name`)
- `tests/unit/ioc/test_app_config.py` — AppConfig field assertions

## Out of Scope

- **`pipeline/` → `multi_agent/` import decoupling** — the 4 runtime imports
  (`AgentDescriptor`, `AgentAddress`, `AgentMessageEnvelope`,
  `AgentMessageType`) remain. Moving these to `core/` is a future Tier 2
  optionality effort.
- **`pool_config_controller.py`** stays in bot — it is an HTTP REST orchestrator
  bound to aiohttp. Only its import path changes.
- **`pool_builder.py`** stays in bot — it is an IOC factory with
  business-specific tool registration, MCP config, terminal setup. Only its
  signature changes.
- **`wiring.py`, `background.py`, `pool_data.py`** stay in bot — workspace
  assembly is a business concern.
- **`resolve_pool.py`** stays in bot — input pipeline stage.
- **Communication-log artefact** (ADR-0019 deferred #2) — separate future work.
- **Per-pair context fork** (ADR-0019 deferred #3) — marked as not doing.

## Further Notes

### Implementation order (suggested)

1. **Create `multi_agent/pool_config/` package** with `specs.py`, `deps.py`,
   `experience.py`, `media.py` — new files with renamed models. No consumers
   broken yet.
2. **Move `PoolStore`** to `multi_agent/pool_config/store.py` — update all
   imports. Bot tests for PoolStore now test the framework module.
3. **Move `PoolRouter`, `PoolSessionStore`, `PoolInstance`** to
   `multi_agent/` — update imports + `PoolRouter` type to
   `dict[str, PoolInstance]`.
4. **Converge `AgentTemplate`** — change fields to `spec` + `memory` +
   `skills`; update `materialize()` + all external access sites to
   `self.spec.*` / `template.spec.*`.
5. **Converge `AgentTemplateRegistry`** — change constructor to accept
   `PoolStore`; delete manual yml parsing.
6. **Introduce `PoolAssemblyDeps`** + change `create_pool()` signature —
   update `wiring.py`, `background.py`, `_wire_pool_to_resources`,
   `web_ui_service.py`, `core.py`.
7. **Delete `PoolConfig`, `AgentConfig`, `AppConfig.pools`,
   `AppConfig.default_pool`** — delete files, delete fields, delete
   `from_yaml()` pool loading. This is the point of no return.
8. **Run full test suite** — all three test seams must pass.

Steps 1-5 are additive (new code, old code still works). Step 6 is the
cutover (signatures change). Step 7 is the cleanup (dead code deleted). This
ordering minimizes the window of broken state.

### Module optionality after convergence

The `multi_agent/` package (including `pool_config/`) satisfies **Tier 1
(config-level optional)**: a business that never configures pools, never
imports `multi_agent`, and never passes pool specs to assembly will not start
any pool runtime. The package exists as dead code from the business's
perspective.

Tier 2 (import-level optional) is NOT satisfied — `pipeline/` has 4 runtime
imports of `multi_agent` types. This is pre-existing and explicitly out of
scope.

### ADR-0019 compatibility

`PoolSpec.peers: list[str]` is preserved unchanged. The peer wiring in
`wiring.py` (Phase 2 of ADR-0019) reads `pool_spec.peers` instead of
`pool_tree.peers` — same field, renamed type. `PoolStore.add_peer_pair` /
`remove_peer_pair` are preserved unchanged. No ADR-0019 behavior is affected.
