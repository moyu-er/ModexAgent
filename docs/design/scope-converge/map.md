# Wayfinder 地图：移植参考项目的开放层（组件化 agent + 开放数据流）

Labels: wayfinder:map
Status: open

## Destination

把参考项目已验证的三件事移植进 ModexAgent：

1. agent 完全用"组件名 + 配置"在 YAML 里拼装——工具/钩子/模型/提示词/拦截器/命令全部可注册，重启生效；
2. 插件能带自己的数据类型——会话内变量的开放存取（Python 版的"数据户口"）；
3. 图能力原样保留并在新数据层上受益——同一份 agent 清单能进 DAG 节点，图变量照常持久、可恢复。

完成判据：不改框架代码、只写 YAML + 装插件，就能定义出行为不同的新 agent；插件能安全存取自己的会话数据并在系统提示/WebUI 生效；现有图工作流不回退。

## Notes

- 参考项目：外部开源 harness（本地源码克隆，gitignored）。前期两份源码级探索结论已在本会话沉淀；票内需要的细节由 06 号取证票补全。
- 讨论票一律用 **deliberate** 技能（用户指定，不用 grilling）。
- **价值定位（2026-08-18 用户确认）**：本次改造约 2/3 是收敛（还债：装配代码回框架、反射弧回框架、解开自我焊死、给半成品器官找消费者），约 1/3 是真提升（第三方扩展面、纯 YAML 组装、类型化数据词汇、spec→图节点）。收敛部分独立成立——即使参考项目不存在也该做。**图焊接（05）是从"收敛"升格为"提升"的唯一杠杆**：时间紧宁可砍插件面广度，不可砍 05。只做插件化不做 05 = 买到"重启生效的参考项目子集"= 仅防御；插件化 + 05 = 开放性围绕我们独有资产（图 × 常驻 agent × 持久进化）成为放大器。
- **参考项目开放性的三层结构（02 号票设计输入）**：① 真内核写死（装载器/ctx/注册表/事件分发语义 + append-only、"模型可见即已记录"两条不变量）；② 56 个能力槽位（框架代码主动穿过的接缝，槽位清单与注册面签名固定）；③ 类型开放点（新服务键/新数据类型/新事件——骑在固定机制上的开放词汇）。读写两侧同一条原则：**框架定信封（接缝形状），插件定信件（流过接缝的数据形状）**；读侧 = 通道封闭、框架数据负载固定、词汇开放（可读集合 = 框架固定数据 ∪ 插件生态声明的类型）。
- **v1 明确留白（已论证）**：槽位闭集（不开放自定义服务键——无运行时组合时价值低）；无插件间事件总线（Python 直接 import + 命名空间变量仓已覆盖）；重启生效（不背运行时 mount/HMR 复杂度）。
- 迁移纪律（用户已定）：模块内部一次到位、不留双路径（repo 收敛规则）；跨模块按依赖序分步，每个中间态完整可用。
- 类型纪律不放松：组件 = ABC 实例，配置 = frozen Pydantic，登记处 = 唯一信任边界。
- 本图前提（用户已定）：不做运行时热插拔 / HMR / 沙箱自挂载，重启生效。
- bot_project 终局（用户已定）：降格为"默认组件包 + 默认点名册"，装配反射弧收回框架。

## Decisions so far

- [01 会话数据的家](issues/01-session-data-home.md) — 插件持久数据住进现有 KVStore + 新增类型命名空间层（含一等 resolve_bundle 访问面）；不动会话记忆/transcript；turn 级留 runtime.state；v1 只做变量面
- [06 取证：参考项目三件套具体形状](issues/06-reference-evidence.md) — 移植对照表落盘于本地 gitignored 取证笔记；参考项目插件持久态走存储域（与 01 决议同构）；三种注册形态（named-provider / definition registry / capability-flagged ABC）
- [07 Python 生态先例调研](issues/07-python-precedents.md) — 结论：薄手写登记处 + 仅复用 stdlib entry_points 发现；不引 pluggy/stevedore/npe2/MCP SDK；MCP v2 Extension 为形状模板（基类+frozen 绑定+params_type+单一公共注册面）；报告含 7 条 02 号票落地指针
- [02 组件登记处](issues/02-component-registry.md) — v1 12 槽位（TOOL/HOOK/MEMORY_PROVIDER/SKILL_SOURCE/MEMORY_SYSTEM_MODIFIER/LLM_PROVIDER/SYSTEM_PROMPT_PROVIDER/INTERCEPTOR/COMMAND_HANDLER/EXECUTION_STRATEGY/INPUT_STAGE/DATA_NAMESPACE）；延后：ADAPTER+EMITTER/EXTERNAL_PROVIDER→04，GOVERNANCE/APPROVAL_CLASSIFIER→03；Q2 name-keyed ComponentRegistry+保持 collect-then-inject；Q3 v1 部分降格（LLM 工厂+4 hook），工厂形状 builtins 等 04，ToolTimeoutInterceptor 永不降
- [03 agent 点名册 + 统一装配器](issues/03-agent-yaml-roster.md) — 两层 YAML（pool.yml + agent template）+ toolPreset 合并；旧 plugin 体系全删；形式化为 AssemblyPipeline（spec→result 形态，借鉴 input_pipeline 机制但非 process-in-place）；7 种 agent 类型收敛（external main/sub 统一 strategy、特殊 agent 构造 inline + 触发 plugin 化）；stage 划分选 A 起步 + AssemblySpec 按关注点设计；workspace/pool 骨架保持框架、资源/策略成为 stage；ComponentRegistry 全局单例 + roster 显式增删
- [04 装配搬家](issues/04-assembly-repatriation.md) — 搬家清单：ResourceFactory/ExecutionStrategy/memory/governance/hooks/interceptor/experience/approval 反射弧收回框架为管道 stage；bot_project 降格为默认组件包 + 默认点名册；create_pool/AgentTemplate.materialize 变成管道调用；experience/dream 静默跳过由 Q12 触发 plugin 化消除；factory 形注册由管道支持
- [05 图接缝](issues/05-graph-integration.md) — agent-as-node 同源已满足（图节点复用 pool 预构建 agent）；GraphSpec 新增声明式 state_schema（FieldSpec + state_schema_compiler 注入点，编译逻辑在 modex_agent 侧）；类型登记与 DATA_NAMESPACE 共用（原则 4）；变量投影不进本图；图装配代码框架级部分搬 FW、业务级留 BIZ

