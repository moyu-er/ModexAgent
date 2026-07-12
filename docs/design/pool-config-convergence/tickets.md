# Tickets: Pool Config Convergence and Framework Promotion

Converge the pool configuration layer into the framework as a cohesive optional
module — 6 models → 3 disk models + 1 runtime template + 1 deps value object,
net ~310 lines reduced. Reference spec: `docs/design/pool-config-convergence/PRD.md`.
Reference ADR: `docs/adr/0020-pool-config-convergence-and-framework-promotion.md`.

Work the **frontier**: any ticket whose blockers are all done. Tickets 1 and 3
have no blockers and can start in parallel immediately.

## 1. Create pool_config package with new models

**What to build:** The renamed Pydantic models (`PoolSpec`, `MainAgentSpec`,
`SubagentSpec`) and the new `PoolAssemblyDeps` value object exist in
`multi_agent/pool_config/` as new files. Also relocate `ExperienceConfig` and
`MediaConfig` into `pool_config/`. Old types (`PoolTree`, `MainAgentNode`,
`SubagentNode`, `PoolConfig`, `AgentConfig`) still exist unchanged — this is
purely additive. Unit tests verify the new models' validation, frozen, and
`extra="forbid"` behavior (including `SubagentSpec` rejecting `approval`/
`experience` keys).

**Blocked by:** None — can start immediately.

- [x] `multi_agent/pool_config/__init__.py` created with public re-exports
- [x] `pool_config/specs.py` defines `PoolSpec`, `MainAgentSpec`, `SubagentSpec`
      with frozen + `extra="forbid"`; `SubagentSpec` has NO `approval`/`experience`
      fields; `MainAgentSpec` keeps `approval` (main agents DO have approval)
- [x] `pool_config/deps.py` defines `PoolAssemblyDeps` (frozen, `extra="forbid"`):
      `memory: MemoryConfig | None`, `media: MediaConfig`,
      `experience: ExperienceConfig | None`
- [x] `pool_config/experience.py` relocates `ExperienceConfig` from
      `ioc/configs/agent.py` (old file keeps a re-export temporarily for Ticket 8
      to delete)
- [x] `pool_config/media.py` relocates `MediaConfig` from `ioc/configs/pool.py`
      (same temporary re-export)
- [x] Unit tests verify: `SubagentSpec` rejects `approval` key with validation
      error; `SubagentSpec` rejects `experience` key with validation error;
      `PoolAssemblyDeps` is frozen (mutation raises); all models reject unknown
      keys via `extra="forbid"`
- [x] Old types unchanged — no consumer broken

## 2. Move PoolStore to framework + rename models everywhere

**What to build:** `PoolStore` lives in `multi_agent/pool_config/store.py` and
uses `PoolSpec`/`MainAgentSpec`/`SubagentSpec` (the types from Ticket 1). The
old `bot/config/pool_payloads.py` and `bot/config/pool_store.py` are deleted.
All callers (`pool_config_controller.py`, `wiring.py`, `web_ui_service.py`,
tests) import from the new path and use the new type names. The disk layer is
fully converged — one YAML reader, one set of model names. No re-export shims.

**Blocked by:** Ticket 1 (needs `PoolSpec`/`MainAgentSpec`/`SubagentSpec` to exist).

- [x] `PoolStore` moved to `multi_agent/pool_config/store.py`; internal model
      references use `PoolSpec`/`MainAgentSpec`/`SubagentSpec`
- [x] `_read_subagents()` switched to `SubagentSpec.model_validate(raw)` instead
      of field-by-field construction — this makes `extra="forbid"` actually reject
      unknown yml keys (including `approval`/`experience`); Pydantic field defaults
      replace the lenient `or` fallback patterns (`raw.get("tool_preset") or
      ToolPreset.READ_WRITE` → field default `ToolPreset.READ_WRITE`)
- [x] `_extract_main_agent()` similarly switched to `MainAgentSpec.model_validate(data)`
      if feasible; otherwise keep field-by-field (main agent yml is flat, not nested)
- [x] `bot/config/pool_payloads.py` deleted
- [x] `bot/config/pool_store.py` deleted
- [x] `pool_config_controller.py` imports `PoolStore` + wire models from
      `multi_agent.pool_config` (peer logic unchanged — already converged)
- [x] `wiring.py` imports `PoolStore` + wire models from new path
- [x] `web_ui_service.py` imports from new path
- [x] All test files import from new path; `test_pool_store.py` uses renamed
      types throughout
