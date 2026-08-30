# ADR-0041: 插件化统一 Agent 装配系统

> **Authoritative spec**: `docs/design/scope-converge/SPEC.md` — this ADR records the decision rationale only. Implementation details live in the SPEC.

## Context

### Problem

ModexAgent 的 agent 装配代码散落在 `examples/bot_project` 的 11 个 pool/ 模块 + 4 个 wiring/ 模块 + builders.py + memory_defaults.py 中。当前有 7 种 agent 类型（native main/sub、external main/sub、experience reviewer、session compactor、core memory consolidator），各走不同装配路径，没有统一装配器。现有 plugin 体系（`PluginManager`/`PluginContext`/`PluginLoader`/`PluginIntegration`）是空壳——`enabled: False` 初始化，`inject_*` 零外部调用者。

用户要求进行架构升级，实现"插件化/配置化统一处理和配置 agent"。经过三路源码级调查（参考项目 ctx 装配机制、input_pipeline 模式、workspace/pool plugin 化可行性）和 17 个 deliberate 决议（Q1-Q17）+ 闭包检查（F1-F4/C1-C4/G1-G9），形成完整闭环设计。

### Key findings from investigation

1. **参考项目没有多阶段管道式装配**——参考项目的装配是反应式依赖图（Fiber 驱动），声明顺序"无关紧要"。参考项目的 Python SDK 只是 subprocess + JSON-RPC 客户端，零 Python 投影。
2. **input_pipeline 是请求处理管道（process-in-place），不是对象构造管道**——机制可迁移但形态不可迁移。最终采用 spec→builder→agent 三层分离形态。
3. **workspace/pool 已经是 plugin 形状**——`ResourceFactory[R]` 和 `ExecutionStrategy` ABC 已可插拔；但骨架（lifecycle/routing/paths/session-mapping）是框架级硬耦合。
4. **旧 plugin 体系是空壳**——`enabled: False` 初始化，`inject_*` 零调用者。

### Constraints

- 类型纪律不放松：组件 = ABC 实例，配置 = frozen Pydantic `extra="forbid"`，登记处 = 唯一信任边界，零 Protocol
- 收敛纪律：模块内一次到位不留双路径，跨模块按依赖序分步
- 重启生效，不做运行时热插拔/HMR/沙箱自挂载
- modex_graph 保持独立包（架构守卫强制不 import modex_agent）
- bot_project 终局 = 默认组件包 + 默认点名册

## Decision

### 五条统一原则

1. **一个 ComponentRegistry** — 全局单例，12 槽位 StrEnum 闭集，所有组件工厂按名解析
2. **一套 YAML 配置** — pool.yml + agent template + graph spec，都引用 registry 组件名
3. **一条 AssemblyPipeline** — main/sub/external 全走同一管道（spec→builder→agent 三层分离），不同 stage 子集
4. **一套类型登记** — DATA_NAMESPACE 的 Pydantic model 既用于 KVStore 插件数据，也用于图 state schema
5. **骨架固定** — workspace/pool 的 lifecycle/routing/paths/session-mapping 不可替换

### AssemblyPipeline 三层分离

- **AssemblySpec**（frozen 输入）：组件名引用 + config 值 + workspace context 引用
- **AssemblyBuilder**（可变累积区）：构建中的实例（ToolManager、HookRunner、MemorySystem 等）
- **AssembledAgent**（输出）：最终组装的 agent 实例
- Stage 签名：`process(spec, builder, ctx) -> None`——stage 修改 builder 不返回 spec

这是全新设计，非参考项目移植。5 个默认 stage（删除了闭包检查中发现的 SpecialAgentStage 和 GraphAssembleStage——前者完全在管道外，后者归入 InfraAssembleStage）。

### ComponentFactory 统一存储

ComponentRegistry 的每个条目都是 `ComponentFactory`——统一为工厂形。无依赖组件用 `SimpleFactory(instance)` 包装。工厂 `create(config, ctx)` 从 `AssemblyContext` 获取 per-pool 参数（tree_manager、control_channel 等）。

