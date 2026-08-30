# 02 组件登记处：PluginContext 长什么样，哪些东西能报名

Labels: wayfinder:deliberate
Status: closed (resolved 2026-08-18, amended 2026-08-18 per SPEC §12)
blocked-by: 07

## Question

照参考项目的 ctx 抄一份我们的登记处。参考项目里每种可扩展的东西都是 `ctx.xxx.register(...)`，插件还自带配置表（schema），配置从 YAML 行进来。

要决定：

- PluginContext 暴露哪些注册方法。现有 5 种（tool / hook / memory_provider / skill_source / memory_modifier）；候选新增：provider（LLM）、prompt_section、interceptor、command、adapter，以及"每个组件自带 Pydantic 配置模型"的通用契约。
- 登记进来的东西放哪：直接填现有 ToolManager / HookRunner，还是先入独立的 ComponentRegistry 再由装配器分发。
- 重名冲突语义、启用/禁用语义（现有 `_enabled` 名单如何演化）。
- 内置组件（13 个 hook、3 个 interceptor、2 个 provider）是否全部降格为"默认插件"走同一注册路径（消灭内置/插件双轨）。

## 设计输入（2026-08-18 会话沉淀）

**参考项目三层结构**（详见 map.md Notes 与本地 gitignored 取证笔记）：

1. **真内核**（写死）：装载器、ctx、服务注册表、事件分发语义；两条结构不变量（会话日志 append-only、模型可见即已记录）。
2. **能力槽位**（56 个 ctx 键）：框架代码主动穿过的接缝；槽位清单与各注册面**签名**固定。
3. **类型开放点**：新数据类型（SessionEventMap 合并、StorageForms 域、投影、UI 渲染器）骑在固定机制上——词汇开放。

**读写原则**：框架定信封（接缝形状），插件定信件（流过接缝的数据形状——工具 parameters schema、插件 Config、事件负载）；读侧通道封闭、框架数据负载固定、可读词汇开放（= 框架固定数据 ∪ 插件生态声明的类型）。

**槽位闭集决策**：v1 不开放自定义服务键（参考项目需要它是因为有运行时 mount 与 scoped 组合；我们重启生效 + Python 插件间直接 import，价值低不背复杂度）。槽位清单用 StrEnum 闭集，与 frozen spec 类型纪律同构。

**56 槽位清单的用法**：作为"完整 agent 框架需要哪些接缝"的查漏对照表——评估候选注册面时逐域对照（已识别缺口：`systemPrompt.variable()` 式提示词变量注册面、`tools.guard()` 式否决中间件——我们分别由 prompt pipeline provider 与 interceptor scope 对应，命名与覆盖面在此票内对齐）。

## Comments

### Resolution (2026-08-18, Sisyphus + explore audit ses_febd833e0ffeQ26jRCYCA0XR64)

**审计基线**：47 个活跃接缝 + 7 个休眠/废弃（全部 file:line 级证据，见本地 gitignored 探索会话记录）。三条意外发现修正了票面前提：
- `CommandHandler` 已是 ABC 非 Protocol（"违反零 Protocol 规则"的前提过时）。
- `adapters/platform.py` 的 `PlatformAdapter`/`AdapterRegistry` **零消费者、零实现**——ADAPTER 槽位的同名框架注册表是死代码；活接缝在 `pipeline/adapters.py` + 业务层 `ADAPTERS`。
- interceptor 4 个 scope（AGENT_RUN/LLM_CALL/PIPELINE_STEP/POOL_TASK）定义未接线——今天注册会静默无效。

**最终 v1 槽位（12 个，按框架现成词汇统一命名）**：

| 槽位 (StrEnum) | 注册方法 | 现状/动作 |
|---|---|---|
| `TOOL` | `register_tool` | ✅ 已有 |
| `HOOK` | `register_hook` | ✅ 已有（trace span/cassette flush 都骑此槽） |
| `MEMORY_PROVIDER` | `register_memory_provider` | ✅ 已有 |
| `SKILL_SOURCE` | `register_skill_source` | ✅ 已有 |
| `MEMORY_SYSTEM_MODIFIER` | `register_memory_system_modifier` | ✅ 已有（原 MEMORY_MODIFIER 改名对齐 API） |
| `LLM_PROVIDER` | `register_provider` | 新增（工厂 2 分支→注册表查找，Q3 首批降格） |
| `SYSTEM_PROMPT_PROVIDER` | `register_prompt_provider` | 新增（`SystemPromptProvider` ABC 现成；`memory/system.py:224-324` 的硬编码 11 步列表要开插入缝，v1 用"append-before-role-contracts"排序契约） |
| `INTERCEPTOR` | `register_interceptor` | 新增（注册时**校验 scope**，拒绝未接线值；TOOL_CALL/TURN/ITERATION/LLM_STREAM 可用） |
| `COMMAND_HANDLER` | `register_command` | 新增（`CommandHandler` ABC；IM 控制命令 /cd /stop /pool 仍由业务 input-pipeline 阶段先拦截，文档注明） |
| `EXECUTION_STRATEGY` | `register_execution_strategy` | 新增（指向**已存在的** `ExecutionStrategyRegistry`——审计发现的最大遗漏；03 的 `execution_strategy: external` 必须按名解析它） |
| `INPUT_STAGE` | `register_input_stage` | 新增（`InputStage` ABC + `UserInputPipeline` 框架已有；moderation/redaction/PII 过滤是标准插件用例；需 before/after 命名阶段的插入位置语义） |
| `DATA_NAMESPACE` | `register_namespace` | 新增（01 号票的类型命名空间层 + `resolve_bundle` 一等访问面——两票在此焊接） |

