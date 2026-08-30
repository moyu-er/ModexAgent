# HANDOFF: Unified Consumption & Convergence (Waves 0/A/B/D/E/F)

> **Status**: Implementation complete (HEAD=`448f0bb6`, 2026-08-19). This document is the implementation handoff for the unified-consumption plan `.omo/plans/unified-consumption-abdef.md`. Design authority: `SPEC.md` Errata-6 + Errata-7; decision rationale: `docs/adr/0041-plugin-unified-assembly-system.md` (Unified Consumption Realization addendum).

## 1. Implementation Overview

| Wave | Commit | Key files | Key deliverable |
|---|---|---|---|
| W0 | (pre-WE) | `tests/integration/test_production_consumption_e2e.py` (new) | Red tests T0.1/T0.2/T0.3 locking the consumption contract (custom tool/hook/strategy must reach production assembly) |
| WE | `8c363795` | `examples/bot_project/plugins/bot_strategies.py`, `bot/service/core.py`, deleted `bot/service/pool/strategy_registry.py`, `src/modex_agent/multi_agent/AGENTS.md` | `EXECUTION_STRATEGY` slot is the sole registration source; `ExecutionStrategyRegistry` derived from `SimpleFactory` instances; T0.3 reaches custom strategy gating |
| WA-A1A2 | `049a549b` | `plugins/assembly/spec.py`, `config/spec_builder.py`, `assembly/context.py`, `stages/pool_assemble.py` | `AssemblySpec` extended (`tool_supplements`/`mcp_servers`/`default_llm_provider`); `PoolRuntimeDeps` extended (`todo_store`/`root_provider`/`mcp_registry`/`emitter_factory`); Stage 3 fills them |
| WA-A3 | `0d9dadc3` | `plugins/defaults/tools.py`, `bot_strategies.py`, `bot_hooks.py`, `defaults/hooks.py` | Runtime-aware factories: bash (terminal-aware), todo_read/todo_write (todo_store), experience, `bot_default` LLM (`BotModelProvider`); stateless ACI/ast_grep supplements |
| WA-A4A5 | `b651f6dc` | `multi_agent/template.py`, `bot/service/react_strategy.py`, `bot/service/pool/pipeline_wiring.py`, `bot/service/builders.py` | Sub + main consume `AssemblySpec` component names through `ComponentRegistry` for tools/hooks/LLM/prompt/memory; hooks incremental layer (`+`/`-` syntax); T0.1/T0.2 turn green |
| WB + WF | `a24e9c4c` | `plugins/assembly/native_core.py` (new), `plugins/assembly/stages/agent_assemble.py`, `multi_agent/template.py`, `bot/service/pool/factory.py`, `bot/service/pool/pipeline_wiring.py`, `SPEC.md` (Errata-5), `plugins/AGENTS.md`, `multi_agent/AGENTS.md` | Unified core `assemble_native_agent`; Stage 4 realized (authoritative main output); `native_main` restored to 1→2→3→4; `_register_main_agent`/`_AssembledAgentStub` deleted (I4 closed); sub `materialize` delegates to core. WF folded in: per-pool interceptor chain (clone-on-config) + COMMAND_HANDLER slot-ization |
| WD | `9a4e454a` | `bot/service/core.py`, `bot/input_pipeline/assembly.py`, `examples/bot_project/plugins/im_input_stages.py`, `bot/input_pipeline/AGENTS.md`, `examples/bot_project/plugins/AGENTS.md` | `BotService` holds service-level `_service_assembly_ctx`; `build_im_pipeline`/`build_webui_pipeline` resolve stages via INPUT_STAGE slot (skeleton order code-defined); `IMInputStagesPlugin` factory signatures adapted |

## 2. 13-Slot Consumption Matrix

> **Superseded by SPEC Errata-8 (2026-08-20)**: the slot set is now 10 (`MEMORY_PROVIDER`/`SKILL_SOURCE`/`MEMORY_SYSTEM_MODIFIER` removed; `MEMORY_SYSTEM` kept and completed). See §8 below for the updated matrix with honest states. This matrix is retained as the historical record of the unified-consumption waves.

