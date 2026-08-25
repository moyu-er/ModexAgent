# PLAN: 槽位理性化 — 删除低价值槽位, 做实纳入设计的槽位

> **Status**: Approved-for-planning (2026-08-19, 用户授权按评审建议执行); Momus 评审 GO-WITH-FIXES, 5 项修正已吸收（W4 消费者/时序、WebUI TS 面、preset 消费者补全、T-P2 表述、精确集合测试点名）
> **Input**: 三路评审（架构/正确性/覆盖度）+ 仲裁验证; `HANDOFF.md` 交接; SPEC Errata-5/6/7
> **Authority**: 本文档是实施计划; 设计变更以 **SPEC Errata-8** 落盘为准（随实施波次增量写入, W7 收口）
> **Range**: 基线 HEAD=`48be5c10`

---

## 1. 背景与问题

评审结论（2026-08-19，三评审员 + 逐条仲裁验证）:

1. **13 槽位中仅 5 个完整第三方可达**（TOOL/HOOK/MEMORY_SYSTEM/INTERCEPTOR/COMMAND_HANDLER）。HANDOFF "13 槽位全部生产可达" 是过度承诺。
2. **3 个槽位零生产者、零消费者、零 YAML 引用**: MEMORY_PROVIDER、SKILL_SOURCE（解析即丢弃, C1）; MEMORY_SYSTEM_MODIFIER 生产走 fallback, 工厂注册无人点名。
3. **理论价值被覆盖**: MEMORY_SYSTEM（整体替换）+ memory 包既有扩展点（SystemPromptProvider pipeline、MemorySystem 子类化）覆盖全部细粒度需求; MemoryOverrides YAML 覆盖参数级需求。
4. **附带架构债**: `MemoryProvider` ABC 定义在 plugins/abc.py:198 被 memory/recorder.py:26 反向 import（memory→plugins 依赖倒置, 也是 native_core lazy-import 循环的根源）; `defaults/triggers.py` 4 个工厂注册在 DATA_NAMESPACE 槽（类型滥用, 消费分支已在 Errata-5 删除）。

**决议（用户授权）**: 删除低价值槽位; 做实有价值且纳入设计的槽位。

## 2. 决议总表: 槽位 13 → 10

| 槽位 | 决议 | 理由 |
|---|---|---|
| TOOL | 保留（已做实） | E2E 机器证明 |
| HOOK | 保留（已做实） | E2E 机器证明 |
| MEMORY_PROVIDER | **移除** | 零生产/零消费/零引用; 被 MEMORY_SYSTEM 与 memory 包扩展点覆盖; ABC 迁回 memory 包修复依赖倒置 |
| SKILL_SOURCE | **移除** | 零生产/零消费/零引用; 磁盘 skill 体系覆盖全部真实用法; SkillManager 无追加 seam, 组合分支生产不可达 |
| MEMORY_SYSTEM_MODIFIER | **移除** | 生产走 `inputs.memory_config` fallback; preset 函数被 wiring 直调（工厂注册与函数直调双路径, arch-15 债）; 参数级需求由 MemoryOverrides YAML 覆盖 |
| MEMORY_SYSTEM | 保留 + **补完** | 唯一整体替换粒度, 已做实; 缺 config roster 面（I3）与孤儿 hook 报错（I2） |
| LLM_PROVIDER | 保留 + **收敛** | 生产预填旁路槽解析（双路径）; spec 侧已引 bot_default, 拆旁路即做实; 顺修 sub 无 per-turn 切换（leftover #1） |
| SYSTEM_PROMPT_PROVIDER | 保留 + **做实** | 槽机制活但无 YAML 选择器（_expand_system_prompt 出口恒为 file_prompt） |
| INTERCEPTOR | 保留（已做实） | per-pool 克隆链, E2E 证明 |
| COMMAND_HANDLER | 保留（已做实） | roster 缺省/点名双态, E2E 证明 |
| EXECUTION_STRATEGY | 保留（main 做实） | sub 自定义名被 cast 的限制如实文档化, 不扩面 |
| INPUT_STAGE | 保留（全局语义） | 自定义 stage 全局自动插入; 如实文档化, 不开 YAML 选择 |
| DATA_NAMESPACE | 保留 + **接线** | 槽语义纯化（triggers 滥用清理）; GraphOrchestrator 接受注入的 compiler |

