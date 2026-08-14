# Pool config convergence and framework promotion

Status: accepted (2026-07-12; implemented)

## Context

The pool configuration layer has accreted across two locations and six models:

- **Framework** (`src/modex_agent/ioc/configs/`): `PoolConfig` (mutable runtime
  config, 55 lines), `AgentConfig` (per-agent config, 104 lines — 3 of its 12
  fields are dead in the pool path: `llm`, `safety`, `hooks`).
- **Business** (`examples/bot_project/bot/config/`): `PoolTree`,
  `MainAgentNode`, `SubagentNode` (frozen wire models, 192 lines) + `PoolStore`
  (899 lines, the actual yml reader/writer).
- **Framework** (`src/modex_agent/multi_agent/template_registry.py`):
  `AgentTemplateRegistry._load()` re-parses the same `templates/*.yml` files
  that `PoolStore` already reads, using ~100 lines of manual yml parsing
  instead of Pydantic validation.

`AppConfig.from_yaml()` scans `config/pools/` and loads pool.yml into
`PoolConfig` objects — the framework reads business-layer configuration files.
`PoolConfig` then has its `memory` field mutated at runtime
(`core.py:231-233` overwrites `max_context_tokens`), which only works because
`PoolConfig` is mutable.

Two symptoms: (1) `PoolConfig` and `PoolTree` describe the same pool with
overlapping but not identical fields; (2) `AgentTemplate` (runtime dataclass)
duplicates all 10 of `SubagentNode`'s fields. The bot-side `PoolStore`,
`PoolRouter`, `PoolInstance`, `PoolSessionStore` are framework-pure (zero
business imports) but live under `examples/bot_project/bot/`.

## Decision

### 1. Converge to three disk models + one runtime template

Delete `PoolConfig` and `AgentConfig`. Promote the bot-side wire models to the
framework, renaming to remove the false "Node" tree-structure implication:

| Model | Type | Location | Role |
|-------|------|----------|------|
| `PoolSpec` (was `PoolTree`) | frozen Pydantic | `multi_agent/pool_config/specs.py` | One pool's disk projection |
| `MainAgentSpec` (was `MainAgentNode`) | frozen Pydantic | same | Main agent's editable fields |
| `SubagentSpec` (was `SubagentNode`) | frozen Pydantic | same | Subagent's editable fields (no `approval`/`experience` — subagents never have these) |
| `AgentTemplate` | @dataclass | `multi_agent/template.py` | Runtime: wraps `SubagentSpec` + baked `memory`/`skills`, carries `materialize()` |

`AgentTemplate` fields: `spec: SubagentSpec` + `memory: MemoryConfig | None` +
`skills: SkillsConfig | None`. All field accesses go through `self.spec.*`
(no property shims, no backward-compat layer). `materialize()` internal
`self.agent_name` → `self.spec.agent_name` etc. (~15 sites).

### 2. PoolAssemblyDeps — runtime-only dependency injection

Replace the runtime params previously held by `PoolConfig`/`AgentConfig` with
a frozen Pydantic value object:

```python
class PoolAssemblyDeps(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    memory: MemoryConfig | None = None
    media: MediaConfig = Field(default_factory=MediaConfig)
    experience: ExperienceConfig | None = None
```

`memory` replaces both `PoolConfig.memory` and `AgentConfig.memory` (which were
the same baked `main_agent_memory()` default — the two-field split was a
historical accident; `AgentConfig.memory` was always `None` on the main-agent
path). The business layer injects `max_context_tokens` at construction time
(through `model_copy`) instead of mutating `PoolConfig.memory` at runtime.

### 3. AppConfig strips pools

`AppConfig.pools` and `AppConfig.default_pool` are deleted. The framework no
longer reads `config/pools/`. `AppConfig.from_yaml()` deletes its ~40-line
pool.yml loading block. `MultiAgentConfig` stays (it configures runtime
session retention, not pool definitions) but loses its `default_pool` field
(business decision, not framework config). The business layer (`BotService`)
holds `dict[str, PoolSpec]` + `default_pool: str` directly.

### 4. PoolStore is the single yml reader

`PoolStore` moves to `multi_agent/pool_config/store.py`. It is the only code
that reads or writes `pool.yml` + `templates/*.yml`. `_read_subagents()` is
switched from field-by-field construction to `SubagentSpec.model_validate(raw)`
so that Pydantic's `extra="forbid"` actually validates the raw YAML dict
(field-by-field construction bypasses dict-level validation).
`AgentTemplateRegistry` no longer parses yml — it takes a `PoolStore` and
wraps each `SubagentSpec` into an `AgentTemplate` with the baked
`subagent_memory()` default. ~100 lines of manual yml parsing + `_ACCEPTED_KEYS`
validation are deleted; Pydantic's `extra="forbid"` handles unknown-key
rejection.

### 5. PoolInstance, PoolRouter, PoolSessionStore promoted