| Slot | Registrar | Consumer | State |
|---|---|---|---|
| `EXECUTION_STRATEGY` | `BotStrategiesPlugin` (react/external, `SimpleFactory`) | `PoolAssembleStage` derives `ExecutionStrategyRegistry`; gating consumes | producing-consuming |
| `TOOL` | FW `defaults/tools.py` + `BotStrategiesPlugin` (bash) + `BotHooksPlugin` (experience) | `assemble_native_agent` (`_resolve_multi`) | producing-consuming |
| `HOOK` | FW `defaults/hooks.py` + `BotHooksPlugin` | `assemble_native_agent` (`_dispatch_hooks`, `applies_to` filter + react/memory dual runner) | producing-consuming |
| `LLM_PROVIDER` | FW `defaults/llm.py` (default) + `BotStrategiesPlugin` (bot_default) | `assemble_native_agent` (`_resolve_single`; `inputs.llm_provider` pre-fill bypasses) | producing-consuming |
| `SYSTEM_PROMPT_PROVIDER` | FW `defaults/prompt.py` (file_prompt) | `assemble_native_agent` (`_resolve_single`, project_dir relative-path resolution) | producing-consuming |
| `MEMORY_SYSTEM_MODIFIER` | FW `defaults/memory.py` (main_agent_memory/subagent_memory) | `assemble_native_agent` (`_merge_memory`, single-modifier limit) | producing-consuming |
| `MEMORY_SYSTEM` | plugins on demand (no built-in factory; `DefaultPlugin` leaves empty) | `assemble_native_agent` (`spec.memory_system` → `registry.resolve` → `factory.create(config, ctx_with_llm_provider)` → `ContextManager`); None = `inputs.context_manager` path unchanged | production-consumed |
| `COMMAND_HANDLER` | FW `defaults/commands.py` + bot | `pipeline_wiring` builds per-pool `SlashCommandProcessor` (roster `commands` absent = service default) | producing-consuming |
| `INPUT_STAGE` | `IMInputStagesPlugin` (im_input/webui_input aggregate factories) | `build_im_pipeline`/`build_webui_pipeline` resolve by slot name (skeleton order code-defined) | producing-consuming |
| `MEMORY_PROVIDER` | plugins on demand | `assemble_native_agent` (`_resolve_multi`); consumed as roster references them | register-available |
| `SKILL_SOURCE` | plugins on demand | `assemble_native_agent` (`_resolve_multi`); consumed as roster references them | register-available |
| `INTERCEPTOR` | FW `defaults/interceptors.py` (tool_timeout) + bot | `pipeline_wiring` per-pool chain (no list = shared reference; list = clone + append) | per-pool chain |
| `DATA_NAMESPACE` | plugins on demand | `GraphSpecCompiler` state_schema type resolution + KVStore `TypedBundle` | graph compiler |

Machine proof: T0.1 (custom sub tool+hook), T0.2 (custom main tool+hook), T0.3 (custom strategy gating) all green. T-MEM (custom MEMORY_SYSTEM plugin replaces main+sub `ContextManager`, `test_memory_system_slot.py`) green.

## 3. Key Architecture Decisions (index)