**memory 终局定位（两层）**: `memory:` YAML 覆盖（MemoryOverrides: max_context_tokens / archive / core — 参数级） + `memory_system:`（整体替换 ContextManager — 接口级）。程序化扩展走 memory 包自有 seam（SystemPromptProvider pipeline / MemorySystem 子类化）, 不占槽位。

**附带清理**: `defaults/triggers.py` 删除（DATA_NAMESPACE 槽中的死注册, 消费分支已不存在）。

## 3. 设计变更详述

### 3.1 preset 搬家（MODIFIER 移除的前置）

- `main_agent_memory()` / `subagent_memory()` → 新模块 `modex_agent/memory/presets.py`（memory 包拥有 memory 预设; 只依赖 ioc.configs.memory, 不引入 memory→multi_agent 依赖环）。
- `main_agent_experience()` → 与 `ExperienceConfig` 同址（`multi_agent/pool_config/experience.py`）, 消除跨包倒挂。
- 消费者切换（完整清单, Momus 核验）: BIZ wiring（`_build_assembly_deps_for_pools`, stack.py）、`AgentTemplateRegistry(default_subagent_memory=...)`（factory.py:536）、native_core `_merge_memory` fallback、`bot/eval/agent_harness.py:35`（import `main_agent_memory`）、`examples/bot_project/tests_ext/regression/test_golden_replay.py:10-11`。
- native_core 的 lazy `subagent_memory` 导入（原自已删除的默认 memory 工厂模块）改为顶层 `from modex_agent.memory.presets import subagent_memory`（循环根源消除）。
- `plugins/defaults/memory.py` 整文件删除（工厂随 MODIFIER 槽死亡; `register_default_memory` 调用点从 defaults bundle 移除）。

### 3.2 MEMORY_PROVIDER 移除 + ABC 归位

- 删: `ComponentSlot.MEMORY_PROVIDER`、`PluginRegistrationContext.register_memory_provider`、`AssemblySpec.memory_providers/memory_provider_configs`、spec_builder `_extract_named_slot(memory, "providers")`、native_core `_resolve_multi(MEMORY_PROVIDER, ...)` 与 `NativeAssemblyResult.memory_providers`。
- `MemoryProvider` ABC → `modex_agent/memory/core/provider.py`（ABC 归 core/）; `memory/recorder.py` 改本地 import; `plugins/__init__` 停止导出。修复 memory→plugins 倒置。
- 新增架构守卫: **memory 包禁止 import modex_agent.plugins**（tests/architecture/）。
- recorder fan-out 机制（`MemoryAppendRecorder(providers=...)`）原样保留 — 与槽位无关的独立能力。

### 3.3 SKILL_SOURCE 移除

- 删: `ComponentSlot.SKILL_SOURCE`、`register_skill_source`、`AssemblySpec.skill_sources/skill_source_configs`、`SubagentSpec.skills` 字段、roster 链路上的 skills 投影、native_core `skill_manager is None` 组合分支（:317-319）与 `NativeAssemblyResult.skill_sources`。
- `store.py` editable 字段集合、config/AGENTS.md roster 表同步。
- **WebUI TS 面（Momus 补）**: `webui/src/types/pool.ts:128-131` 的 `SubagentNode.skills?: string[]` 字段及 SKILL_SOURCE/MEMORY_SYSTEM_MODIFIER 槽位注释一并移除; 核查 PoolEditor 往返路径不再携带 `skills` 键（`SubagentSpec extra="forbid"` 会拒绝）。
- SkillManager 组合 seam **不开**（无需求; 未来做远程技能源时再议, 届时成本更高但需求真实）。