### 旧 plugin 体系全删 + collect-then-inject 推翻

02 票 Q2 的 "collect-then-inject" 被 Q4 推翻——旧体系的 inject 路径从未实现。新设计：stage 从 registry 按名解析工厂，调用 `factory.create()` 得到实例，直接注入 builder。

### workspace/pool 骨架固定 + AssemblyContext 分层

骨架保持框架级。AssemblyContext 分全局层（ComponentRegistry，不随驱逐销毁）+ workspace 层（resources + pool 运行时，随驱逐销毁、重新 materialize 时重建）。工厂全局共享无状态，create 出的实例 per-pool。

### 特殊 agent 例外

experience reviewer / session compactor / core memory consolidator / archive summarizer 不走 AssemblyPipeline（tool 特殊配置不可组件化）。它们的触发机制通过 plugin 配置控制。例外封闭（4 个内置 agent，数量固定）。

### GraphSpec 声明式 state schema

modex_graph 的 `GraphSpec` 新增 `state_schema: dict[str, FieldSpec]`。编译逻辑通过 `state_schema_compiler` 注入点由 modex_agent 侧提供。modex_graph 保持独立包。

## Consequences

### Positive

- 7 种 agent 类型收敛到 1 条装配路径（特殊 agent 4 个例外封闭）
- 用户写 YAML + 注册插件即可定义新 agent，不改框架代码
- 三层分离（spec/builder/agent）让装配过程可审计、可测试
- ComponentFactory 统一存储形态消除了实例 vs 工厂的歧义
- 插件数据类型与图 state schema 共用类型登记
- A→B stage 重组路径保留（AssemblySpec 按关注点设计）

### Negative

- 新增 ~2800 行框架代码 + ~2500 行测试
- ComponentFactory 统一形态对无依赖组件增加 SimpleFactory 包装的样板代码
- 现有测试需适配新装配路径
- 特殊 agent 例外是设计中的不对称（虽然封闭）
- modex_graph 需要新增 `state_schema` 字段 + `FieldSpec` model + `state_schema_compiler` 注入点
- v1 external provider 硬编码 `provider_kind` 是临时不一致（v2 落 EXTERNAL_PROVIDER 槽位时修正）

### Neutral

- 参考项目的反应式依赖图在 Python 里由 AssemblyPipeline 做线性投影
- 组件工厂无状态的假设意味着所有运行时状态走 KVStore

## Related

- `docs/design/scope-converge/SPEC.md` — 完整设计文档（authoritative，含闭包检查修正 §12/§16）
- `docs/design/scope-converge/issues/01-08` — 8 张 wayfinder ticket（all closed）
- ADR-0020 — pool config convergence（前驱）
- ADR-0025 — execution strategy abstraction（前驱）
- ADR-0033 — generalized graph engine（图接缝基础）
- ADR-0039 — turn context configuration pipeline（stage 模式参照）

## Architecture Selection Boundary (added 2026-08-18)

### Decision: keep our architecture, do NOT adopt the reference project's architecture

经过参考项目源码级审计确认：参考项目的 workspace/pool 概念与我们的**根本不同**。参考项目的 workspace 是数据记录（path + sessionIds CRUD），不是运行时容器。参考项目无 pool/router/bus/poller/tree 等等价物。两种架构是不同的运行时模型。

**保持当前架构**（共享总线 + 持久池 + LRU 容器）。完整理由、优势、缺点、绝对不做清单见 SPEC §17。

### Our architecture's strengths (why we keep it)

- 持久池零延迟（agent 预构建，首次 turn 直接执行）
- LRU eviction（多 workspace 自动内存管理）
- In-flight turn protection（防止并发 turn 破坏状态）
- 跨 pool peer 通信（send_to_peer）
- 显式 session 树（O(1) 父子查询）
- poll-driven 收敛（单 InboxPoller 统一调度）
- 已验证生产运行