- [x] `bot/config/__init__.py` updated (no re-exports of deleted modules)
- [x] Full test suite green — disk round-trip, peer validation, rename/delete
      all work with renamed types; `extra="forbid"` rejects unknown keys in
      `templates/*.yml` (test with a template containing `approval:` block)

## 3. Move PoolRouter + PoolSessionStore + PoolInstance to framework

**What to build:** `PoolRouter`, `PoolSessionStore`, and `PoolInstance` live in
`multi_agent/`. All callers import from the new paths. `PoolRouter`'s `pools`
parameter type changes from `dict[str, Any]` to `dict[str, PoolInstance]`.
`PoolInstance` still holds `config: PoolConfig` at this stage (field change
comes in Ticket 6). The routing layer is framework-resident.

**Blocked by:** None — can start immediately (independent file move, doesn't
need new model types from Ticket 1).

- [x] `PoolRouter` + `PoolSessionStore` moved to `multi_agent/pool_router.py`
- [x] `PoolInstance` moved to `multi_agent/pool_instance.py`
- [x] `bot/service/pool_router.py` deleted
- [x] `bot/service/pool_instance.py` deleted
- [x] `PoolRouter.__init__` `pools` parameter typed as `dict[str, PoolInstance]`
      (was `dict[str, Any]`)
- [x] All callers (`wiring.py`, `web_ui_service.py`, `dispatch.py`, tests)
      import from new paths
- [x] Full test suite green — session→pool routing, workspace dispatch work
      with framework-resident types

## 4. Converge AgentTemplate to wrap SubagentSpec

**What to build:** `AgentTemplate` has 3 fields (`spec: SubagentSpec`, `memory`,
`skills`) instead of 14. `materialize()` reads disk fields via `self.spec.*`.
All external access sites (`template.agent_name` → `template.spec.agent_name`)
updated. Dead fields (`approval`, `experience`) deleted from `AgentTemplate`.
No property shims — direct field access through `self.spec.*` everywhere.

**Blocked by:** Ticket 1 (needs `SubagentSpec` to exist as the wrapped type).

- [x] `AgentTemplate` fields reduced to: `spec: SubagentSpec`,
      `memory: MemoryConfig | None = None`, `skills: SkillsConfig | None = None`
- [x] `AgentTemplate.approval` deleted (subagents never have approval)
- [x] `AgentTemplate.experience` deleted (subagents never have experience review)
- [x] 10 duplicated fields deleted (`agent_name`, `description`, `max_steps`,
      `tool_preset`, `tool_supplements`, `context_mode`, `system_prompt_mode`,
      `fork_max_messages`, `mcp`)
- [x] `materialize()` internal `self.<field>` → `self.spec.<field>` for all 10
      disk fields (~15 sites); `self.memory` / `self.skills` unchanged
- [x] `AgentTemplateRegistry._load()` constructs `AgentTemplate(spec=SubagentSpec(...), ...)`
      instead of passing individual fields (temporary — full registry convergence
      in Ticket 5; this ticket keeps the yml parsing but constructs with `spec=`)
- [x] All external access sites updated: `template.agent_name` →
      `template.spec.agent_name` (~8 sites across framework + bot + tests)
- [x] Full test suite green — subagent materialization, tool building, MCP
      loading, FORK context, APPEND prompt all work via `self.spec.*`

## 5. Converge AgentTemplateRegistry to use PoolStore

**What to build:** `AgentTemplateRegistry` constructor takes `PoolStore` instead
of `project_dir`. The ~100-line manual YAML parsing in `_load()` is deleted —
the registry calls `pool_store.list_pool_names()` + `read_pool()` and wraps each
`SubagentSpec` into `AgentTemplate`. `_ACCEPTED_KEYS` frozenset and manual
enum parsing/fallback logic deleted (Pydantic validation handles this). One
YAML parsing path remains: `PoolStore`.

**Blocked by:** Ticket 2 (needs `PoolStore` in framework), Ticket 4 (needs
`AgentTemplate` to accept `spec: SubagentSpec`).

- [x] `AgentTemplateRegistry.__init__` signature:
      `(pool_store: PoolStore, *, default_subagent_memory: MemoryConfig | None = None)`
- [x] `_load()` rewritten: iterate `pool_store.list_pool_names()` →
      `read_pool(name)` → `tree.subagents` → construct
      `AgentTemplate(spec=sub_spec, memory=default_subagent_memory)`