### 3.4 MEMORY_SYSTEM_MODIFIER 移除

- 删: `ComponentSlot.MEMORY_SYSTEM_MODIFIER`、`register_memory_system_modifier`、`AssemblySpec.memory_system_modifiers/memory_system_modifier_configs`、`_extract_named_slot(memory, "modifiers")`、`_merge_memory` 的 resolved 分支与多 modifier ValueError。
- 保留: MemoryOverrides 全部（max_context_tokens/archive_enabled/core_enabled）。
- `_build_memory_overrides` 对 `memory:` dict 的未知子键（含已死的 `providers`/`modifiers`）打 warning — 死配置从静默变有声。
- 顺手修 M1: override 应用改为 `model_copy(update={"enabled": ...})` 保留 preset 子配置内部值, 而非整建。

### 3.5 MEMORY_SYSTEM 补完（Errata-7 收尾）

- **config 面（I3）**: `MainAgentSpec`/`SubagentSpec` 新增 `memory_system_config: dict[str, Any]`（默认 `{}`）; spec_builder 投影（替换 :135 硬编码）; store.py editable 集合。
- **孤儿 hook 报错（I2）**: `assemble_native_agent` 中 `spec.memory_system is not None` 时, `_dispatch_hooks` 遇 memory-runner hook → `ValueError`（显式报错优于静默挂死, SPEC §6.8 哲学）。报错文案指向"将 hook 注册进自定义 ContextManager"。
- **生态损失清单**写入 Errata-8: 替换 CM 即放弃 experience 注入（ExperienceProvider 骑默认 CM 的 prompt pipeline）、BIZ cleanup hooks（挂 `pool_data.context_manager.memory_system`）、dream 触发 — 自定义 CM 需自担等价物。

### 3.6 SYSTEM_PROMPT_PROVIDER 选择器（做实）

- `MainAgentSpec`/`SubagentSpec` 新增 `system_prompt_provider: str | None` + `system_prompt_provider_config: dict`。
- `_expand_system_prompt` 优先级: **显式 provider 名 > `system_prompt` 糖（file_prompt+path）> `prompt_name`/agent 名约定**。
- 第三方注册自定义 prompt provider 后可被 YAML 点名; `file_prompt` 仍是缺省。

### 3.7 LLM_PROVIDER 生产收敛（拆双路径, 做实槽位）

现状（Momus 核验修正）: main 与 sub 路径状态不同——
- **main**: `ReactExecutionStrategy.assemble_main` 内部**已经**经槽解析 provider（react_strategy.py:111-122）, 产出进 `StrategyAssembly.provider`; Stage 4 又把它预填进 `inputs.llm_provider`（factory.py:431）, 使 native_core 自己的 `_resolve_single` 分支生产死。即: 机制上已走槽, 但存在 **strategy 侧解析 + native_core 侧解析的双路径**, 且 strategy 构建链上另有 builders.py:287 `BotModelProvider` 手工构造并存。
- **sub**: `deps.llm_provider` 由 factory.py:614 一带手工构造预填, template.py:360 转填 —— sub 的槽解析从未发生（T-P2 红锚点的实体）。

`StrategyAssembly.provider` 实际消费者 ≥3 处: factory.py:394（`_build_agent_factory(default_llm_provider=...)` — **agent factory 在 Stage 4 槽解析之前构造**, 时序约束）、factory.py:510-511（assembly 缺失/external 旁路路径）、factory.py:614（sub deps）。