**延后（不在 v1）**：
- `ADAPTER` + `EMITTER` → 04（活接缝在业务层 + 按 (session_id,pool) 工厂构造的 per-instance 生命周期，需要装配回迁才有框架落点；其框架同名 `PlatformAdapter` 是死代码，绝不让新注册面指向它）。
- `EXTERNAL_PROVIDER` → 04（接缝真实——Pi/OpenCode 双实现满足"两个适配器才是真接缝"规则——但选择表业务硬编码 + `ProviderKind` 枚举扩展，随 04 外部装配回迁一起落 `kind → (backend_factory, parser)` 注册）。
- `GOVERNANCE` / `APPROVAL_CLASSIFIER` → 03（per-agent 选择是点名册的事；`CompositeGovernance(strategies)` 结构开放但链从 `MemoryConfig` 派生，插件槽会分叉配置源）。
- `SANDBOX` → 无槽位（17 文件零外部引用；先以 `TOOL_CALL` interceptor 形态接线再议，仍不需要自己的槽）。
- Memory store backend → 无槽位（`PersistenceBackend` 闭集 2 值枚举 + 需要 `ConnectionManager`；01 已给插件 KV，需求满足）。

**Q2 决议（登记进哪）：name-keyed ComponentRegistry + 保持 collect-then-inject。**
按 repo 自己的先例统一形状——`ExecutionStrategyRegistry`（重名 ValueError、restart 作用域）、`ADAPTERS`（name+enabled+factory）、`LintRegistry`（idempotent-by-name）：`ComponentRegistry` = 每槽位 `dict[name → (component_or_factory, plugin_name, config_model_cls)]`，闭集 StrEnum 与 frozen spec 纪律同构；`PluginContext.register_*` 写入注册表（context 仍是 collecting facade）；`PluginLoader.inject_*` 桥"注册表→现有管理器"不动；03 的 `assemble_agent(spec)` 按名解析。不直接填管理器——hook/interceptor 今天无稳定 YAML 名，点名册无从查起。

**Q3 决议（内置降格）：v1 部分降格，全量降格搭 04 的车。**
- 现在降（无依赖零风险）：LLM 工厂两分支（openai/litellm 注册为默认插件——直接消灭最刺眼的硬编码）、LoopDetection、CurrentTimeInjection、Knowledge、RunLogging。
- v1 保留接线（工厂形状：TodoContinuation 要 per-pool 树、ControlDrain 要控制通道、ExperienceReview 要 memory+全局 provider、TurnOutcomeNotify 要 notification_service、SubagentAutoSend 要 tree+names）——等 04 的装配器支持 factory 形注册时一次收敛，**不在 v1 给未稳定的面加第二种组件形状**。
- **永不降**：`ToolTimeoutInterceptor`（`interceptor/builtin/tool_timeout.py`，由 `ToolExecutor` 内层组合，是工具超时死线——正确性不变量不是功能）。
- 排序契约写死：`TodoContinuationHook priority=-1000` 必须最先于 AfterTurnHook；trace 钩子 Root→Tool→Handoff 顺序——注册处尊重 priority。
- 前提修正：`register_tree_aware_hooks` 只注册 2 个（TodoContinuation+DeliverRetry）；"13 个内置 hook"散在 7 个装配点（`multi_agent/factory.py:380-458`、`hook/wiring.py:31`、`bot/.../resources.py:230-235`、`pipeline_wiring.py:110-151`、`template.py:244,286`、`pool_wiring.py:87-101`、memory cleanup hooks）。

### Amendment (2026-08-18, per SPEC §12 冲突决议)

**Q2 "保持 collect-then-inject" 被推翻。** 03 票 Q4 决议全删旧 plugin 体系（`PluginManager`/`PluginContext`/`PluginLoader`/`PluginIntegration`），包括 `PluginLoader.inject_*` 桥。旧体系的 inject 路径从未实现（零调用者），collect-then-inject 是空壳。

新设计（SPEC §4.4）：`ComponentRegistry` 是唯一信任边界。`AssemblyPipeline` 的 stage 从 registry 按名解析组件，直接注入到正在构建的 manager/agent——不经过 collect-then-inject 的中间列表。

**Q3 "工厂形状 builtins 等 04" 与 Q9 AssemblyPipeline 一致，无冲突。** Q9 的管道支持 factory 形 stage：stage 在 `process(spec, ctx)` 时从 ComponentRegistry 解析工厂，调用 `factory.create(config) -> Component`。

完整设计见 `docs/design/scope-converge/SPEC.md`。

**配套（来自 07 号票的 7 条落地指针）**：`Plugin(ABC)` 类型化入口 + `config_model: ClassVar[type[PluginConfig]]`（frozen+forbid）+ `api_version: ClassVar[int]` 契约常量；同源重名炸响、跨源 first-seen-wins（bundled>user>PyPI）；保留 `_enabled` 白名单；隔离保持 `except Exception → log, skip`（永不捕 KeyboardInterrupt/SystemExit）。