| Decision | Location | Summary |
|---|---|---|
| D-A5 LLM layering | plan §1 D-A5; ADR-0041 addendum | FW `default` (model.yml bottom) + BIZ `bot_default` (`BotModelProvider` per-turn switch wrapper, reuses FW default product). `SpecBuilder.from_roster` gains `default_llm_provider` param; BIZ main passes `bot_default` to preserve per-turn switching. |
| D-A8 hooks incremental layer | plan §1 D-A8; ADR-0041 addendum | Roster `hooks` with `+`/`-` is an increment over the code-wired default set, not a replacement. Default set stays code-wired; core's `_dispatch_hooks` applies deltas. Full roster-ization gated on "no bot test breakage", stopped at incremental layer. |
| D-B1 unified core | plan §1 D-B1; ADR-0041 addendum; SPEC Errata-6 (b) | `assemble_native_agent` in `native_core.py` is the single slot-resolution path for both main (Stage 4) and sub (`materialize`). `NativeAssemblyInputs`/`NativeAssemblyResult` typed carriers. Per-type wiring stays at entry points. |
| D-D INPUT_STAGE skeleton | plan §1 D-D; ADR-0041 addendum; SPEC Errata-6 (d) | Stage order is code-defined and not roster-configurable. Slot-name resolution only opens "what stage to insert", not "where". `BotService` holds service-level `_service_assembly_ctx`. Empty registry raises loudly. |
| D-F interceptor/commands | plan §1 D-F; ADR-0041 addendum; SPEC Errata-6 (e) | Per-pool interceptor chain (clone-on-config, `InterceptorChain` composability verified F0). COMMAND_HANDLER slot-ization; roster `commands` absent = default processor, present = per-pool processor. |
| D-E registry derivation | plan §1 D-E; SPEC §4.3 | `core.py` derives `ExecutionStrategyRegistry` from `ComponentRegistry` EXECUTION_STRATEGY slot `SimpleFactory` instances; manual `register()` deleted; double registry eliminated. |

## 4. Leftovers

1. **Subagent LLM uses `agent_factory` default provider, not `bot_default`.** Wave B unified the core but preserved the sub default-provider path carried from Wave A. Per-turn model switching on subagents is not wired. Tracked as a deliberate carry, not a regression.
2. **4 pre-existing unrelated integration failures** (predate W0, not caused by these waves): `test_env_injection_concurrency`, `TestFactoryRouting.test_no_prefix_routes_to_litellm` (openai_provider routing), `TestQQBotServiceIntegration.test_bot_service_pool_mode_bridge_routing`, `TestQQBotServiceIntegration.test_bot_service_pool_registers_subagent_residents`.
3. **D-A8 hooks default-set roster-ization not done.** Stopped at the incremental layer (code-wired defaults + roster increments). The upgrade path requires `BotHooksPlugin` to supply `turn_outcome_notify`/`cassette_flush`/`model_choice_bind` factories with full `ctx.pool_runtime` coverage; gated on no bot test breakage.
4. **`web_ui_service` standalone mypy** retains 5 pre-existing errors outside changed lines (unrelated to WD).
5. **`graph_routes._yaml` serialization contract — deferred convergence** (added 2026-08-20, graph-display debug session). `model_dump()` emits unset optional fields (`state_schema: null`, per-node `trigger: null`), so API YAML diverges from the hand-written `config/graphs/` file shape and the noise propagates into saved files via editor round-trips (PUT writes editor text verbatim). The WebUI YAML parser now tolerates these keys and read-only rendering uses the topology endpoint, so this is hygiene, not a bug. Planned fix: `model_dump(mode="json", exclude_none=True)` — verified round-trip lossless, free-form `config` dict nulls preserved (only model fields are dropped), declarative `state_schema` blocks fully serialized; store idempotency unaffected (`save_if_changed` compares `model_dump_json()`, never `_yaml` output). Same session also flagged `InfraAssembleStage`'s `state_schema_compiler` fill as a zero-consumer dead line — tracked as plan-slot-rationalization.md §5.1 INC-4.

## 5. Final Verification Matrix

Source: WD ledger entry (`2026-08-19T06:55:26Z`), the final implementation wave.