**收敛设计（解决时序）**: 槽解析点**前移到生产入口**（agent factory 构造之前）——
- main: `build_native_inputs` 内改为 `provider = 槽解析(spec.llm_provider)`（一次）, 同时喂 (i) `_build_agent_factory(default_llm_provider=provider)`、(ii) `inputs.llm_provider=provider`。
- sub: `materialize` 的 deps 组装处同样槽解析一次, 喂 `deps.llm_provider` 与 agent factory。
- `ReactExecutionStrategy` 停止构建/解析 provider; `StrategyAssembly.provider` 字段移除。
- native_core 的 `_resolve_single(LLM_PROVIDER)` 分支降格为**通用 fallback**（`inputs.llm_provider=None` 的直调方), 文档化 —— 不再是生产双路径的一支。
- 槽成为唯一生产机制（bot_default 工厂经 `pool_assembly_ctx` 构造 `BotModelProvider`）; sub 默认 bot_default（spec 侧 factory.py:634 已如此）→ **顺手关闭 leftover #1（sub per-turn 模型切换）**。
- **I5 修复**: `plugins/defaults/llm.py` 删除 `_llm_config_from_multi_provider`（FW 解析 BIZ 多 provider 格式的桥, :80-122）— BIZ 格式归 BIZ `bot_default` 工厂（已用真 `BotModelConfig` 解析）。FW `default` 只管 FW 单 provider schema。畸形文件静默 `model=""` 的弱错误处理随桥消失。
- 风险门: bot 套件（1900）全绿。翻转条件: sub 拿 bot_default 出现语义性红（如测试断言 sub 用裸底层 provider）→ sub spec 默认回 `default`（FW 底层）, roster 可显式 `bot_default`; main 收敛不变。
- `bot_default` 工厂对 sub ctx 的 `model_choice_registry` 可用性核查（bot_strategies.py:100-104; 预期 per-pool 共享, 在位）。

### 3.8 DATA_NAMESPACE 接线（GraphOrchestrator）

- GraphOrchestrator 增可选 `state_schema_compiler` 注入（当前 :190 自行两参构造, InfraAssembleStage 组装的 compiler 被丢弃）。
- BIZ graph wiring 将组装产物传入。
- 槽语义纯化: DATA_NAMESPACE 只做类型登记（state_schema 类型解析 + TypedBundle）。

## 4. 波次规划

每波独立提交（可单独 revert）; 有新行为的波先写红测试。

### W0 盘点 + 红测试锚（~0.5d）
- grep 全部 `register_*` 调用点 → 死面清单终验（预期仅 triggers.py; 若发现更多, 纳入 W1）。
- 红测试（进入 `tests/integration/`, 锚定到生产路径）:
  - **T-P1**: 自定义 prompt provider 经 roster 点名到达 agent system_prompt（红: 无选择器）
  - **T-P2**: registry probe LLM provider 经 roster 到达 sub instance（红: sub 手工构造预填, 槽解析从未发生; main 半边今日经 strategy 侧解析已可达, 断言目标写为"收敛后单一路径"而非新能力）
  - **T-P3**: MEMORY_SYSTEM probe 工厂带必填 config 字段, roster `memory_system_config` 传入（红: 无字段）
  - **T-P4**: `spec.memory_system` 已设 + roster 点名 memory hook → 期待 ValueError（红: 当前静默挂死）
- 架构守卫红: memory 包禁 import plugins。

### W1 槽位移除 + preset 搬家（~1d, 4 commits）
- c1: preset 搬家（§3.1; native_core lazy import 顶层化）
- c2: 移除 MEMORY_SYSTEM_MODIFIER（§3.4, 含 M1）
- c3: 移除 MEMORY_PROVIDER + ABC 归位（§3.2; 守卫转绿）
- c4: 移除 SKILL_SOURCE + triggers.py 清理（§3.3 + §2 附带）
- 验收: `memory.providers`/`modifiers`/`skills` 出现在 YAML → warning/拒绝（skills=Schema 拒绝, memory 子键=warning）; register_* 映射测试 13→10; 精确集合测试同步: `test_abc.py` 成员精确计数测试→10、`test_defaults_bundle.py`（populated 8→6, empty 5→4, 精确名集断言）、`test_loader.py` register 方法计数测试→10; FW+bot 全绿。