### Our architecture's weaknesses (honestly acknowledged)

- 8 个骨架组件框架级硬耦合（不可独立替换）
- 组件交互复杂（InboxPoller + AgentMessageBus + PoolRouter + SessionTreeManager 四者耦合）
- 插件化边界受骨架限制
- 无 Fiber 式自动 disposal（手写 cleanup）
- 首次 pool 创建有成本（预构建所有 main agent）

### What we absolutely do NOT do (N1-N24)

24 项明确排除清单见 SPEC §17.5。涵盖：不采用参考项目架构、不实现参考项目的 Fiber/Context/Reflect 机制的 Python 等价物、不做热插拔/HMR、不做 per-session 配置隔离、不做动态 agent 定义、不开放 5 个延后槽位、不替换 6 个骨架组件、不将特殊 agent 走 AssemblyPipeline、不做 per-step model selection（框架层）等。

## Unified Consumption Realization (added 2026-08-19, HEAD=9a4e454a)

> **Authoritative detail**: `docs/design/scope-converge/SPEC.md` Errata-6. This section records the decision rationale for the unified-consumption waves (WE/WA/WB/WF/WD) that made the 12-slot matrix production-reachable.

### Context

Errata-5 (2026-08-19) converged the AssemblyPipeline to a main-agent orchestrator: `native_main` walked stages 1→2→3 (Stage 4 removed because its output was discarded, I4), and subagents were built directly by `AgentTemplate.materialize` without the pipeline. That convergence eliminated the union-type strategy dispatch (I12) and the stub-masked self-build branch (I8), but it left three gaps:

1. main and sub still had two parallel inline assembly bodies (7-slot resolution + memory merge + hook dispatch duplicated).
2. `_register_main_agent` still created the real main agent, so Stage 4's discard was a symptom, not a fix. I4 stayed open.
3. The 12-slot matrix was a declaration, not a runtime fact: `INTERCEPTOR`/`COMMAND_HANDLER`/`INPUT_STAGE` were dead `MainAgentSpec` fields that `pipeline_wiring` never consumed; `MEMORY_PROVIDER`/`SKILL_SOURCE` were "register-available" but unverified in production.

The unified-consumption plan (`.omo/plans/unified-consumption-abdef.md`) was approved to close these gaps across five waves.

### Decision

**D-B1 — unified core.** A single function `assemble_native_agent` in `src/modex_agent/plugins/assembly/native_core.py` resolves the 7 per-agent slots (TOOL/LLM_PROVIDER/SYSTEM_PROMPT_PROVIDER/MEMORY_PROVIDER/SKILL_SOURCE/MEMORY_SYSTEM_MODIFIER/HOOK), merges memory, dispatches hooks (react/memory dual runner with `applies_to` filtering), constructs the descriptor, and calls `agent_factory.create_agent`. Typed carriers `NativeAssemblyInputs` (regular class, ~25 fields, `__init__` assignment) and `NativeAssemblyResult` (descriptor + instance + resolved components) replace the scattered ctor params. Per-type wiring (governance/approval/interceptor/command/experience/cassette/emitter) stays at the entry points, not in the core.

**Stage 4 realized — native_main restored to 1→2→3→4.** `AgentAssembleStage.process` calls the core; its `result.instance` is the authoritative main agent. `create_pool` consumes that instance. `_register_main_agent` and `_AssembledAgentStub` are deleted, closing I4 permanently. This reverses Errata-5's Stage 4 removal: the removal was justified only while the stage's output was discarded, and the core extraction requires the stage to own the authoritative output. `external_main` stays at 1→2→3 (the strategy owns external build).

**Two entry timings, one core.** main-pipeline (Stage 4 assembles `NativeAssemblyInputs` from `PoolAssemblyContext` + `StrategyAssembly` side products) and sub-materialize (`AgentTemplate.materialize` assembles inputs from `AgentMaterializeDeps`, passes `parent_session` + `invocation_id`). The per-invocation data flows through keyword args, not the per-pool `AssemblyContext`, preserving Errata-5's sub-direct-build decision while converging the assembly body.