- `PoolInstance` → `multi_agent/pool_instance.py` — loses `config: PoolConfig`,
  gains `media: MediaConfig` + `subagent_count: int` (the only two values
  actually read from config). Stays `@dataclass` (rule-12: runtime object with
  connections).
- `PoolRouter` + `PoolSessionStore` → `multi_agent/pool_router.py` —
  `pools: dict[str, Any]` converges to `dict[str, PoolInstance]`.

### 6. Bot-side consumers updated (no compat shims)

Direct import path changes, no re-exports:

- `pool_config_controller.py`: `PoolStore` import path changes; peer logic
  unchanged (already converged — controller is a thin HTTP→Store mapper).
- `pool_builder.py`: `create_pool(pool_spec, assembly_deps, ...)` replaces
  `create_pool(pool_cfg, ...)`; `_require_main_agent()` deleted
  (`pool_spec.main` is direct); `_register_main_agent` reads `main_spec.*`
  and `assembly_deps.memory`.
- `pool_data.py`: `build_pool_data()` signature changes from
  `(ctx, pool_name, pool_cfg: PoolConfig, provider, memory_cfg_factory:
  Callable[[PoolConfig], MemoryConfig], ...)` to
  `(ctx, pool_name, pool_spec: PoolSpec, assembly_deps: PoolAssemblyDeps,
  provider, ...)`; `_main_agent_name(pool_cfg)` → `pool_spec.main.agent_name`;
  `_build_experience_manager(pool_cfg, ...)` → reads `assembly_deps.experience`;
  `memory_cfg_factory` deleted (memory from `assembly_deps.memory` direct).
- `wiring.py`: constructs `assembly_deps: dict[str, PoolAssemblyDeps]` from
  `pool_specs` + `BotModelConfig.max_context_tokens`; passes both to
  `create_pool`, `BackgroundTaskRunner`, `_wire_pool_to_resources`.
- `background.py`: `pools_config: dict[str, PoolConfig]` →
  `assembly_deps: dict[str, PoolAssemblyDeps]`; reads `deps.memory.dream_engine`
  and `deps.experience`.
- `web_ui_service.py`: agent→pool mapping reads `PoolStore.list_pool_names()`
  + `PoolSpec` instead of `AppConfig.pools` + `pool_cfg.agents`; the duplicate
  `AgentTemplateRegistry` construction is deleted (subagent names come from
  `PoolSpec.subagents`).
- `core.py`: `_system_prompt_for` reads `PoolSpec.main` instead of
  `pool_cfg.agents`; `max_context_tokens` mutation deleted (moved to wiring).

### 7. ExperienceConfig and MediaConfig relocated

`ExperienceConfig` moves from `ioc/configs/agent.py` to
`multi_agent/pool_config/experience.py`. `MediaConfig` moves from
`ioc/configs/pool.py` to `multi_agent/pool_config/media.py`. Their original
files are deleted along with `PoolConfig` and `AgentConfig`.

## Module optionality

The `multi_agent/` package (including the promoted `pool_config/`)
satisfies **Tier 1 (config-level optional)**: a business that never
configures pools, never imports `multi_agent`, and never passes pool specs
to assembly will not start any pool runtime. The package exists as dead
code from the business's perspective.

It does NOT satisfy Tier 2 (import-level optional): `pipeline/` has 4
runtime imports of `multi_agent` types (`AgentDescriptor`, `AgentAddress`,
`AgentMessageEnvelope`, `AgentMessageType`) for inter-agent message
handling. Moving to Tier 2 is a future direction (arguably these types
belong in `core/` rather than `multi_agent/`), not a precondition for this
ADR.

## Consequences

**Positive:**
- 6 models → 3 disk models + 1 runtime template + 1 deps value object.
  Net ~320 lines deleted.
- Single yml reader (`PoolStore`). `AgentTemplateRegistry` ~100 lines of
  manual parsing deleted.
- Framework no longer reads business config files. `AppConfig` is pure
  framework config.
- No runtime mutation of config objects (`max_context_tokens` injected at
  construction via `model_copy`).
- `AgentTemplate` has 3 fields instead of 14; no duplicated field
  definitions.
- Pool config layer is a cohesive framework module
  (`multi_agent/pool_config/`), not split across `ioc/configs/` and
  `bot/config/`.
- `PoolRouter` type-safety improves (`dict[str, PoolInstance]` vs
  `dict[str, Any]`).

**Negative:**
- `create_pool()` signature changes (two params replace one). All callers
  updated.
- `AppConfig` consumers that read `.pools` or `.multi_agent.default_pool`
  must switch to the business-held `dict[str, PoolSpec]` + `default_pool`.
- `pipeline/` → `multi_agent/` runtime imports remain (pre-existing, not
  addressed here).

**Not in scope:**
- `pipeline/` → `multi_agent/` import decoupling (future Tier 2 work).
- `pool_config_controller.py` stays in bot (HTTP REST orchestrator).
- `pool_builder.py` stays in bot (IOC factory with business-specific tool
  registration).
- `wiring.py`, `background.py`, `pool_data.py` stay in bot.