### W2 MEMORY_SYSTEM 补完（~0.5d）
- §3.5; T-P3/T-P4 转绿; `test_memory_system_slot.py` 扩展 config 场景; Errata-8 相应小节。

### W3 SYSTEM_PROMPT_PROVIDER 选择器（~0.5d）
- §3.6; T-P1 转绿; config/AGENTS.md roster 表补行。

### W4 LLM_PROVIDER 收敛（~2d, 风险最高 — 时序重构计入）
- §3.7（槽解析前移到 agent factory 构造之前; `StrategyAssembly.provider` 三处消费者迁移）; T-P2 转绿（sub 半边红→绿; main 半边断言单一路径）; bot_default-on-sub 核查; I5 桥删除 + 相应 E2E 断言改真值（修恒真断言 T5）; bot 全套件风险门 + 翻转条件预案。

### W5 正确性修复包（~1d, 与 W2-W4 并行可行）
- F1: `MemoryOverrides.max_messages` 死配置删除（字段+parser+README/AGENTS 引用; SessionConfig 无对应实现面）。
- F2: hooks `+/-` 收敛进 SpecBuilder（`_merge_hooks`; 删 template.py:182-190 与 factory.py:353-361 双份; ADR D-A8 措辞修正为"SpecBuilder 应用增量"）。`-name` 不可移除代码写死默认集的限制维持文档化。
- F3: sub `PoolRuntimeDeps` 补 `session_tree_manager`（template.py:191-201）+ todo_continuation-on-sub 测试。
- F4: 同源重名 ValueError（PluginRegistrationContext 携 source; 跨源维持 first-seen-wins warning）; registry docstring 修正; loader :399 except 加日志; 删 :371 死 hasattr 兼容。
- Minors 打包: M7 assert→ValueError（hooks.py ×4）; M9 删 `arbitrary_types_allowed`; M11 todo 工厂按名配对; M3/M13 `_propagated_ctx` 公开化并用于 factory.py:713（interceptor/command 工厂拿到 pool_runtime 富集 ctx）; M4 `bundled_factories` 改 tuple; M14 roster hook 与 extra_hooks 去重。
- 覆盖补齐: `_merge_memory`（W1 后形态: fallback + 3 override + M1 toggle 语义）全覆盖; 存活负路径（unknown 组件名穿透装配层、空 INPUT_STAGE registry raise、memory hook 无 system、Stage-3 缺失 RuntimeError）; E2E modexctl skip 守卫（无 modexctl → skip 带 reason, 不再环境性红）。

### W6 DATA_NAMESPACE 接线（~1d, 含 spike）
- spike: GraphOrchestrator 构造点清单 + 注入参数设计; §3.8 实施。
- E2E: 自定义 DATA_NAMESPACE plugin 注册类型 → 图 spec `state_schema` 经 registry 解析编译（当前经 InfraAssembleStage 产物被丢弃, 红）。

### W7 文档收口（~0.5d）
- SPEC **Errata-8** 完整落盘: (a) 槽位集 13→10 权威清单 + 各移除项理由与迁移指引; (b) memory 两层定位; (c) MEMORY_SYSTEM 补完三件; (d) prompt 选择器语法; (e) LLM 收敛（取代 D-A5 分层表述）; (f) triggers 清理 + DATA_NAMESPACE 语义纯化; (g) INPUT_STAGE 全局语义与 EXECUTION_STRATEGY sub 限制如实声明。
- ADR-0041 追加 Slot Rationalization addendum; 顺手修 "frozen dataclass" 陈旧表述（M10）。
- HANDOFF 增补本轮波次; plugins/AGENTS.md、abc.py docstring、loader 计数、config/AGENTS.md、bot AGENTS.md、bot README「Plugin System」段重写（现仍是旧 `plugins: enabled` 体系描述）、`webui/src/types/pool.ts` 槽位注释与死字段清理。

## 5. 明确不做（本轮 out of scope, 记录翻转条件）