## SPEC

完整设计文档：`SPEC.md`（2026-08-18 落盘）。五条统一原则 + 整体架构 + ComponentRegistry + AssemblyPipeline + 图接缝 + 特殊 agent 例外 + 框架骨架 + 冲突决议记录 + 分阶段实现路径。

### 02 票修正

02 票 Q2 "保持 collect-then-inject" 被 SPEC §12 推翻——旧 plugin 体系全删（Q4），collect-then-inject 是空壳。新设计：ComponentRegistry + AssemblyPipeline 直接按名解析。

## Frontier

无。所有 8 张票已关闭。设计完备性待闭包检查（见 SPEC §16）。

## Not yet specified

- agent 自写闭环（agent 写的 skill/experience 下一回合即可用，参考项目的同步失效机制）——数据层落地后毕业。
- A→B stage 重组（SPEC §6.2 选项 A→B 迁移）——AssemblySpec 已按关注点设计，迁移成本中等，待阶段 2 完成后评估。
- 3 个保守组件外部化（WorkspacePaths/session_workspace_map/GraphOrchestrator）——SPEC §17.7 记录路径，不在 v1 范围。
- 骨架策略参数化（LRU 容量/轮询间隔/单飞超时等）——SPEC §17.6 记录，不在 v1 范围。

## Out of scope

完整清单见 SPEC §17.5（N1-N24）。核心排除项：

- **不采用参考项目的 per-agent Inbox + scope 事件架构**（N1）——参考项目的 workspace 是数据记录，不是运行时容器。完整重写骨架（~5000 行）+ 重新实现 LRU/in-flight/peer/持久池（~3000 行），高风险无对等收益
- **不实现参考项目运行时原语（作用域化执行、上下文代理、响应式系统）的 Python 等价物**（N2/N3）——TS 特定，ComponentRegistry 已是等价物
- **不做运行时热插拔/HMR**（N4）——重启生效降低所有设计复杂度
- **不做 per-session 配置隔离**（N5）——配置隔离到 pool 级，session 级只需状态隔离
- **不做动态 agent 定义**（N6）——所有 agent 启动时从 YAML 加载
- **不做插件间依赖声明**（N7）——固定 stage 顺序隐式覆盖
- **不开放 5 个延后槽位**（N8-N12）——ADAPTER/EMITTER/EXTERNAL_PROVIDER/GOVERNANCE/APPROVAL_CLASSIFIER/SANDBOX/ASSEMBLY_STAGE
- **不替换 6 个骨架组件**（N13-N18）——WorkspaceRegistry/PoolRouter/AgentPool/SessionTreeManager/AgentMessageBus/特殊 agent
- **不做 per-step model selection 框架层**（N19）——通过 hook 实现
- **不做 per-workspace 私有插件目录**（N20）——用全局 registry + roster 引用近似
- **不做变量面板/事件面开放/eval 适配**（N21-N23）——v1 只做变量面
- **不将特殊 agent 走 AssemblyPipeline**（N18）——tool 特殊配置不可组件化，例外封闭

### 架构选型理由

保持当前架构（共享总线 + 持久池 + LRU 容器）的优势：持久池零延迟 / LRU eviction / in-flight turn protection / 跨 pool peer 通信 / 显式 session 树 / poll-driven 收敛 / 已验证生产运行。

缺点（诚实承认）：8 个骨架硬耦合 / 组件交互复杂 / 插件化边界受骨架限制 / 无作用域自动 disposal / 首次 pool 创建有成本。

详见 SPEC §17 + ADR-0041。
