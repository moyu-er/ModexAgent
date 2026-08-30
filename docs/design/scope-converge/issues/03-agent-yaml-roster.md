# 03 点名册：agent 的 YAML 形态与统一装配器

Labels: wayfinder:deliberate
Status: closed
blocked-by: 02
closed-by: deliberate session 2026-08-18 (Q1-Q12)

## Question

agent 定义（pool.yml + templates/*.yml）从"固定字段白名单"升级为"组件点名册"：用哪些登记过的组件名 + 各自配置块 + 增删（示意：`tools: [fs, +aci, -bash]`、`hooks: [todo]`）。

要决定：

- YAML 具体形状；ToolPreset 枚举降格为"预置点名组"的换算规则。
- 统一装配器 `assemble_agent(spec)` 放框架哪个模块；`AgentTemplate.materialize` 与它的关系（必须收敛为一条路径，不留双轨）。
- memory 字段放开到什么程度（subagent memory 现在是结构性烘死的）——受 01 答案影响。
- 外部 agent（Pi/OpenCode）在点名册里如何表达（`execution_strategy: external` 的组件化说法）。

## Comments

### Deliberate 决议（2026-08-18，三路调查后）

三路并行调查：(1) 参考项目 ctx 装配机制——结论：参考项目 **无**多阶段管道，装配是反应式依赖图；(2) input_pipeline 模式——结论：请求处理管道（process-in-place），非对象构造管道，机制可迁移但形态不可迁移；(3) workspace/pool plugin 化可行性——结论：资源/策略层已可插拔（`ResourceFactory[R]`/`ExecutionStrategy` ABC），骨架（lifecycle/routing/paths/session-mapping）框架级硬耦合。

#### Q1: ComponentRegistry scope — 全局单例 + roster 显式增删

ComponentRegistry 启动时从三源（bundled > user > entry_points）加载所有可用组件，全局一份。workspace/pool 不持有自己的 registry——通过 roster 从全局 registry 按名解析子集。参考项目的 ScopedLayers（agent→preset→global 就近遮蔽）由 roster 的显式增删语法替代：`tools: [fs, +my_plugin_tool, -bash]`。

**翻转条件**：若需 per-workspace 私有插件（workspace `.modex/plugins/` 目录加载该 workspace 独有插件），需 per-workspace registry 层。v1 可用"workspace 专属插件注册到全局 + roster 只在该 workspace 引用"近似。

#### Q2: assemble_agent 收敛范围 — 被 Q9 修正

初始推荐"只收 agent 构建路径，不收 pool 编排"。Q9 调查后修正为：形式化为 AssemblyPipeline（见 Q9），收敛所有 agent 构建路径（main + sub + external + 特殊 agent），pool 编排保持 BIZ `create_pool` 但**调用**管道。

#### Q3: Roster YAML 形态 — 两层 + toolPreset 合并

两层 YAML：`pool.yml` 携带 pool 级（哪些 agents + execution_strategy + peers），agent template 携带 per-agent 级（tools/hooks/memory 显式点名 + config blocks）。main agent 也有 template（当前没有——配置在 pool.yml + `memory_defaults.py` 烘死）。template 可被多个 pool 引用。

`ToolPreset`（当前 4 值枚举 full/read_write/read_only/...）降为"预置点名组"：新增 `toolPreset` 配置字段，与 `tools` 字段取并集使用。`tools: read_write` 展开为具体组件名宏，与 `tools: [custom]` 合并。

#### Q4: 旧 plugin 体系处置 — 全删

当前 plugin 体系（`PluginManager` + `PluginContext` + `PluginLoader` + `PluginIntegration`）是空壳且设计形状与新决议不兼容：5 个 register 方法 vs 12 槽位；collect-then-inject 的"inject"路径从未实现；`enabled: False` 初始化 + `inject_*` 零外部调用者。全删，从 02 决议的 `Plugin(ABC)` + `ComponentRegistry` 从零实现。

删除清单：FW `src/modex_agent/plugins/`（5 文件）；BIZ `bot/plugins/integration.py`；引用清理 `resources.py:226-233`、`_runtime_builders.py:28-46`、`core.py:366`、`builders.py:188`。零功能损失（空壳）。

#### Q5: 特殊 agent（experience/compactor/consolidator）— 固定不 template 化

特殊 agent 的 tool 是特殊配置的（只读不可写），不收敛到统一 `assemble_agent` template。它们的装配保持 inline。触发机制见 Q12。

#### Q6: External main + sub 收敛 — 统一到一个 ExternalExecutionStrategy

当前 external main 走 `ExternalExecutionStrategy.assemble`，external sub 走 `BotSubagentExternalBuilder.build`——两条路径。收敛到 `ExternalExecutionStrategy.assemble`，sub 的 `BotSubagentExternalBuilder` 逻辑吸收进 strategy。strategy 根据 `ctx.is_subagent` 分支构建 main 或 sub 的 `ExternalAgent`。借鉴参考项目的 execution strategy 按"如何执行"分类（不按 main/sub 分类）。

#### Q7: 特殊 agent assembly 时机 — 触发时调用

特殊 agent 是短生命周期的，在触发时（hook 触发、token 预算触发、后台任务触发）调用装配，执行完毕后销毁。不需单例、不需常驻。触发频率低，性能不是瓶颈。

#### Q8: 特殊 agent 作为 plugin 注入 — 被 Q12 修正

初始提出"是否可作为 plugin 注入"。Q12 修正为：特殊 agent 的**构造**保持 inline，**触发机制**作为 plugin。

#### Q9: 装配形式化 — 形式化为 AssemblyPipeline（spec→result 形态）

**核心决议**。当前装配是过程式的（函数调用链），扩展靠 5+ 个分散 ABC（`ResourceFactory`/`ExecutionStrategy`/`AgentFactory`/`SubagentExternalBuilder`/`AgentTemplate.materialize` 的 native/external 分支）。收敛到一个管道形态。

**管道形态**：
- **Payload**：`AssemblySpec`（frozen Pydantic）携带 pool spec + agent template + workspace context + 组件名引用
- **Stage**：`AssemblyStage.process(spec, ctx) -> AssemblySpec`（返回修改后的 spec，非原地修改）
- **Runner**：`AssemblyPipeline.run(spec, ctx) -> AssembledAgent`
- **Context**：`AssemblyContext` 携带 ComponentRegistry + workspace resources + persistence + provider
- **Stage 子集**：native main / native sub / external main / external sub / special agent 各有不同的 stage 子集

借鉴 input_pipeline 的机制（ABC + 单方法 + 构造器注入 + context 载体 + per-pipeline 子集 + enum-keyed metadata），但用 spec→result 形态（非 input_pipeline 的 process-in-place）。借鉴 `TurnContextConfigPipeline` 的 `applies()` + `configure()` 用于条件性 stage。

**参考项目不做管道装配**（参考项目是反应式依赖图，PENDING→LOADING→ACTIVE）。这是全新设计，非 port。理由：(1) 当前过程式装配的扩展点已分散在 5+ ABC，收敛到管道让所有扩展点可见、可排序、可配置；(2) 参考项目的反应式依赖图在 Python 里需要等价物——管道是有序依赖图的线性投影。

**翻转条件**：若用户认为过程式 + ABC 扩展点已足够灵活，不值得引入管道抽象。

#### Q10: Stage 划分 — 选 A 起步，AssemblySpec 按关注点设计

选项 A（按当前装配链 7 步：WorkspaceMaterialize→InfraAssemble→PoolAssemble→AgentAssemble→SubagentAssemble→SpecialAgentAssemble）vs 选项 B（按关注点：ResolveComponents→BuildMemory→BuildTools→BuildHooks→BuildGovernance→AssembleAgent→AssemblePipeline→RegisterPool）。

**选 A 起步**，但 AssemblySpec 按关注点设计（携带 `memory_config`/`tools_list`/`hooks_list`/`governance_config` 等关注点级数据，而非只携带 pool 级 `PoolSpec`）。A→B 迁移成本中等：Stage 接口不变、Pipeline runner 不变、AssemblySpec 不变（若初始设计够细），主要工作是拆 `PoolAssembleStage` 内部逻辑到细 stage。

#### Q11: workspace/pool 作为管道 stage — 骨架保持框架，资源/策略成为 stage

workspace/pool 的骨架（`WorkspaceRegistry`/`PoolRouter`/`WorkspacePaths`/`AgentPool`/`SessionTreeManager`/`InboxPoller`/`AgentMessageBus`/`session_workspace_map`）保持框架级硬耦合。资源构建（`ResourceFactory`）和策略选择（`ExecutionStrategy`）成为管道 stage（包装已有 ABC）。

`WorkspaceMaterializeStage` 调用 `ResourceFactory.materialize(ctx)`；`PoolAssembleStage` 调用 `ExecutionStrategy.assemble(ctx)`。骨架不进入管道。用户可选不同 `ResourceFactory`（单 workspace vs 多 live）和不同 `ExecutionStrategy`（react vs external vs graph），但不能替换骨架。

#### Q12: 特殊 agent 处置 — 构造 inline，触发机制 plugin 化

保持 Q5 决议（特殊 agent 不 template 化）。但它们的**是否启用 + 触发条件**通过 plugin 配置控制。`experience_review` plugin 提供 `ExperienceReviewHook` + 配置（`enabled`/`min_messages`/`cooldown_turns`）；`session_compactor` plugin 提供 compactor + 配置（`max_output_tokens`/`max_iterations`）。用户在 roster 里启用/禁用这些 plugin，不配置 agent template。

### 未落票的衍生问题

- Q9 的管道形态满足 ADR 三条件（难逆/反直觉/真实权衡），建议后续落 ADR。
- 05（图接缝）需继续 deliberate。
- 08（数据层原型）需 prototype 验证 01 决议。