- [x] ~100 lines of manual YAML parsing deleted (enum parsing, fallback logic,
      `_ACCEPTED_KEYS` frozenset, per-field `raw.get()` calls)
- [x] `pool_builder.py` constructs `AgentTemplateRegistry(pool_store, ...)` instead
      of `AgentTemplateRegistry(project_dir, ...)`
- [x] `web_ui_service.py` — the duplicate `AgentTemplateRegistry` construction
      for agent→pool mapping is still present (deleted in Ticket 7); for now
      it uses the new `PoolStore`-based constructor
- [x] Tests verify: templates loaded from `PoolStore`; unknown keys rejected by
      Pydantic `extra="forbid"` (not by manual `_ACCEPTED_KEYS` check)
- [x] Full test suite green

## 6. Cutover create_pool to PoolSpec + PoolAssemblyDeps

**What to build:** `create_pool()` signature changes from
`create_pool(pool_cfg: PoolConfig, ...)` to
`create_pool(pool_spec: PoolSpec, assembly_deps: PoolAssemblyDeps, ...)`.
`wiring.py` constructs `PoolAssemblyDeps` (with `max_context_tokens` injected
via `model_copy`) and passes both to `create_pool`. `BackgroundTaskRunner`
takes `dict[str, PoolAssemblyDeps]`. `_wire_pool_to_resources` takes
`deps: PoolAssemblyDeps`. `BotService` holds `pool_specs: dict[str, PoolSpec]`
loaded from `PoolStore`. `PoolInstance` loses `config` and gains `media` +
`subagent_count`. `AppConfig.pools` still exists but becomes dead config
(deleted in Ticket 8).

**Blocked by:** Ticket 1 (PoolAssemblyDeps), Ticket 2 (PoolStore in framework),
Ticket 3 (PoolInstance in framework), Ticket 4 (AgentTemplate converged),
Ticket 5 (AgentTemplateRegistry converged).

- [x] `create_pool()` signature: `(pool_name: str, pool_spec: PoolSpec,
      assembly_deps: PoolAssemblyDeps, *, ...)` — `pool_cfg: PoolConfig` removed
- [x] Internal reads: `pool_cfg.name` → `pool_name`; `pool_cfg.memory` →
      `assembly_deps.memory`; `pool_cfg.media` → `assembly_deps.media`;
      `_require_main_agent(pool_cfg)` deleted → `pool_spec.main` direct;
      `main_cfg.<field>` → `main_spec.<field>`; `main_cfg.memory` →
      `assembly_deps.memory`; `pool_cfg.agents` count → `len(pool_spec.subagents)`
- [x] `_register_main_agent` signature: drops `pool_cfg: PoolConfig` and
      `main_cfg: AgentConfig`; takes `main_spec: MainAgentSpec` +
      `assembly_deps: PoolAssemblyDeps`; `descriptor.memory_config = main_cfg.memory`
      → `descriptor.memory_config = assembly_deps.memory`
- [x] `_build_terminal_manager` reads `main_spec.use_terminal` +
      `main_spec.terminal_visibility` instead of iterating `pool_cfg.agents`
- [x] `_wire_main_pipeline` signature: drops `pool_cfg`; reads from `pool_spec` /
      `assembly_deps` as needed
- [x] `PoolInstance` loses `config: PoolConfig`; gains `media: MediaConfig`
      (from `assembly_deps.media`) + `subagent_count: int`
      (from `len(pool_spec.subagents)`)
- [x] `wiring.py`: constructs `pool_specs: dict[str, PoolSpec]` from `PoolStore`;
      constructs `assembly_deps: dict[str, PoolAssemblyDeps]` with
      `_main_agent_memory(max_context_tokens)` via `model_copy`; passes both
      to `create_pool`, `BackgroundTaskRunner`, `_wire_pool_to_resources`
- [x] `build_pool_data()` signature: `pool_cfg: PoolConfig` +
      `memory_cfg_factory: Callable[[PoolConfig], MemoryConfig]` →
      `pool_spec: PoolSpec` + `assembly_deps: PoolAssemblyDeps`;
      `_main_agent_name(pool_cfg)` → `pool_spec.main.agent_name` direct;
      `_build_experience_manager(pool_cfg, ...)` → reads `assembly_deps.experience`;
      `memory_cfg_factory(pool_cfg)` deleted → `assembly_deps.memory` direct