**12-slot consumption matrix realized (SPEC Errata-6 table).** 8 producing-consuming slots (EXECUTION_STRATEGY/TOOL/HOOK/LLM_PROVIDER/SYSTEM_PROMPT_PROVIDER/MEMORY_SYSTEM_MODIFIER/COMMAND_HANDLER/INPUT_STAGE), 2 register-available (MEMORY_PROVIDER/SKILL_SOURCE consumed as roster references them), INTERCEPTOR as per-pool chain, DATA_NAMESPACE for graph state-schema type resolution. T0.1/T0.2/T0.3 red tests turned green: custom tool/hook/strategy referenced from roster YAML reach the production assembly product. This is the machine proof of SPEC §1's completion criterion.

**D-A5 — LLM provider layering.** FW `default` factory (bottom: model.yml parse) and BIZ `bot_default` factory (wrapper: `BotModelProvider` for per-turn model switching, reusing the FW `default` product internally). `SpecBuilder.from_roster` gains `default_llm_provider: str = "default"`; BIZ main passes `"bot_default"` to preserve per-turn switching. Empty template keeps `BotModelProvider` behavior; roster `llm_provider: default` reaches the bare bottom provider.

**D-A8 — hooks incremental layer.** Roster `hooks` with `+`/`-` syntax is an increment over the code-wired default set, not a replacement. The default set stays code-wired in `_wire_main_pipeline` (main) and `materialize` (sub); `SpecBuilder._merge_hooks` applies the increments at spec build time — `from_roster` produces the final hook list, and the core's `_dispatch_hooks` resolves those names only. The upgrade path to full default-set roster-ization was gated on "no bot test breakage" and stopped at the incremental layer (documented).

**D-D — INPUT_STAGE skeleton order.** `build_im_pipeline`/`build_webui_pipeline` resolve stages by INPUT_STAGE slot name, but the stage order is code-defined and not roster-configurable. Custom stage names are deterministically sorted into code-defined extension points; they cannot reorder the built-in skeleton. `BotService` holds a service-level `_service_assembly_ctx` (registry + home workspace_ctx) so the pipeline builders resolve stages through the real ComponentRegistry. An empty INPUT_STAGE registry raises `ComponentNotFoundError` at `set_channel` rather than falling back to direct construction.

**D-F — per-pool interceptor chain + COMMAND_HANDLER slot.** Pool.yml `interceptors` projects into `AssemblySpec`; `pipeline_wiring` clones the shared `InterceptorChain` only when additions are configured and appends roster-specified interceptors (resolved via the INTERCEPTOR slot). No list = shared chain reference (behavior unchanged, cross-pool isolation preserved). `InterceptorChain` composability verified (F0). `CommandDispatchStage` handlers resolve from the COMMAND_HANDLER slot by name; roster `commands` absent = service default processor, present = per-pool `SlashCommandProcessor` built from named handlers. The IM `/cd`/`/stop` input-stage channel is untouched (dual-channel现状 documented).

### Consequences

**Positive**
- SPEC §1 honored in production: user YAML + plugin (restart-effective) drives custom tool/hook/llm/prompt/memory/strategy/input-stage/interceptor/command through real assembly paths. T0.1/T0.2/T0.3 green is the machine proof.
- Single `assemble_native_agent` core eliminates the main/sub dual inline assembly body. Both entry timings flow through one slot-resolution path.
- 12 slots all production-reachable; no dead roster fields remain (`interceptors`/`commands`/INPUT_STAGE all consumed).
- I4 (`_register_main_agent` duplicate creation) permanently closed; I8 (stub-masked self-build) stays closed from Errata-5.