| 项 | 理由 | 翻转条件 |
|---|---|---|
| RosterLoader 双投影收敛（I6） | 内部收敛债, 非插件面价值; 触及 pool 创建投影, bot 测试风险大 | pool 创建/PoolStore 投影重构时一并做 |
| Stage-4 cleanup 生产注册（I4 全量） | supply-mode 下 orchestrator 拥有 broker/pool/workspace; 仅文档化"清理归 orchestrator" | 出现装配中途泄漏的实际事故 |
| hooks 默认集 roster 化（D-A8 升级） | 维持既有决议（1900 测试门） | bot hooks 全量插件化需求真实出现 |
| INPUT_STAGE YAML 选择 | 全局自动插入语义如实文档化即可 | 出现"按 pool 选 input stage"需求 |
| 自定义 sub execution strategy 路由 | 投机面; 限制文档化 | 出现真实第二 sub strategy |
| M5/M6（Any 注释化 / ComponentFactory 泛型化） | 独立风格债, 不阻塞 | 下次触碰相关文件时顺手 |
| SkillManager 组合 seam | 同 §3.3 | 远程/DB 技能源需求 |

### 5.1 远期增量候选（**初步设计, 设计中 — 非最终设计**; 参考项目对照评审 2026-08-19 产出）

> ⚠️ **状态声明**: 本节是方向性草案, 不是最终设计。仅记录"有什么用、大概往哪走、还没定什么", 避免设计丢失。**禁止据此实施**——任一候选提升为正式波次前, 须单独立项出完整设计（红测试锚 + 验收门）并过评审。

三个候选均为真增量: 独立提交可 revert、有红测试可锚、消费 W 波次产出而不改变其形状。依赖单向无环: W1→W5 F2→INC-1, W4→INC-2。明确不采纳（记录防反刍）: 反应式重装配/HMR（SPEC 已决议放弃, Python 无对应运行时基建）、everything-is-row 连 loop 可配置（违背骨架固定原则, 类型安全换灵活性的取舍方向相反）、workspace 插件化（参考项目 workspace 本身只是数据注册表, 我们的 workspace 是产品核心复杂度, 无第二实现需求）。

**INC-1 sub 组合继承**
- **作用**: 让 main 在 pool.yml 的 tool_supplements/hooks 增量流向 subagent 模板, 消除"main 加了能力、逐模板手工同步"的配置漂移（参考项目父组合绑定语义的 roster 表达）。
- **设计方向草案**: 继承的是增量（supplements/hooks `+/-`）而非整体替换, 复用 W5 F2 收敛后的 SpecBuilder 单一增量实现; 模板显式声明视为覆盖。
- **待决策**: ① 缺省继承还是 opt-in（sub 最小权限自包含的安全反案 vs 同步便利, 两个方向参考项目都有先例）; ② 继承粒度（仅 supplements/hooks, 还是含 memory overrides）; ③ external strategy 的 sub 是否参与。
- **前置**: W5 F2、W1。**翻转条件**: 出现"main 增量需流向 sub"的真实配置需求或漂移事故。

**INC-2 AgentType 判别式收敛**
- **作用**: `AgentType` 4 值可从 `(ExecutionStrategy, AgentCommKind)` 推导——消除双编码, 防未来第三策略/第三拓扑维度时枚举组合爆炸。
- **设计方向草案**: AgentType 改为推导函数/派生属性; stage 矩阵、`applies_to` 过滤、is_main 门、template materialize 分派点改读推导值。
- **待决策**: ① 保留兼容别名还是彻底删除（收敛规则 2 倾向删, 但 API 面广）; ② 推导函数归属模块; ③ 是否顺带收敛 `native_sub` 与 `comm_kind.SUBAGENT` 的语义重复。
- **前置**: W4 稳定后（同期动 strategy 分派互相干扰）。**翻转条件**: 枚举组合爆炸出现。