- [x] `BackgroundTaskRunner` signature: `pools_config: dict[str, PoolConfig]` →
      `assembly_deps: dict[str, PoolAssemblyDeps]`; reads
      `deps.memory.dream_engine` + `deps.experience`
- [x] `_wire_pool_to_resources` signature: `pool_cfg: PoolConfig` →
      `deps: PoolAssemblyDeps`; reads `deps.experience`
- [x] `web_ui_service.py:620` `pi.config.media` → `pi.media`;
      `core.py:388` `pi.config.agents` count → `pi.subagent_count`
- [x] Bot test files that construct `PoolConfig`/`AgentConfig` directly updated
      to construct `PoolSpec`/`MainAgentSpec` + `PoolAssemblyDeps` instead:
      `test_agent_workspace_ownership.py`, `test_wire_main_pipeline_approval.py`,
      `test_pool_builder_model_choice.py`, `test_mcp_resilience.py`
- [x] `_build_tools` signature: `pool_cfg: PoolConfig` + `main_cfg: AgentConfig`
      → `main_spec: MainAgentSpec` + `assembly_deps: PoolAssemblyDeps`; internal
      reads `main_cfg.tool_supplements` → `main_spec.tool_supplements`;
      `main_cfg.name` → `main_spec.agent_name`
- [x] Framework test `tests/unit/bot/test_pool_builder_todo_path.py` updated:
      calls `_build_tools(pool_spec=..., main_spec=..., assembly_deps=...)`
      instead of `_build_tools(pool_cfg=PoolConfig(...), main_cfg=AgentConfig(...))`
- [x] `AppConfig.pools` still exists but `create_pool` no longer reads it
- [x] Full test suite green — pool assembly, main agent registration, terminal
      building, memory initialization, dream engine, experience curator all
      work via `PoolSpec` + `PoolAssemblyDeps`

## 7. Migrate remaining consumers off AppConfig.pools

**What to build:** `web_ui_service.py` builds the agent→pool mapping from
`PoolStore` + `PoolSpec` instead of `AppConfig.pools` + `pool_cfg.agents`. The
redundant `AgentTemplateRegistry` construction in `web_ui_service.py` is deleted
(subagent names come from `PoolSpec.subagents`). `core.py:_system_prompt_for`
reads `PoolSpec.main` instead of `pool_cfg.agents`. `core.py:_load_model_config`'s
`max_context_tokens` mutation loop is deleted. All remaining `AppConfig.pools`
consumers are migrated — `AppConfig.pools` becomes unused dead code.

**Blocked by:** Ticket 6 (needs `create_pool` cutover + `PoolInstance` fields
changed, so `web_ui_service.py` can read `pi.media` / `pi.subagent_count`).

- [x] `web_ui_service.py:684-688`: agent→pool mapping reads `PoolStore.list_pool_names()`
      + `PoolSpec.main.agent_name` + `[s.agent_name for s in spec.subagents]`
      instead of `AppConfig.pools` + `pool_cfg.agents`
- [x] `web_ui_service.py:691-700`: redundant `AgentTemplateRegistry` construction
      deleted (subagent names already in mapping from `PoolSpec.subagents`)
- [x] `web_ui_service.py:385,658`: `default_pool` read from `BotService`-held
      value instead of `app_cfg.multi_agent.default_pool`
- [x] `web_ui_service.py:686`: `self._app_config.pools.items()` loop deleted
- [x] `core.py:170`: `default_pool` read from `BotService`-held value instead
      of `self._app_config.multi_agent.default_pool`
- [x] `core.py:231-233`: `max_context_tokens` mutation loop deleted (moved to
      `wiring.py` in Ticket 6)
- [x] `core.py:281,391,404`: `app_config.pools` references replaced with
      `BotService`-held `pool_specs`
- [x] `core.py:_system_prompt_for`: `pool_cfg.agents` filter → `pool_spec.main`
      direct access
- [x] `BackgroundTaskRunner` construction in `wiring.py:346`:
      `pools_config=pool_configs` already changed in Ticket 6 — verify no
      stale `AppConfig.pools` reference remains
- [x] Grep confirms zero remaining `app_config.pools` or `app_cfg.pools` or
      `.multi_agent.default_pool` references in bot code
- [x] Full test suite green

## 8. Delete PoolConfig + AgentConfig + strip AppConfig