**Negative**
- sub LLM still uses the `agent_factory` default provider, not `bot_default`. Wave B unified the core but preserved the sub default-provider path (carried from Wave A); per-turn model switching on subagents is not wired. Documented as a leftover.
- 4 pre-existing unrelated integration failures retained (not caused by these waves): `env_injection_concurrency`, `openai_provider` routing, `qq_bot_service` × 2. They predate W0 and are tracked separately.
- The hooks default-set roster-ization upgrade (D-A8) did not happen; hooks remain a hybrid (code-wired defaults + roster increments). This is a deliberate stop, not a regression.

**Neutral**
- Stage 4 is authoritative for `native_main` only. `external_main` still walks 1→2→3 and the strategy owns the external build. The core serves native paths; external agents bypass it.
- subagent still does not walk the pipeline (Errata-5 retained), but its `materialize` body now delegates to the same core. The pipeline is the main orchestrator; the core is the convergence point.

### Alternatives considered

- **Keep Errata-5's Stage 4 removal (main built by `strategy.assemble_main` only).** Rejected. It left `_register_main_agent` duplicate creation (I4) and the main/sub dual assembly body intact. The core extraction required Stage 4 to own the authoritative main output; without that, the core would be a second path alongside the strategy build, recreating the divergence.
- **Full default-hook-set roster-ization (D-A8 upgrade path).** Not taken. It would require `BotHooksPlugin` to supply `turn_outcome_notify`/`cassette_flush`/`model_choice_bind` factories with full `ctx.pool_runtime` param coverage, and the gate was "no breakage of the 1900 bot tests". Stopped at the incremental layer with the code-wired defaults retained. The upgrade path is documented and reversible.
- **INTERCEPTOR as per-instance chain.** Rejected. The wiring granularity is per-pool (one pipeline per pool main agent), not per-instance. `InterceptorChain` composability (clone + append) was verified in F0, making per-pool clone the correct isolation unit.
- **A third AssemblyPipeline stage for subagents (run stages 4→5 through the pipeline).** Rejected in Errata-5 and reaffirmed here. Subagent per-invocation data (`parent_session`/`invocation_id`/materialize deps) does not fit the per-pool `AssemblyContext` factory contract. The core's keyword args absorb that data without forcing a pipeline reentry.

### Related

- `docs/design/scope-converge/SPEC.md` Errata-6 (authoritative detail), Errata-5 (predecessor convergence)
- `.omo/plans/unified-consumption-abdef.md` (execution plan, all waves)
- `docs/design/scope-converge/HANDOFF.md` (implementation overview + verification matrix)
- Commits: `8c363795` (WE) → `049a549b`+`0d9dadc3`+`b651f6dc` (WA) → `a24e9c4c` (WB, includes WF) → `9a4e454a` (WD)

## Slot Rationalization (added 2026-08-20, HEAD=d96497d9)

> **Authoritative detail**: `docs/design/scope-converge/SPEC.md` Errata-8. This section records the decision rationale for the slot-rationalization waves (W0-W6) that shrank the slot set from 13 to 10 and closed the audit gaps.

### Context

A three-reviewer audit (2026-08-19, architecture/correctness/coverage, arbitrated item by item) found the thirteen-slot system overstated its own surface:

1. **Three dead slots** — `MEMORY_PROVIDER`/`SKILL_SOURCE` had zero producers, zero consumers, zero YAML references (resolution was resolve-and-discard); `MEMORY_SYSTEM_MODIFIER` factories were registered but never named in production (the `inputs.memory_config` fallback always won).
2. **LLM provider bypass** — production pre-filled `inputs.llm_provider` on both paths (strategy-side resolution on main, hand-built `BotModelProvider` on sub), so the slot's own resolution branch was production-dead; `StrategyAssembly.provider` and a builders.py fallback duplicated the resolution.
3. **Prompt selector missing** — the `SYSTEM_PROMPT_PROVIDER` slot mechanism was live but its `_expand_system_prompt` exit was always `file_prompt`; YAML had no way to name a provider.
4. **MEMORY_SYSTEM gaps** — no `memory_system_config` roster face (I3), and memory hooks silently attached to an orphaned default memory system when a custom `ContextManager` replaced it (I2).