| Suite | Result |
|---|---|
| Production consumption + boot E2E (`test_production_consumption_e2e.py` + `test_production_boot_e2e.py`) | 8 passed, 1 skipped |
| FW unit + architecture (`tests/unit/` + `tests/architecture/`) | 7917 passed, 24 skipped |
| Architecture guards (`tests/architecture/`) | 88 passed |
| Bot suite (`examples/bot_project/tests/`) | 1900 passed, 6 skipped |
| Integration (`tests/integration/` `-m integration`) | 84 passed, 4 failed (known baseline, see leftovers #2), 14 skipped |
| ruff (changed Python files) | pass |
| mypy (changed production Python files) | pass |
| LSP diagnostics (changed production + direct tests) | clean |

Red-to-green trajectory: T0.1/T0.2/T0.3 red in W0, green from `b651f6dc` (WA-A4A5). Custom interceptor red-then-green in WF. Custom input-stage red-then-green in WD.

## 6. Commit Chain

```
W0 (red tests, pre-WE)
  -> WE  8c363795  registry unification
  -> WA  049a549b  A1+A2 assembly contract layer
  -> WA  0d9dadc3  A3 runtime-dependent factories
  -> WA  b651f6dc  A4+A5 consumption switch (T0.1/T0.2/T0.3 green)
  -> WB  a24e9c4c  B1-B5 path unification + WF dead-field disposal
  -> WD  9a4e454a  INPUT_STAGE slot consumption
  ->     5d0925c4  docs wrap-up — SPEC Errata-6 + ADR-0041 addendum + HANDOFF
  ->     9e82803a  F1+F3 reviewer fixes — type safety + dead code + convergence
  ->     4482cac4  F1 re-audit fixes — builder types, constant, class, convergence
  ->     17f64914  F4 reviewer fix — doc/code consistency
  ->     fbe0fad4  F4 re-audit fix — LlmDefaults Pydantic type in SPEC
  ->     b929d873  cut C — MEMORY_SYSTEM slot (ComponentSlot enum + AssemblySpec fields)
  ->     ff79542a  cut C — SpecBuilder reads memory_system from roster
  ->     ff801ea7  cut C — native_core resolves MEMORY_SYSTEM slot → ContextManager
  ->     448f0bb6  cut C — E2E proof (custom MEMORY_SYSTEM plugin replaces main+sub ContextManager)
```

Each wave is an independent commit (revertible in isolation). The red-to-green trail is preserved in `tests/integration/test_production_consumption_e2e.py` (T0.1/T0.2/T0.3) and `tests/integration/test_memory_system_slot.py` (T-MEM).

## 7. Documents Touched by the Wrap-Up

| Document | Change |
|---|---|
| `docs/design/scope-converge/SPEC.md` | Errata-6 appended (Stage 4 做实 + native_core signature + 12-slot matrix + INPUT_STAGE skeleton + per-pool interceptor/COMMAND_HANDLER); Errata-7 appended (MEMORY_SYSTEM slot — 13th slot, consumption path, AssemblyContext.llm_provider, design rationale, E2E proof) |
| `docs/adr/0041-plugin-unified-assembly-system.md` | Unified Consumption Realization addendum appended (context/decision/consequences/alternatives) |
| `docs/design/scope-converge/HANDOFF.md` | This file (new) |
| `src/modex_agent/plugins/AGENTS.md` | `native_core` entries added to Key Types table; Stage 4 row updated to reference core delegation; slot count 12→13, MEMORY_SYSTEM entry added, native_core slot count 7→8 (Errata-7) |
| `src/modex_agent/plugins/abc.py` | `ComponentSlot` docstring: slot count 12→13 (Errata-7; now 10 per Errata-8), authoritative-set constraint updated to allow errata-governed additions |

## 8. Slot Rationalization Waves (W0-W6, 2026-08-19/20)

> Implementation record for the slot-rationalization plan (`docs/design/scope-converge/plan-slot-rationalization.md`; execution ledger `.omo/plans/slot-rationalization-steps.md`). Design authority: `SPEC.md` Errata-8; decision rationale: ADR-0041 "Slot Rationalization" addendum. Baseline `48be5c10` → wave-end `d96497d9`.

### 8.1 Commit Chain

```
W0 (inventory + red anchors)
  -> 510d59f4  w0.1 slot gate verifier (16 mechanical gates)
  -> 6324711f  w0.2 red-anchor E2E tests T-P1..T-P4
  -> afe4c74f  w0.3 architecture guard: memory must not import plugins
  -> f5faf299  w0.4 dead-surface verdicts + design plan doc
W1 (slot removal + preset relocation)
  -> e90e092a  w1.1 memory presets relocate to memory/presets.py
  -> c6a26ec0  w1.2 remove MEMORY_SYSTEM_MODIFIER slot
  -> b1471219  w1.3 remove MEMORY_PROVIDER slot; MemoryProvider ABC moves home
  -> 03e3acdf  w1.4 remove SKILL_SOURCE slot + dead trigger registrations + TriggerOverrides
W2+W3 (MEMORY_SYSTEM completion + prompt selector)
  -> a2ab864a  w2.1 memory_system_config roster face
  -> 9f01f0fb  w2.2 orphan memory hook raises ValueError
  -> 58e12f6c  w3   system_prompt_provider roster selector (priority chain)
W6 (DATA_NAMESPACE wiring)
  -> 5ac55cda  w6.1 GraphOrchestrator accepts injected state_schema_compiler
  -> 33207eca  w6.2 graph E2E: custom DATA_NAMESPACE type reaches state_schema compilation
W4 (LLM provider convergence)
  -> 7c37ad12  w4.1 sub path resolves LLM_PROVIDER at deps assembly
  -> 97760a21  w4.2 main path resolution moves to build_native_inputs; StrategyAssembly.provider + builders fallback deleted
  -> 6200a6fb  w4.3 FW multi-provider model.yml bridge deleted
  (w4.4 = no commit: all gates held, flip decision NOT triggered — recorded in the notepad)
W5 (correctness pack)
  -> 427e1000  w5.1 remove dead MemoryOverrides.max_messages config
  -> 02ff42bf  w5.2 hooks +/- merge converges into SpecBuilder._merge_hooks
  -> aa1b6a44  w5.3 sub PoolRuntimeDeps carries session_tree_manager
  -> 14d2fe5c  w5.4 same-source duplicate registration raises
  -> c8e5a716  w5.5 minor cleanups (M7/M9/M11/M3+M13/M4/M14)
  -> d96497d9  w5.6 coverage pack (_merge_memory branches, negative paths, E2E skip guards)
  -> 00f1f30e  fix(config): atomic_write mtime race (pre-existing bug fixed en route, outside the W5 sequence)
```

Each wave is an independent revertible chain (W1's four commits revert in reverse order; W5.6 depends on W1's `_merge_memory` shape).

### 8.2 Red-Anchor Trajectory

T-P1 (prompt selector), T-P2 (sub LLM slot resolution), T-P3 (`memory_system_config`), T-P4 (orphan-hook ValueError) — all red at `48be5c10`/`f5faf299` for the right reasons, green at their wave commits (T-P1 at `58e12f6c`, T-P3 at `a2ab864a`, T-P4 at `9f01f0fb`, T-P2 at `7c37ad12`), and all green together from W4 on. W6 graph E2E (`tests/integration/test_graph_data_namespace_e2e.py`) red pre-`5ac55cda` (the self-built compiler never sees registry types), green after. `python scripts/verify_slot_gates.py --check`: 0/16 at baseline → 10/16 after W1 → 13/16 after W4 → 15/16 after W5 → **16/16** at W7.3 (the last gate was a docs-only hit).

### 8.3 Updated Slot Matrix (10 slots, honest states)

| Slot | Registrar | Consumer | State |
|---|---|---|---|
| `EXECUTION_STRATEGY` | `BotStrategiesPlugin` (react/external) | `PoolAssembleStage` derives `ExecutionStrategyRegistry`; gating consumes | producing-consuming (main); sub path casts custom names — only `external` special-cased (known limitation, SPEC Errata-8 (g)) |
| `TOOL` | FW `defaults/tools.py` + bot plugins | `assemble_native_agent` (`_resolve_multi`) | producing-consuming |
| `HOOK` | FW `defaults/hooks.py` + `BotHooksPlugin` | `assemble_native_agent` (`_dispatch_hooks`, `applies_to` filter); roster `+/-` increments merged in `SpecBuilder._merge_hooks` | producing-consuming (defaults stay code-wired; roster is incremental) |
| `LLM_PROVIDER` | FW `default` (FW single-provider schema only) + `BotStrategiesPlugin` (`bot_default`) | resolved exactly once per agent at the production entry → `AgentFactory.create_agent(llm_provider=...)` override > factory default > LiteLLM | producing-consuming (single mechanism; native_core `_resolve_single` is the documented generic fallback) |
| `SYSTEM_PROMPT_PROVIDER` | FW `defaults/prompt.py` (`file_prompt`) | `assemble_native_agent` (`_resolve_single`) + roster selector: explicit `system_prompt_provider` > `system_prompt` sugar > `prompt_name`/agent-name convention | producing-consuming |
| `MEMORY_SYSTEM` | plugins on demand (`DefaultPlugin` leaves empty) | `assemble_native_agent` (`spec.memory_system` → factory → `ContextManager`); `memory_system_config` roster face; orphan memory hook → `ValueError` naming the hook | producing-consuming (on-demand; ecosystem-loss list in SPEC Errata-8 (c)) |
| `COMMAND_HANDLER` | FW `defaults/commands.py` + bot | `pipeline_wiring` per-pool `SlashCommandProcessor` (roster `commands` absent = service default) | producing-consuming |
| `INPUT_STAGE` | `IMInputStagesPlugin` (im_input/webui_input) | pipeline builders resolve by slot name; stage order code-defined, custom stages globally inserted | producing-consuming (global semantics — no per-pool YAML selection) |
| `INTERCEPTOR` | FW `defaults/interceptors.py` + bot | `pipeline_wiring` per-pool chain (no list = shared reference; list = clone + append) | per-pool chain |
| `DATA_NAMESPACE` | plugins on demand (`DefaultPlugin` leaves empty) | graph state_schema type resolution via injected `state_schema_compiler` (BIZ passes `build_state_schema_compiler(service._component_registry)`) + KVStore `TypedBundle` | graph compiler (on-demand) |

`DefaultPlugin` populated 6 {TOOL, HOOK, LLM_PROVIDER, SYSTEM_PROMPT_PROVIDER, INTERCEPTOR, COMMAND_HANDLER}, empty 4 {EXECUTION_STRATEGY, INPUT_STAGE, MEMORY_SYSTEM, DATA_NAMESPACE}; `PluginRegistrationContext` exposes 10 `register_*` methods; same-source duplicate registration raises `ValueError`, cross-source stays first-seen-wins + warning.

### 8.4 Leftovers Update

1. **#1 subagent per-turn model switching — CLOSED (W4).** The flip condition never triggered (bot suite green at every W4 commit); subagents keep the `bot_default` default and gain per-turn switching.
2. **D-A8 hooks default-set roster-ization — unchanged, still incremental-only.** Code-wired defaults + roster `+/-` increments (now merged in `SpecBuilder._merge_hooks`); full roster-ization stays gated on real demand.
3. **4 pre-existing integration failures — unchanged** (`test_env_injection_concurrency`, `TestFactoryRouting.test_no_prefix_routes_to_litellm` (openai_provider routing), `TestQQBotServiceIntegration` × 2). They predate W0 and are unrelated.
4. **Known environment failures (not caused by these waves):** 4 sandbox-network unit tests (`tests/unit/sandbox/test_guard_network.py`) fail on this machine (network-dependent, verified pre-existing at `f5faf299`); the think_tag integration tests fail in offline environments (2 tests, intermittent — 0 failures on the W5.6 wave-end run); the live-Langfuse unit tests (`tests/unit/trace/test_langfuse_query.py::test_live_*`) are flaky against a live OTLP endpoint that intermittently returns HTTP 502 (failing subset varies run to run).
5. **Known gaps recorded, not hidden (SPEC Errata-8 (g)):** InfraAssembleStage's assembled `state_schema_compiler` product was deleted in the final review — it had zero consumers, and BIZ wiring (`resources.py` `build_state_schema_compiler`) is the single construction site; `GraphOrchestrator._create_state` handles only `state_class` (state_schema specs compile but do not run through the orchestrator).