**INC-3 未 join 组合装配警告**
- **作用**: 装配产物引用零个非默认组件时打日志——裸 agent 合法, 但可诊断"以为配了其实没生效"（参考项目同类装配警告的等价物; 我们 C1 类静默失效问题的低成本守卫）。
- **待决策**: ① info 还是 debug 级别; ② "零自定义"的判定口径。
- **前置**: 无, 随任意波次顺手实现。

**INC-4 图 state_schema_compiler 双构建点收敛**
- **作用**: `InfraAssembleStage` 填充的 `builder.infra.state_schema_compiler` 零下游消费（`builder.infra` 仅被 `PoolAssembleStage` 消费 `pool_assembly_ctx`/`pool` 两个字段）, 实际生效的编译器由 `bot/workspace/wiring/resources.py` 直建并注入 `GraphOrchestrator`（w6.1）——同一 `build_state_schema_compiler` 两个构建点、一死一活, 属收敛规则 1 意义上的分叉。
- **设计方向草案**: (a) 删除 stage 死填充及其单测, 承认 `resources.py` 为唯一路径（倾向——Errata-5 后 SPEC §8.5"图装配进 pipeline"的前提已不成立）; (b) `resources.py` 改为消费 `assembled.infra`（符合 §8.5 原意, 但需重启装配链验证）。
- **待决策**: (a)/(b) 选型。
- **前置**: 无（w6.1 已引入现状）。
- **翻转条件**: scope-converge 收尾评审。

## 6. 最终验证矩阵（W7 后）

| 套件 | 门槛 |
|---|---|
| FW unit + architecture（含新守卫 memory↛plugins） | 全绿, 无回归 |
| Bot suite（examples/bot_project/tests） | 全绿（W4 风险门） |
| Integration: 既有 4 文件 + T-P1..T-P4 + W6 图 E2E | 全绿（modexctl 缺失环境 skip 而非 fail） |
| ruff / mypy / LSP（changed files） | pass |
| 死面零残留 | grep: 移除槽名/register_*/spec 字段零引用; roster 死键有声拒绝 |
| 声明对齐 | HANDOFF/SPEC/AGENTS.md/README 槽位数一致（10） |

## 7. 工作量

W0 0.5d + W1 1d + W2 0.5d + W3 0.5d + W4 2d + W5 1d + W6 1d + W7 0.5d ≈ **7 个专注日**。W5 可与 W2-W4 并行穿插。

## 8. Momus 评审记录（2026-08-19, GO-WITH-FIXES → 修正已吸收）

| # | 缺口 | 处置 |
|---|---|---|
| 1 | W4 `StrategyAssembly.provider` 消费者预期"仅一处"与代码不符（≥3 处）+ agent factory 先于 Stage 4 槽解析构造的时序问题 | §3.7 重写: 消费者清单更正 + 槽解析前移设计; W4 估算 1.5d→2d |
| 2 | WebUI TS 面（pool.ts:128-131 `SubagentNode.skills`）未入移除账目 | §3.3 补 WebUI TS 面 + PoolEditor 往返核查; W7 声明对齐含 pool.ts |
| 3 | §3.1 preset 消费者漏 `bot/eval/agent_harness.py:35` 与 `tests_ext/regression/test_golden_replay.py:10-11` | §3.1 清单补全 |
| 4 | T-P2 "槽解析永不触发"对 main 不精确（strategy 侧已解析; 死的是 native_core 侧） | W0/T-P2 表述修正; §3.7 现状区分 main/sub |
| 5 | W1 精确集合测试账目隐式（test_abc 13-member、test_defaults_bundle 8/5、test_loader 12-register 命名） | W1 验收点名三项 |

Momus 判定原文维度结论: Clarity ✅ / Verifiability ✅（T-P1/P3/P4 锚点核验为真红）/ Completeness 有缺口（上述 1-3）/ Risk ✅（W4 翻转条件三要素齐全; preset 搬家无环）/ Traceability ✅（20+ 锚点全部命中）。