### Decision

**10-slot authoritative set** (TOOL/HOOK/MEMORY_SYSTEM/LLM_PROVIDER/SYSTEM_PROMPT_PROVIDER/INTERCEPTOR/COMMAND_HANDLER/EXECUTION_STRATEGY/INPUT_STAGE/DATA_NAMESPACE). The three dead slots are deleted, not completed: their theoretical value is covered — `MEMORY_SYSTEM` granularity plus the memory package's own seams (SystemPromptProvider pipeline, MemorySystem subclassing) plus `memory:` YAML overrides for parameter level.

**Convergence, not addition.** The LLM name→instance resolution happens exactly once per agent at the production entry, through one mechanism: `AgentFactory.create_agent(llm_provider=...)` override > factory default > LiteLLM. `StrategyAssembly.provider` and the builders.py fallback are deleted. The FW `default` factory serves only the FW single-provider schema; multi-provider model.yml parsing belongs to BIZ `bot_default`.

**Presets as functions.** `main_agent_memory`/`subagent_memory` live in `modex_agent/memory/presets.py` as plain functions — no factory indirection for a production path that always called the functions directly. The `MemoryProvider` ABC moves home to `memory/core/provider.py`, fixing the memory→plugins import inversion (guarded by `tests/architecture/test_memory_package_isolation.py`).

Memory ends as two YAML layers plus package seams: `memory:` (parameter-level MemoryOverrides) and `memory_system:` (whole-ContextManager replacement), with the ecosystem-loss list (experience injection, BIZ cleanup hooks, dream triggers) declared honestly for custom-ContextManager owners.

### Consequences

**Positive**
- Smaller, honest surface: every slot in the set has a live producer-and-consumer story or a declared on-demand semantics; the old "all slots production-reachable" over-promise is corrected.
- Dead configuration becomes loud: `memory.providers`/`memory.modifiers` warn; `skills:` hard-rejects at SubagentSpec (400 at the pool REST API).
- The LLM slot is real: subagents gain per-turn model switching (`bot_default` default; the W4 flip condition never triggered).
- `scripts/verify_slot_gates.py` (16 mechanical gates over the removal/convergence ledgers) is the permanent regression anchor; the red-anchor suite (T-P1..T-P4 + graph E2E) locks the new contracts.

**Negative**
- Migration cost for any (hypothetical) user of the removed slots — mitigated by warnings and hard rejections rather than silent acceptance.
- `plugins/defaults/` bundle shrinks (populated 6, empty 4); the empty four are by-design (bot territory per Errata-3, or on-demand).
- Known gaps recorded rather than fixed: InfraAssembleStage's `state_schema_compiler` product was deleted in the final review (it had zero consumers — BIZ wiring via `resources.py`'s `build_state_schema_compiler` is the single construction site); `_create_state` handles only `state_class`; custom sub execution-strategy names are cast (only `external` special-cased); INPUT_STAGE order stays code-defined with global insert.

### Alternatives considered

- **Complete all thirteen slots (build producers/consumers for the dead three).** Rejected: no demand existed for any of them — the disk-skill system, the memory-package seams, and MemoryOverrides YAML cover the real use cases. Completing would add surface to defend without a user.
- **Keep the dead slots as documented "register-available".** Rejected: a slot that silently does nothing is the exact silent-ineffectiveness trap the audit condemned; warnings and hard rejections are honest, an inert slot is not.

### Related

- `docs/design/scope-converge/SPEC.md` Errata-8 (authoritative detail), Errata-7 (MEMORY_SYSTEM), Errata-6 (unified consumption)
- `docs/design/scope-converge/plan-slot-rationalization.md` (approved plan) + `.omo/plans/slot-rationalization-steps.md` (execution ledger)
- `docs/design/scope-converge/HANDOFF.md` "Slot Rationalization Waves" (commit chain + verification)
- This addendum supersedes D-A5 (LLM layering) and the slot-matrix wording of the Unified Consumption Realization section above.