**What to build:** `PoolConfig` deleted (file removed). `AgentConfig` deleted
(file removed; `ExperienceConfig` already in `pool_config/` from Ticket 1;
temporary re-exports deleted). `MediaConfig` temporary re-export deleted.
`AppConfig.pools` and `MultiAgentConfig.default_pool` fields deleted.
`AppConfig.from_yaml()`'s pool.yml loading block + `_MAIN_AGENT_YAML_FIELDS`
+ `_validate_pool_name()` deleted. No remaining references to `PoolConfig` or
`AgentConfig` anywhere. The framework no longer reads `config/pools/`. The
convergence is complete.

**Blocked by:** Ticket 6 (create_pool no longer uses PoolConfig), Ticket 7
(all consumers migrated off AppConfig.pools).

- [x] `ioc/configs/pool.py` deleted entirely (PoolConfig + MediaConfig both
      relocated to `pool_config/`)
- [x] `ioc/configs/agent.py` deleted entirely (AgentConfig deleted;
      ExperienceConfig already in `pool_config/`; `DEFAULT_SYSTEM_PROMPT`
      is already duplicated in `ioc/factories/descriptors.py:86` — just
      delete the duplicate definition from agent.py, no move needed)
- [x] Temporary re-exports of `ExperienceConfig` / `MediaConfig` from old
      locations deleted
- [x] `AppConfig` model: `pools: dict[str, PoolConfig]` field deleted
- [x] `MultiAgentConfig`: `default_pool: str` field deleted; only
      `session_retention` remains
- [x] `AppConfig.from_yaml()`: pool.yml scanning block deleted (~40 lines);
      `_MAIN_AGENT_YAML_FIELDS` constant deleted; `_validate_pool_name()`
      deleted (validation lives in `PoolStore`)
- [x] `ioc/configs/__init__.py` updated — no exports of deleted types
- [x] Grep confirms zero `PoolConfig` references in entire codebase
- [x] Grep confirms zero `AgentConfig` references in entire codebase
- [x] Grep confirms zero `AppConfig.pools` or `.default_pool` references
- [x] `test_app_config.py` updated: asserts `AppConfig` has no `pools` field;
      asserts `MultiAgentConfig` has no `default_pool`; asserts `from_yaml()`
      does not read `config/pools/`
- [x] **Test files DELETED entirely** (exist solely to test deleted models):
      `tests/framework/configs/test_pool_loader.py` (tests PoolConfig +
      from_yaml pool loading), `tests/framework/configs/test_agent_config.py`
      (tests AgentConfig), `tests/unit/ioc/test_agent_config.py` (tests
      AgentConfig), `tests/unit/bot/test_pool_isolation.py` (tests PoolConfig
      name decoupling)
- [x] **Test files PARTIAL delete/update:**
      `tests/unit/ioc/test_pool_config_media.py` — DELETE
      `TestPoolConfigMediaWiring` class; KEEP `TestMediaConfigDefaults`;
      update import `modex_agent.ioc.configs.pool.MediaConfig` →
      `modex_agent.multi_agent.pool_config.media.MediaConfig`; delete
      `PoolConfig`/`AgentConfig` imports
- [x] `tests/unit/ioc/test_integration.py` — DELETE `TestAgentConfigEdgeCases`
      class; DELETE `test_pools_carried_through` method; UPDATE
      `test_extra_fields_ignored`: `assert cfg.pools == {}` → assert AppConfig
      has no `pools` field; KEEP `TestDeepMergeEdgeCases` +
      `TestSafetyConfigDefaults`
- [x] `tests/unit/bot/test_pool_router.py` — DELETE `TestPoolNameValidation`
      class (tests deleted `_validate_pool_name`); DELETE dead
      `_make_pool_config` helper + `PoolConfig`/`AgentConfig` imports (routing
      tests use `_FakePoolInstance`, unaffected); DELETE `_validate_pool_name`
      import from `ioc.configs.app`
- [x] **Test files TRIVIAL migration:**
      `tests/unit/commands/test_skill_command_handler.py:113-115` — replace
      `AgentConfig(name="main", role="main")` with direct string `"main"`
      (only uses `.name`); delete `AgentConfig` import
- [x] `tests/unit/workspace/test_isolation.py:35` — string reference
      `"PoolConfig"` → `"PoolSpec"` (or whatever the isolation test checks)
- [x] Full test suite green — the convergence is complete, framework no longer
      reads business configuration files
