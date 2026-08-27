# SPEC：Scope 统一装配体系

**状态**：已实施（2026-08-22 落地，票据 01-19 全部关闭；实现期勘误与 ADR 锚点审计见 §13）
**决策依据**：ADR-0042（Scope 声明树）、ADR-0043（双执行模型）
**前置体系**：ADR-0041 / `docs/design/scope-converge/SPEC.md`（组件注册表 + 装配管道，已实施，票据 01-08 全关闭）

---

## 0. 一句话

把"agent 怎么组装"从 Python 胶水变成一份树形声明：workspace / pool / agent 全部是配置声明的装配单元，装配是纯函数，执行各归其位，贵的基础设施全部共享复用。

---

## 1. 背景与动机

### 1.1 已有的地基（本设计不重新发明它们）

ADR-0041（scope-converge 体系）已交付并验证：

- **10 槽 `ComponentRegistry`**（tool / hook / llm_provider / system_prompt / memory_system / interceptor / command_handler / execution_strategy / input_stage / data_namespace），全局单例，只存工厂
- **`Plugin` ABC + 四源加载**（bundled > project > user > entry_points），故障隔离 + 原子注册
- **`AssemblyPipeline`**（main 4 stages）+ 统一装配核心 `assemble_native_agent`（main/sub 共用，5 槽位解析）
- **YAML roster → `AssemblySpec`**（frozen）：`SpecBuilder.from_roster()`，按名引用组件
- **MCP 共享层**（ADR-0017）：`McpConnectionRegistry` → `SharedMcpBackend`
- **T0.1/T0.2/T0.3 测试**：只写 YAML + 插件即可定义新 agent

"灵活注册"在**槽位维度**已经解决。本设计解决的是它上面的**组合维度**。

### 1.2 痛点（代码级证据）

1. **BIZ 胶水旁路**：`bot/service/builders.py` 直接构造工具实例（terminal 三合一 `:450-468`、subagent 的 `SubprocessTool` `:424`）、todo 路径注入、experience 组件构造、`_build_terminal_manager` fallback 阶梯——注册表建好了，但业务装配绕开它走第二条路
2. **main/subagent 类型分裂**：`MainAgentSpec` / `SubagentSpec` 两个类型 + 两套字段表 + `main_agent_name`（还默认取目录名）——位置信息被类型系统硬编码
3. **workspace/pool 组装是 Python 不是声明**：`bot/workspace/wiring/` 4 个模块 + `bot/service/pool/` 11 个模块的资源选择与接线全在代码里
4. **配置表达力不足**：无嵌套（subagent 的 subagent 只是潜在能力）、无 profile（同形 agent 重复配置）、无 workspace 资源声明面（工具配置里散落路径知识）
5. **装配时机僵化**：pool 创建一次冻结，main 急切注册 / subagent 懒物化的差异靠调用点分支而非声明

### 1.3 用户诉求（审议归纳）

1. 所有组件插件化 + 配置化，动态生成、配置化调用
2. 每次调用组装逻辑对象，MCP/inbox 等基础设施复用
3. workspace = tool/memory/运行路径的选择配置；pool = 区分 agent 树的人造概念；peer = 树间联系——它们也应是配置装配单元
4. tree = node→subNode 组合 + 合法性校验 + root 唯一推导；root/sub 的特殊配置按位置推断
5. 先有完整插件化组装体系，**收敛实现**——替代胶水而非增量
6. 插件能拿到的数据流/配置（上下文链）是关键设计面
7. 配置是图形的（树），但执行调度各归其位

### 1.4 参照系：参考项目的启示与扬弃

**参考项目做对的（我们吸收）**：

| 参考项目机制 | 我们的对应物 |
|---|---|
| 组件即插件（"everything is a plugin"） | ADR-0041 已落地 |
| 双平面（host / agent）资源归属 | **上下文链**（三层）——但归属从注释约定变成类型契约 |
| Standing mounts（同配置 session 共享组合子树，~3ms） | 常驻池 + config 盖章缓存（本就共享，无需机制） |
| 分层 patch + profile 组合宏 | **Profile**（继承+深层合并） |
| per-step 视图组装（prompt/工具视图每步重投影） | per-turn 覆盖缝（BotModelProvider 先例，待推广） |

**参考项目不适合我们的（我们扬弃）**：

| 参考项目机制 | 弃用理由 |
|---|---|
| 参考项目的 Fiber 生命周期（disposal 级联、泄漏审计、generations） | 我们是持久池 + 无状态工厂——插件不持有生命周期，装配是纯函数，"正确地拆掉"的机器整个不需要 |
| per-session agent 创建 | 我们选三层生命周期（见 N1），会话隔离靠 session-scoped memory |
| 无 workspace/pool 容器概念 | 我们的运行时需要它们（多活 / 树间通信），参考项目没有 multi-live workspace |

**我们的结构性优势**（比参考项目好的部分，来源：`WorkspaceRegistry[R]` 本就是泛型机器、寻址已按 (workspace, pool) 两级）：平面归属从文档纪律变成结构事实；层级统一为一条继承链，没有第二套平面概念。

---

## 2. 设计原则

- **P1 装配是纯函数**：`(配置, 上下文链) → agent`。无副作用、无生命周期回调、无 disposal。参考项目一半的插件机器花在"正确地拆掉"上，我们靠持久池 + 无状态工厂直接不需要它
- **P2 三层生命周期**：作用域持有的共享基础设施（MCP 连接为进程级；broker 为 workspace 级；inbox/bus/memory 句柄为 pool 级——作用域内所有装配共享，仅随作用域逐出而重建）→ config 盖章的常驻缓存（配置变了才重建）→ per-turn 视图组装（永远新鲜）。闭环检视修正：初稿误标"进程级永不重建"与代码事实不符（inbox/bus 每 pool、broker 每 workspace、逐出即重建）——负载性质是"作用域内共享复用"，仅 MCP 连接为真进程级。
- **P3 声明与执行分离**：声明树是编译期纯数据（校验→推导→装配），不进入任何引擎；运行时机器吃装配产物
- **P4 类型即能力边界**：工厂签名声明可读的上下文层，越层访问是类型错误，不是 code review 约定
- **P5 收敛纪律**：逐 wave 净减或持平，每 wave 附删除台账；唯一豁免是上下文链地基（一次性投入），且下一 wave 必须点名对冲删除目标
- **P6 骨架固定、槽位可换**：10 个组件槽可换（ADR-0041）；骨架机器（InboxPoller/Bus/SessionTreeManager/WorkspaceRegistry/PoolRouter）不可换，行为参数走配置
- **P7 逢二再抽象**：kind 只有 workspace/pool 两个预制值；不开骨架槽，直到第二个真实宿主形态出现（"一个 adapter 是假设，两个才是真缝"）

---

## 3. 核心模型

### 3.1 Scope：唯一结构原语

```
层级          选择什么（继承父层 + 声明差异）      产出什么
Workspace    资源选择：memory 后端、路径布局、      供下层共享的资源句柄
             共享基础设施（MCP server 集、
             media store、trace 设施）
Pool         树声明：root=main、children=          一棵 agent 树的装配规格
             subagents（可嵌套）、links=peers       + 树间链接
Agent        组件组合：tools/hooks/prompt/          一个 agent 的全部组件实例
             llm/memory/strategy...
```

- **kind 双面性**：数据面（资源、嵌套、默认值）是配置；机器面（workspace 的多活注册 + state.db 布局；pool 的 bus/inbox/poller 通信作用域）是骨架
- **继承是树，通信是图**：资源/路径/默认值沿 parent 链解析（单亲）；通信拓扑 = 树边（parent-child 派生）+ links 边（peer，根对根），从声明派生
- **两层即可起步**：单 pool 无 workspace？声明 pool 为根作用域。三层嵌套？声明就是。v1 kind 枚举 = `workspace` / `pool`（agent 是 pool 内部数据，见 N6）

### 3.2 统一 AgentSpec 与位置推导

`MainAgentSpec` / `SubagentSpec` 合并为一个 `AgentSpec`；根 = 唯一 in-degree-0 节点（推导，不声明）。

**位置推导默认值表**：

| 维度 | 根节点默认 | 非根节点默认 | 覆盖 |
|---|---|---|---|
| memory | archive/core/experience 预设 | session-only | `memory:` 块 |
| approval 资格 | 可选开启 | 不可（v1 策略校验） | — |
| task 工具 | 有子则得 | 有子则得 | 见 V6 硬校验 |
| 注册时机 | 急切（boot） | 懒（首次 dispatch 物化） | `eager: true` |
| 工具集默认 | FW 预设 full（root 位置默认） | FW 预设 read-write（非根位置默认） | `tools:` / `tool_supplements:` / profile |

**task 工具的动态解析**（打磨确认）：可用 subagent 列表从声明树推导（直接子节点，不含孙代——孙代归子节点自己派）；叶子 agent（无声明子节点）的装配产物里**没有** task 工具（不是启用了但为空）。同一模式覆盖 `send_to_peer`（有 link 才注册）与 `send_to_agent`（非根皆有，父目标来自 parent 引用——机制形状见 §5.2）。推导发生在 ScopeCompiler 编译期；改树重启生效。运行时的"动态"在会话树侧（同 agent 多次派生 = 多个 invocation 分支），与工具可用列表无关。

### 3.3 上下文链（插件数据表面）

```
WorkspaceContext（路径布局、workspace 级资源句柄）
    ↓
PoolContext（pool_runtime 依赖、memory 句柄、terminal manager、通信设施）
    ↓
AgentContext（agent 身份、parent、invocation 数据、per-agent spec 引用）
```

每层 frozen 类型化载体；泛化现有 `AssemblyContext`，吸收 `SubagentInvocationContext` 特例。

**before/after**（以 todo 工厂为例）：

```python
# 现状（builders.py，路径知识散落在每个构造点）：
todo_store = TodoStore(root=ws / ".modex/runtime_state" / pool / "todos")
tm.register(TodoWriteTool(todo_store))

# 设计后（工厂声明层依赖，路径知识只住在 workspace 层）：
class TodoToolFactory(ComponentFactory):
    async def create(self, config, ctx: PoolContext) -> Tool:
        return TodoWriteTool(ctx.pool_runtime.todo_store)
```

工具配置**零路径字段**。工厂拿不到的层，类型上就取不到——这就是"收紧各层能力"的落地。

### 3.4 Profile（命名默认组合宏）

- 解析语义：**继承 + 深层合并**——框架默认 ← profile ← 本地声明（贴合 SpecBuilder 现有 defaults 模式；参考项目的整行替换被否决：嵌套配置里改一个标量会静默丢弃同块兄弟字段）
- 规则 1：**单层引用**——profile 不可引用 profile（配置校验直接报错）
- 规则 2：**列表整字段替换**——`tools` 写了就是整个列表；逐项增删只保留专用机制 `tool_supplements`
- 规则 3：**最终账单可查**——WebUI/校验器可展示每个字段最终值来自哪层（框架默认/profile/本地），以及每个组件的实现来源（哪个源的哪个插件提供了它——O2 覆盖审计面）。数据通路（闭环检视定案）：账单按请求从 YAML 声明重算（P1 纯函数保证廉价），经 W5 的 REST 端点提供；**不做启动期缓存**——否则 WebUI 写回未重启时（S2）会展示与磁盘矛盾的过期来源
- 归属：`config/profiles/`；FW 预制标准 profile，BIZ 可加自己的（如 bot-standard）。profile 存储随启动加载、进程生命周期持有（与 ComponentRegistry 同生命周期类——配置每进程静态，S2）。
- **tool_preset 字段的归宿（闭环检视定案）**：死亡。删除测试给出唯一答案——携带工具集的 profile 能做 tool_preset 做的一切，两者并存即同一关注点两条路径。其取值落位为位置推导默认（root → FW 预设 full、非根 → FW 预设 read-write，与现状行为一致，满足 split-brain 判据）；`tool_supplements` 保留（列表增补语义是"整字段替换"的显式例外，已有专用机制）。

### 3.5 覆盖语义总纲（四层，打磨轮新增）

"覆盖"在本体系中有四种含义，语义各归其轴，共享一条元原则：**覆盖必须显式（声明或明确的源优先级）、编译期解析（不是运行时装配顺序的副作用）、来源可审计（账单可查）**。

| # | 轴 | 问题 | 语义 | 粒度 |
|---|---|---|---|---|
| O1 | 参数覆盖 | 同一组件的配置怎么合并 | 继承+深层合并（profile，§3.4） | 全局→局部 |
| O2 | 实现替换·注册键 | 两个工厂抢注册表同一 `(slot,name)` | **源优先级 user > project > entry_points > bundled**（近用户者赢；当前 first-seen-wins=bundled 赢，方向反了——与参考项目的 patch 分层对齐）。跨源覆盖记 info 日志 + `registration_source` 审计（基础设施已在）；同源同名仍 ValueError（防手滑）；直接 `register()` 同名仍 ValueError（代码路径不经源语义，`overwrite=True` 保持 test-only） | 全局 |
| O3 | 实现替换·产物同名 | 不同注册键、运行时工具名相同（ACI 模式） | **编译期替换记账**：supplement（如 `aci`）在推导有效工具集时按产物 name 替换默认条目，账单记录 `edit ← aci`；装配期 ToolManager 名字唯一。覆盖 ACI 现状（`InMemoryToolManager.register` 静默后写覆盖，装配顺序决定胜负——违反元原则，收敛对象） | per-pool |
| O4 | 列表覆盖 | tools 等列表字段 | 整字段替换（§3.4 规则 2） | — |

O2/O3 互补：全局替换走 O2（装插件即全局生效），按池替换走 O3（roster/supplement 引用决定）。ToolManager 的静默同名覆盖被消灭后，仅存的动态同名（MCP server 恰好暴露 `edit` 工具）保持后写覆盖 + warning 日志；MCP 工具命名空间策略另行处理，不在本轮扩散。

### 3.6 声明承载

- **YAML 文件为真源**（WebUI 写回文件——PoolEditor 现有模式）；重启生效（用户拍板）
- 嵌套 YAML 是**糖**：spec 模型保持扁平 frozen + `parent` 字段 + 无环校验（糖是可逆的表面决策）
- **升级路径**：泛化 GraphSpecStore 存 scope spec（带版本链 = 未来热生效的天然 generation 缝）——当动态创建 scope 成为高频操作时启用（见 N2/N13 的翻转条件）

### 3.7 配置拓扑与插件自定义配置（打磨轮新增）

**四类配置，各归其位，互不吞并**：

| 类 | 内容 | 所在 | 机制 |
|---|---|---|---|
| 组合配置（本设计新增） | 哪些组件 + 小参数块 | scope 声明 + `config/profiles/` | `config_model` 校验（**插件自定义 schema**，任意 frozen Pydantic 字段）、profile 合并、账单可见 |
| 领域配置（现状保留） | 服务级设置 | `model.yml` / `im.yml` / `bot_config.yml` | typed domain（`register_kind`），WebUI 可编辑 |
| 资源配置（现状保留） | 资源定义本体 | `config/mcp/*.json`、`skills/`、`agents/`、`templates/`、`config/graphs/` | 按名/路径引用，自有格式，消费者自管 |
| 秘密（现状保留） | 凭据 | `.env` | `${ENV_VAR}` 插值 |

**规则：组合内联，资源引用**——scope 声明只携带组合（组件名 + 小参数块）；大块/私有格式/非组合数据留在各自文件里按名引用。声明不膨胀，各层不拥挤。

**插件自定义配置的三条路**（按需选，可组合）：

| 路 | 适用 | 形态 | 先例 |
|---|---|---|---|
| A 内联参数 | 小型组合参数 | 插件声明 `config_model`（自己的 schema），roster 块 → 工厂 `create(config)` 收到 | `reference_collector`（自定义 `max_sources`） |
| B 插件自有资源文件 | 大块/私有格式/非组合数据 | config 块携带**名或路径引用** → 工厂在装配期**自己加载自己的文件**（格式与路径插件自管） | MCP（`config/mcp/*.json` 按名引用，MCP 组件自加载） |
| C 领域配置 | 服务级设置且要 WebUI 可编辑 | `register_kind` 进领域文件 | IM 适配器（`im.yml`） |

**插件全局配置**（非 per-agent，注册期一次读取）：插件是 Python 模块，可在 `register()` 里读自己的全局文件并闭包进工厂——零新机器，设计点名允许。要 WebUI 可编辑则走 C。

**路径原则澄清**（避免与 ADR-0042 "工具配置零路径字段"冲突）：零路径字段约束的是 **workspace/pool 数据路径**（todo 存储、memory 目录——必须来自上下文链，不许在配置里重复）；**插件自有资源引用**（如 MCP server 名）是插件内部解析的引用，不受此限。名字引用优于路径引用（声明可跨 workspace 移植）；确需路径的插件资源，路径解析归插件自己。

---

## 4. 四轴差异化收敛

四个正交轴，三收一留：

| 轴 | 收敛方式 | 差异的住所 |
|---|---|---|
| main/subagent | ✅ 彻底收敛 | **位置推导默认值**（§3.2）；两个 spec 类型、`main_agent_name`、两套字段表全部消失 |
| external/native | ✅ 收敛进槽位 | **EXECUTION_STRATEGY 策略组件内部**（ADR-0025 载体现成）。现有三个排除点（materialize 早分派 / create_pool 外部分支 / experience hook 早退）不消失但**搬家**——变成 external 策略自己的装配实现，调用方零 if 分支 |
| 图/会话模式 | ⚖️ **刻意保持正交** | **ADR-0039 turn-context 配置管道**（已存在）：图模式的拓扑注入/GraphDeliverTool/关审批按"消息带不带 graph 元数据"逐 turn 生效。不收进 agent 配置的理由：同一 agent 完全可能这 turn 在会话里跑、下 turn 被图节点引用（BotAgentNode 按名引用）——焊死在声明里会失去这个自由度。**模式中立硬契约（打磨轮新增）**：实例级装配产物必须模式中立——禁止把模式特定状态放进实例（如图 deliver_tool 注册进 tool_manager）；一切模式特定配置经绑定 store 按会话键控、逐 turn 生效（现状先例：GraphTurnArtifacts frozen + binding_store.bind/unbind + configurator.applies 读会话绑定）。**图引用 agent 的物化语义（闭环检视定案）**：与 session 模式完全一致——inbox 消费驱动冷启动物化（`_materialize_then_turn`，机制已存在）；BotAgentNode 的实例前置检查放松（不存在不报错，照常投递，poller 物化），描述解析改从编译声明取。配套 V10 启动校验兜住"图里写错 agent 名"（见 §7）。~~pool 级 `graphs:` 名单~~ 删除——无消费者（闭环检视 F1），WebUI 图发现维持全局 |
| 特殊 agent（reviewer/compactor） | ⏸ 本轮不动 | 代码构造 + 注释说明扩展方向（嵌套 AgentSpec 骑宿主组件 config，如 reviewer 挂 `memory_system_config` 下）。它们已与普通 agent 走同一条装配路（ScopedFileAgent → 内部 ReActAgent），迁移成本低，随时可做 |

**external/native 收敛的机制细节**：external 策略的 `assemble_main()` 返回的 StrategyAssembly 天然不带 memory/experience 协作者——"external 不吃 memory"从散落的判断点变成策略组件的显式返回值形状。

---

## 5. 代表性收敛项（before/after）

### 5.1 终端三合一

| 东西 | 现状 | 设计后 |
|---|---|---|
| 工具注册 | `builders.py` 直接构造 `CommandTool/ProcessTool/TerminalTool`；subagent 另构造 `SubprocessTool` | roster `tools: [bash, process, terminal]`，FW 工厂按名创建 |
| `use_terminal` | 隐含"注册三件套 + 建 manager"两件事 | 只管一件事：建不建 terminal manager（基础设施） |
| 平台/后端选择 | `_build_terminal_manager` 的 fallback 阶梯（BIZ Python） | **留在 FW 的 manager 工厂**——这是真正的平台逻辑（winpty/tmux/pexpect 探测），不是胶水，**不配置化** |
| bash 无后端退化 | BIZ 分支 | 工厂内部行为：`bash` 工厂解析到 manager 不可用 → 返回 SubprocessTool，对配置不可见 |

FW 侧 `BashToolFactory/ProcessToolFactory/TerminalToolFactory` 已存在（`plugins/defaults/tools.py`，含 split-brain 测试）——W2 是**删除性工作**。

### 5.2 通信工具注册

现状（两条注册路径并存）：`register_communication_tools` 只往 main 的 tool_manager 注册（BIZ 侧，target store 是 pool 级单份）；子代理咨询走 FW 侧另一条已存在的 per-agent 路径（`_register_send_to_agent`，template 物化期注册 `CommunicationTargetStore(for_subagent=True)`——代码实证：template.py:510-515，烘焙默认每个子代理都有）。

设计后——**三个通信工具收敛为一条推导路径**：

| 工具 | 获得者 | 目标来源 | 推导依据 |
|---|---|---|---|
| `task` | 有声明子节点的 agent | per-agent store 里的直接子节点条目 | 树位置（§3.2） |
| `send_to_agent` | 每个非根节点 | parent（AgentContext 的 parent 引用） | 树位置 |
| `send_to_peer` | 声明了 link 的根 | per-agent store 里的 peer 条目（含 bus/tree 引用） | links 声明 |

- **机制形状（闭环检视定案，消解 A2）**：三者都是编译器推导注入生效工具集的条目——不是用户 roster 声明的，也不是物化期旁路注册的——经 TOOL 槽位的 FW 预制工厂解析，工厂从 AgentContext 读取 per-agent CommunicationTargetStore。V6 所查的"生效工具集"即推导后的 spec.tools（含注入条目）。这是把现有 FW per-subagent 模式推广到全部通信工具，一条路径。
- **pool 级 CommunicationTargetStore 死亡**（W4 删除台账点名）——per-agent store 全面替代。
- **peer link → bus/tree 引用的获取点（闭环检视定案，闭合 SG1）**：materialize 期经作用域路径解析，从所属 workspace 的资源束获取 peer 的 bus/tree 引用——现有 BIZ Phase 2 peer wiring 的 FW 化迁移（W5 删除台账的对应项）。v1 同 workspace 不变量保证两端同批逐出，无跨作用域悬挂引用。

### 5.3 寻址收敛

| 现状（三份特化寻址） | 设计后 |
|---|---|
| `WorkspaceRegistry`（LRU 机器，workspace 语义独占——但它是泛型的 `WorkspaceRegistry[R]`） | `ScopeRegistry`——同一台机器，作用域通用 |
| `PoolRouter`（28 callers，workspace→pool 特化） | 作用域寻址的 pool 段 |
| `WorkspacePathResolver`（两级硬编码 `ws.pool_data.get(pool_name)`） | 沿 parent 链的通用路径解析 |

"层级可配置"不但没有增加骨架代码，反而吃掉三份特化寻址。

**寻址总纲（打磨轮新增）**：解析只走**显式持久映射或显式声明**，miss 即响亮失败——不做环境推断，不做静默回退。现状先例：路由存储 miss → 记错误日志并丢弃（PoolRouter）；图节点 pool miss → RuntimeError；subagent parent 链由 envelope 携带而非从 store 反推（`materialize_agent` docstring 原话：显式优于反推是代码库付过学费的教训）。配套迁移：`_reconcile_pool_for_agent`（pool_router.py:178，运行时遍历全 pool 搜索服务该 agent 名的池——现存唯一的"反推"启发式）在 W3 迁移为**声明查表**——agent→pool 归属在编译期已知，一次查准，消除全池遍历与误判面。

**workspace 并行隔离不变量（打磨轮新增）**：多 workspace 并行时互不影响，由四层结构性隔离保证（全部为骨架机器，本设计不动）：①数据层——每 workspace 独立资源束，路径按 (workspace, pool) 两级寻址；②工具层——`WorkspaceScopedTool` 每次 execute 现读 per-workspace 根提供器（装配时注入，非全局可变量）；③turn 层——`bind_workspace_root` contextvar 每 turn 任务重绑（asyncio 任务上下文隔离）；④归属层——**pool 恰好属于一个 workspace（scope parent 链）**：会话切换 workspace（/cd）改变的是路由指针，不改变 pool/agent 归属——A 工作区的 agent 结构上不可能读写 B 的根。装配侧的对应约束：路径知识唯一来源 = WorkspaceContext.paths（上下文链传递，工厂声明读层），BIZ 散落式路径注入全部死亡（W2/W3 台账）——"漏传"从 code review 拼运气变成类型错误/启动报错。已知残余风险仅 F6/D1 逐出待办（见 §10）。

### 5.4 同名工具替换（ACI 案例）

现状（打磨轮查证）：`AciEditTool` 继承 `EditFileTool`——name/description/parameters 原样继承（"同名同 schema、不同实现"），激活时靠 `InMemoryToolManager.register` 的**静默后写覆盖**顶掉标准 edit——装配顺序决定胜负，无审计。另：注册表层跨源同名是 first-seen-wins（bundled 赢，用户插件被 skip）——用户想"自定义覆盖内置"时**方向恰好相反**。

设计后（O2/O3 语义，§3.5）：

| 场景 | 路径 |
|---|---|
| 按 pool 用 ACI 版 edit（现状语义保留） | roster `tool_supplements: [aci]` → 编译期替换记账（有效工具集推导时 `edit ← aci`，账单可查）→ 装配期名字唯一 |
| 全局换掉 edit | 用户/项目插件注册 TOOL 槽 `edit` 工厂 → O2 源优先级（user > project > entry_points > bundled）覆盖 bundled，info 日志 + 来源审计 |
| 同一插件注册同名两次 | 启动 ValueError（手滑防御） |
| 代码直接 `registry.register()` 同名 | ValueError（不经源语义，静默覆盖禁止） |

W2 交付判据补一条：ACI 场景 split-brain——同一 pool 配置下，旧路径（ToolManager 后写覆盖）与新路径（编译期记账）产物一致，且新路径日志/账单可见替换关系。

### 5.5 会话树（明确不动的部分）

- SessionTreeManager 机器 = 骨架，每 pool 自动创建、永远开启
- SessionTreeStore = 已插件化（InMemory/LocalFile/Sqlite 三实现，装配时按 workspace 配置选）
- 会话 ID 格式 `{prefix}.{agent_name}`（**严格两段**，点分隔；invocation_id 是子代理会话的**前缀**而非可选第三段——见 §13 Errata-1）、invocation 分支、session group 前缀语义——全部原样
- 声明树管"谁**可以**派谁"，会话树记"谁**实际**派了谁"——前者是出生证明，后者是行动记录，永不相混

---

## 6. 明确不做的事（N 清单）★

每条含内容、理由、翻转条件。**这是本设计的边界，与"要做什么"同等重要。**

| # | 不做 | 理由 | 翻转条件 |
|---|---|---|---|
| N1 | 字面 per-turn 重建 agent | 审批挂起恢复（GraphInterrupt + TurnSnapshot 跨重建等价性）、in-flight turn 撕裂——代价远超收益；参考项目也不做（per-session 组合 + per-step 视图） | 无（三层生命周期已是正确翻译） |
| N2 | 热重载 | 用户拍板：重启生效可接受。**缝已预留**：AssemblySpec 纯函数 hash + pool generation 计数器（~50 行，Seam wave） | WebUI 配置编辑高频化，重启成本开始疼 |
| N3 | modex_graph 承载声明 | GraphSpec/TopologyValidator 是**执行语义**（强制 START/END、环合法、必须达 END）——对声明树全错（叶子合法死端、树必须无环）；塞进去是伪类型 + 破坏 framework-agnostic 边界。**modex_graph 零改动** | 无（基座模式同构复用：typed spec + 纯校验器 + 画布编辑器模式，类型新建在 modex_agent 侧） |
| N4 | 开骨架槽位（InboxPoller/Bus/SessionTreeManager/WorkspaceRegistry/PoolRouter 可替换） | 无第二真实宿主形态；开槽 = 数千行抽象 + 推翻已验证架构。**既有缝已够**：`ResourceFactory[R]` 泛型、InboxMQ backend、persistence backend、ExecutionStrategy 槽 | 出现第二个真实宿主形态（远程 pool / per-workspace 进程隔离） |
| N5 | 跨 workspace peer | 两个园区的 state.db/总线/LRU 要互相打通，每个都是大工程且离"远程 pool"（骨架雷区）一步；现实无需求。**声明形状不带 workspace 硬编码**——将来开 = 放宽校验 + 桥接插件，schema 不变 | 跨园区协作成为真实需求 |
| N6 | agent 提升为 scope kind | agent 级资源束（独立 LRU 逐出）需求未出现；`SubagentInvocationContext` 并入 Agent 装配层已覆盖数据传递 | 某 agent 需要独立逐出的贵资源 |
| N7 | 会话编排图化（动态边铸造/运行时 DAG 编排 agent） | session 模式是 actor 模型（邮箱/任意时刻准入/天级生命周期），引擎是 dataflow（声明边/终态）——语义不匹配，统一 = 用 dataflow 硬演 actor | 树派生本身需要编译期声明的分叉/汇合（即便那时也优先"编排器节点调用业务图"） |
| N8 | 特殊 agent（reviewer/compactor）配置化 | 用户拍板：保持现状，注释留扩展方向；收益最小、动的东西不少 | 想给它们做 WebUI 可编辑配置时 |
| N9 | "并行派生"新机制 | **已存在**：task 是异步投递（发完即走、结果回流 fold-in/新 turn），无上限；唯一不存在的是"声明式汇合闸门"，那恰好是业务图模式管的事（BotAgentNode 引用同一份声明） | 无（机制已在，误会已澄清） |
| N10 | profile 嵌套引用 | 超过一层没人能心算"到底听谁的"；校验直接报错 | 无 |
| N11 | 为刚写的代码留兼容垫片 | 仓库收敛规则 2：旧路错就删、对就收敛；不搞 deprecation 别名 / fallback 分支 / 并行实现 | 无 |
| N12 | 扩 RecordScope 维度词汇表 | 持久化层地震（SQL 生成列全动）；新 kind 复用现有 11 维，超出者进 `metadata_json` | 新作用域身份确实成为高频查询维度 |
| N13 | session 级配置覆盖层 | scope-converge N5 维持延后；scope 面全量落地后再评估 | per-session 工具子集/默认图成为真实需求 |
| N14 | 改会话 ID 格式 / 会话树运行时机制 | 运行时身份系统地基；设计只消费不改变 | 无 |
| N15 | 引入第二个容器概念（scope 之外再发明 plane/region/namespace） | 上一轮审议的教训：概念越多，归属越说不清。一切层级问题都在 scope 体系内表达 | 无 |

---

## 7. 校验规则全集（ScopeTreeValidator）

纯函数、确定性、无副作用（TopologyValidator 同款风格，但规则面向树声明）。**两阶段结构（闭环检视修正）**：声明形状规则在推导前跑；生效值规则（V6/V9）消费编译器推导产物，不可能在推导前检查——校验器输入契约 = 声明树 + profile 存储（阶段一）、推导后 effective 配置（阶段二）。

**阶段一：声明形状（推导前）**

| # | 规则 | 报错时机 |
|---|---|---|
| V1 | 无环 | 启动（spec 加载后） |
| V2 | 连通（每个声明节点从根可达） | 启动 |
| V3 | 每 pool 树恰一根（in-degree-0 节点唯一） | 启动 |
| V4 | kind 层级合法：workspace > pool；agent 为 pool 内部数据，嵌套深度不限 | 启动 |
| V5 | peer 端点存在、同 workspace（v1）、双向不变量（沿 ADR-0019）、根对根。以 pool 为根作用域的声明（无 workspace 层）v1 不可声明 peer——"同 workspace"前提对其无定义 | 启动 |
| V7 | profile 引用单层（输入需含已加载的 profile 存储） | 启动 |
| V11 | **pool 内 agent 名唯一、workspace 内 pool 名唯一**——键控链（`pool.get(name)`/`serves_agent`/parent 按名引用/编译器产出键）全部押在此假设上；兄弟同名 = 静默碰撞（后者覆盖前者 / parent 解析歧义），即"错误路由到另一 agent 配置"的唯一现实入口 | 启动 |
| V10 | **图节点引用存在性**：每个已加载图 spec 中 BotAgentNode 声明的 (pool, agent) 必须存在于声明树（交叉校验，输入含已加载图 specs；实现于校验器或图加载路径皆可）。兜住"图里写错 agent 名"——比运行时报错早一个启动周期 | 启动（图 specs 加载后） |

**阶段二：生效值（推导后）**

| # | 规则 | 报错时机 |
|---|---|---|
| V6 | **有声明子节点的 agent，其推导后生效工具集必须包含 task 工具**——显式 `tools:` 或 profile 整列表替换掉 task 的，启动报错（防配置静默孤儿：子树声明了却永远不可达）。输入 = ScopeCompiler 推导输出的 effective 工具集 | 启动（推导后） |
| V9 | **非根节点声明 approval → 启动报错**（沿 ADR-0008 main-only 策略）。统一 AgentSpec 后类型分裂不再天然阻挡 approval 字段下沉到子节点，此校验是替代守卫——漏掉则子代理 turn 中途挂起等待一个永远不会到达的审批（审批渲染器只接在 main 管线上） | 启动（推导后） |

V8（列表字段整字段替换）为语义规则，文档化 + WebUI 提示，不运行时强制。

---

## 8. Wave 计划与删除台账

体系先行（用户定的顺序），每 wave 净减或持平（P5 纪律）：

| Wave | 内容 | 删除台账（对冲目标） | 交付判据 | 规模估算 |
|---|---|---|---|---|
| **W1 声明基座** | ScopeSpec + ScopeTreeValidator（两阶段 V1-V11）+ 统一 AgentSpec（位置推导默认值表）+ 上下文链三层类型。纯类型与解析，零行为变更；**含注册表源优先级反转**（O2：user > project > entry_points > bundled，loader flush 从 skip 改为覆盖 + info 日志） | `SubagentInvocationContext` 特例（并入 AgentContext）；Main/Sub spec 分裂；`tool_preset` 字段（取值落位为位置默认 profile，见 §3.4） | 校验器单测矩阵全绿；既有测试零回归；源覆盖行为变更测试（user 插件覆盖 bundled 同名组件生效且可审计） | ~600 行 |
| **W2 垂直切片** | 终端三合一 + todo 走 roster（§5.1） | `builders.py` 工具构造段；BIZ 侧 trio fallback 阶梯 | **split-brain 一致**：同一 pool 配置新旧两路产物完全一致；1900 bot 测试 + T0.x slot gates 全绿 | ~300-500 行 |
| **W3 ScopeCompiler + pool 接管** | 树 → per-agent AssemblySpec → 既有管道；`create_pool` 的 11 模块四分类迁移（变配置 / 变槽位组件 / 变 supplied infra / 直接删） | pool/ 模块胶水；`main_agent_name` 解析链；**上下文链的對冲删除在此兑现** | 同行为配置重启后等价运行；W2 的 split-brain 模式扩展到全组件 | ~1000 行 |
| **W4 树激活** | 有子者得 task 工具（含 per-agent target store）；send_to_agent 按 parent 推导（§5.2 三工具推导表）；dispatch 源类型放宽；**BotAgentNode 前置检查放松**（实例不存在 → 照常投递由 poller 冷启动物化，描述改从编译声明取，pipeline 前置校验随实例检查一并移除）；V6/V9/V10 生效 | `register_communication_tools` 的 main 独占逻辑；pool 级 CommunicationTargetStore（per-agent store 全面替代） | 三层嵌套树（main→sub→subsub）dispatch 全链路通；**图引用懒叶子（此前从未 dispatch）全链路通**；会话树/invocation 分支不回退 | ~600-800 行 |
| **W5 workspace 声明面 + WebUI 树画布** | workspace 资源声明面；寻址收敛（§5.3：3 router → 1 scope path）；TopologyCanvas 复用渲染推导边 | workspace wiring 4 模块；三份特化寻址 | 同 pool 两会话不同资源互不渗漏；WebUI 树形可视化 + profile 账单视图 | ~800 行（含前端） |
| **D1 逐出保护补全（待办，不进当前 wave）** | 见 §10 预留缝表——激活 ScopeRegistry 容量**之前**必须完成 turn 括号补全，W5 的交付判据明确**不含**容量激活 | — | — | — |
| **W6+ session 覆盖层** | 延后（N13），另行立项 | — | — | — |
| **Seam** | spec-hash + generation 计数器（N2 预留缝，不建换装机制） | — | hash 稳定性单测 | ~50 行 |

---

## 9. 测试与验收

### 9.1 验收判据（整个体系"完成"的定义）

1. **零胶水声明**：一份嵌套 scope YAML——根是 external、中间层 native 带 terminal、叶子被业务图引用——全部声明出来，BIZ 无一行工具/资源构造代码
2. **split-brain 一致**：W2/W3 的每个收敛项，旧路径（删除前）与新路径产物逐项一致
3. **三层树全链路**：main→sub→subsub 的 dispatch、结果回流、会话树记录（含 invocation 分支）全通
4. **V6 孤儿防护**：声明子节点 + 覆盖掉 task 的配置 → 启动即报错（有测试）
5. **记账兑现**：逐 wave 删除台账全部兑现，总账净减或持平（上下文链投入在 W3 对冲）**（勘误：总账终账实际净 +2482，本判据未达成——删除台账半边成立、净减半边失败，见 §13 Errata-8）**
6. **WebUI**：声明树画布渲染（推导边）+ profile 最终账单视图

### 9.2 测试矩阵

- **校验器单测**：V1-V7 每条正反用例（V8 为文档语义）
- **位置推导单测**：根/非根/中间层默认值表逐行
- **split-brain 回归**：scope-converge Errata-8 W0-W6 已验证的模式，扩展到工具/通信/寻址
- **集成**：三层嵌套 dispatch；external 根 + native 中间层混合树；peer links 双向
- **守卫**：架构测试扩展——BIZ 禁止直接构造工具实例（import 层面拦截，可选）
- **底线**：1900 bot 测试 + T0.x slot gates + 框架全量测试，任何 wave 落地时不许红

---

## 10. 延后项与预留缝（不建机制，只留口）

| 缝 | 现在做什么 | 将来怎么开 |
|---|---|---|
| **逐出保护窗口补全（待办）** | 记录为 W5 后待办：现有 begin_turn/end_turn 只括住消息**投递**，poller 驱动的 turn 执行期（含异步子代理 turn、图编排 turn）无保护——激活容量后逐出会取消在跑的 turn。激活 ScopeRegistry 容量（`max_materialized`）**之前**必须先把括号补到 turn 执行期（poller 与图编排器两处调用）+ 容量旋钮进配置 | W5 落地后、任何人要配置容量时——补全 turn 括号是硬前置条件 |
| 热生效（N2） | spec-hash + generation 计数器 | 换装机制：旧 agent 跑完 in-flight turn（PipelineSnapshot 式 pinning 先例）+ 审批挂起跨换代恢复测试 |
| 第二宿主形态（N4） | 承诺 `ResourceFactory[R]` 泛型缝为入口 | 出现远程 pool / 进程隔离形态时开骨架槽 |
| 跨 workspace peer（N5） | 声明不带 workspace 硬编码，校验器拦截 | 放宽校验 + 跨园区桥接插件 |
| spec store 升级（N13 相邻） | YAML 文件真源 | 动态创建高频后：泛化 GraphSpecStore，版本链即 generation |
| 特殊 agent 配置化（N8） | 代码注释说明扩展方向（嵌套 AgentSpec 骑宿主组件 config） | 需要时直接迁——装配管道已统一 |
| session 覆盖层（N13） | 延后 | scope 面全量落地后评估 |

---

## 11. 与既有决策的关系（不推翻清单）

| 既有决策 | 关系 |
|---|---|
| ADR-0041 SPEC §17 骨架决策（持久池/共享总线/LRU/InboxPoller） | **全部保持**——本设计动装配侧不动运行时侧 |
| scope-converge N1-N6 | N5（session 级配置）延后维持；其余不变 |
| ADR-0019 peer 语义（根对根、双向不变量、session group） | **原样保持**——树框架下 peer 语义不变（本轮审议曾误判为"泛化 link"，已纠正） |
| ADR-0015 修订版（InboxPoller/inflight/fold-in） | 不动 |
| ADR-0025 ExecutionStrategy 槽位体系 | **正是 external/native 收敛的载体重用** |
| ADR-0033/0034 modex_graph | 零改动（N3） |
| ADR-0039 turn-context 管道 | 正是图/会话差异的住所（正交保持） |
| ADR-0008 approval main-only | 位置推导的默认值来源之一；非根声明 approval → v1 校验报错 |
| 仓库收敛规则 1/2 | 本设计的执行纪律就是它们的实例化（P5/N11） |

---

## 12. 术语

见根 `CONTEXT.md`：Scope / Scope Kind / Declaration Tree / Session Tree / Skeleton / Context Chain / Profile（*Forthcoming per ADR-0042* 标注已随实施落地摘除，定义已对照实现修订——实现真源：`src/modex_agent/scope/` 七模块 + `plugins/assembly/context.py` 菱形载体）。

---

## 13. Errata

### Errata-1: 会话 ID 为严格两段制，invocation_id 是前缀不是后缀（2026-08-21，设计定稿后独立代码核验发现）

**原文**（§5.5）：会话 ID 格式 `{prefix}.{agent_name}[.{invocation}]`。

**修订后**：会话 ID 规范格式是**严格两段** `{prefix}.{agent_name}`（点分隔）。invocation_id 不是可选第三段——它是子代理会话的**前缀**（第一段）。`{conv}.{agent}.{invocation_id}` 三段式是 legacy 形态，仅被 `agent_of()` 容忍以兼容历史持久化数据；工厂层面不可能产生三段 ID（`create_with_prefix` 对含 `.` 的前缀直接 `ValueError`）。

三种会话的真实形态（全部两段）：

| 会话 | 格式 | 生成点 |
|---|---|---|
| 主会话 | `{encode_snowflake(conversation_id)}.{main_agent_name}` | `SessionIdFactory.create`（`src/modex_agent/core/session_id.py:162-178`） |
| 子代理会话 | `{invocation_id}.{subagent_name}`（invocation_id 逐字作前缀） | `SubagentDispatchStrategy`（`src/modex_agent/multi_agent/communication/strategies/subagent_dispatch.py:37`） |
| peer 会话 | `{sender_prefix}.{peer_agent_name}`（复用发送方前缀 → 隐式 session group；对端回复落回 `{sender_prefix}.{sender_agent_name}`） | `PeerNormalStrategy`（`src/modex_agent/multi_agent/communication/strategies/peer_normal.py:36`） |

**"invocation 分支"的真实语义**：每次派生铸造新 invocation_id 作为子会话**前缀**——同一父对同一 agent 多次派生 = 多个不同前缀 = 多个会话；请求/应答靠 `MessageTrack.invocation_id` 闭环（`session_tree/models.py:102-105`），fork 版本靠 `TreeNodeRecord.parent_version` 记录。"session group 前缀语义" = 同前缀共享（peer 回复落回发送方前缀的根会话）。

**代码依据**：`src/modex_agent/core/session_id.py:54/64/212`（两段构造）、`:140-148`（`agent_of` docstring 明言三段为 legacy、取中段）、`:193-202`（前缀含 `.` 抛 `ValueError`）。根 `CONTEXT.md` 的 session id 术语条目（两段制描述）与本修订一致——术语表本来就是对的。

**理由**：SPEC §5.5 的括号可选段写法与 `examples/bot_project/AGENTS.md:48/:136` 的过时描述同源（该行还引用已不存在的 `DefaultSessionIdStrategy`；三段式仅作为 legacy 被 `agent_of` 兼容）。不修正的风险：票据 12（嵌套树）/13（peer 解析）实现者可能按三段式解析或校验会话 ID、误以为 invocation_id 是后缀而错建 session group 语义（peer 回复落点依赖前缀复用）、或在装配/校验代码中"容忍"含点前缀——与工厂硬错误直接冲突。

**影响**：仅事实描述修正；"会话 ID 格式不动"的设计决策本身不变，ADR-0042 无需改动。票据 12/13 验收标准写的是 "session-id format unchanged"，语义不受影响，无需改票。同步修正两处误导源：`examples/bot_project/AGENTS.md:48/:136`（格式描述 + 已死的 `DefaultSessionIdStrategy` 引用）、`src/modex_agent/core/AGENTS.md`（移除不存在的 `DefaultSessionIdStrategy`/`conv_id_of`）。

### Errata-2: ACI split-brain 交付判据的闭合时机拆分（2026-08-21，实施规划期定稿）

**原文**（§5.4 W2 交付判据补条 + §8 W2 行）：ACI 场景 split-brain——同一 pool 配置下，旧路径（ToolManager 后写覆盖）与新路径（编译期记账）产物一致，**且新路径日志/账单可见替换关系**——全部于 W2 闭合。

**修订后**：该判据拆为两半、分批闭合——**行为对等**（旧路/新路的有效 `edit` 工具逐项一致：同为 AciEditTool 实现）在 W2（票据 05）验证；**记账可查询**（替换记录 `edit ← aci` 进入编译期 provenance 数据、日志/账单可查）随 W3 ScopeCompiler（票据 06）闭合。

**理由**：记账是编译器的产物，票据 05 落地时编译器尚不存在；把两个判据捆在 W2 会迫使 05 号要么推迟等编译器、要么做半截验证（实施规划 Metis H1）。

**影响**：仅闭合时机澄清，判据本身不变；票据 05 验收的 ACI 段已按此拆分表述；§8 波次表 W2/W3 行据此解读。

### Errata-3: §8 波次表与实际执行的波次映射（2026-08-22，实施收官核验——Metis M4）

**原文**（§8）：波次表为 W1（声明基座）→ W2（垂直切片）→ W3（ScopeCompiler + pool 接管）→ W4（树激活）→ W5（workspace 声明面 + WebUI 树画布）→ Seam，外加 D1/W6+ 延后行；W1 删除台账列 `SubagentInvocationContext` / Main-Sub spec 分裂 / `tool_preset`，暗示 W1 波内兑现；W3 行称"上下文链的對冲删除在此兑现"。

**修订后**：实际执行为七个波（实施计划口径）：W1 = 票据 01/02/03/04；W2 = 票据 05；W3 = 票据 06/07/09/10；W4 = 票据 08/12/13；W5 = 票据 14/16/18；**W6 contract = 票据 11/15/17**；W7 = 票据 19（本文档同步）。两点与 §8 表格的偏差：(i) **票据 11（全部 pool 上声明 + 旧路径删除 contract）与票据 17（多 workspace 并行 + WebUI 运行时新建）在 SPEC 波次表中无对应行**——它们是实施规划期加入的 W6 contract 波与用户点名的一等需求；(ii) **W1 删除台账实际跨三张票兑现**：`tool_preset` 取值落位为位置推导默认（票据 02，W1），`SubagentInvocationContext` 死亡（票据 10，W3），Main/Sub 类型分裂 + `main_agent_name` 死亡（票据 11，W6）——净减不变量（P5）的对冲账因此闭合于 W6 而非 W1，W6 增量转负（src −1721 / bot −671）完成兑现。

**理由**：波次表写于实施规划之前；规划期（Metis M4）按依赖与并行度重排了票据分桶，contract 波（全量切换 + 删除）作为独立波次加入。

**影响**：历史映射澄清；P5 逐波双账以实施计划的 evidence 发布为准；无设计决策变更。

> **修正注（2026-08-22，Errata-8）**：本条"完成兑现"指 W1 删除台账的对冲账闭合于 W6（W6 增量转负）；总账终账实际净 **+2482**，§9.1-5"总账净减或持平"判据**未达成**——终账数字与逐波增量见 **Errata-8**。原文不改动。

### Errata-4: §7 V10 阶段归属——阶段一表格为权威（2026-08-22，实施收官核验——Oracle#4）

**原文**（§7 vs 票据 03）：§7 的阶段一（声明形状，推导前）表格列有 V10（图节点引用存在性）；票据 03 的正文将 V10 归入阶段二。

**修订后**：**阶段一表格为权威**——V10 是声明形状规则，在推导前由 `validate_declaration` 执行（输入面 `GraphAgentReference`，boot 从已加载图 specs 提取）；阶段二 `validate_effective_configs` 只跑 V6/V9。票据 03 的阶段二归属是笔误，已在票据关闭时以 supersession 注记钉正。

**理由**：实现即如此（`scope/validator.py` 两入口的规则分桶）；图节点引用存在性不依赖任何推导产物。

**影响**：规则语义不变；文档一致性修正。

### Errata-5: §1.2 痛点证据的模块计数（2026-08-22，实施收官核验——Metis LOW）

**原文**（§1.2 痛点 3）："`bot/workspace/wiring/` 4 个模块 + `bot/service/pool/` 11 个模块的资源选择与接线全在代码里"（§8 W3 行重复"create_pool 的 11 模块四分类迁移"口径）。

**修订后**：设计基线（4dafb4b）上 `bot/service/pool/` 实为 **7 个文件（6 模块 + `__init__.py`）**，非 11 模块——"11" 是拆分前峰值的陈旧口径；`bot/workspace/wiring/` 确为 4 模块（stack/resources/pool_wiring + `__init__`）。实施后的现状：`pool/` = 8 模块 + `__init__`（factory、declaration、declaration_graphs、communication、pipeline_wiring、assembly_context、agent_factory、pool_construction——胶水已收敛为声明驱动装配）；`wiring/` = stack + resources（`pool_wiring.py` 已随票据 14 删除）。

**理由**：痛点方向成立、量级口径失准；对设计无后果。

**影响**：事实性修正。

### Errata-6: §3.4 Profile 归属——`config/profiles/` 未落地，FW 标准 profile 为代码级常量（2026-08-22，实施审计）

**原文**（§3.4）："归属：`config/profiles/`；FW 预制标准 profile，BIZ 可加自己的（如 bot-standard）。profile 存储随启动加载、进程生命周期持有。"

**修订后**：FW 标准 profile 以**代码级 frozen 常量**落地（`STANDARD_PROFILES`，`scope/profile.py`——五个 toolset preset）；**`config/profiles/` 目录与 BIZ 自定义 profile 的 boot 加载未落地**——shipped 配置只绑定标准 toolset preset（root→full、非根→read_write），目录面零消费者。`ProfileStore` 类型、单层引用拒绝（V7）、深层合并语义均如设计交付；`compile_scope` 的 `profiles: ProfileStore` 参数保持了未来 BIZ store 的加法接入面。跨进程字节稳定（spec-hash 输入面）也是代码级常量的直接收益。

**理由**：票据 06/07 实现审计——目录面无需求方；标准 preset 作为代码常量免去一道启动加载与缓存失效面。

**影响**：§3.4 存储位置一句被取代；解析语义不变。

### Errata-7: ADR-0042/0043 锚点审计表（2026-08-22，票据 19 收官审计）

逐条核验每个"刻意不做"主张与引用代码锚点（2026-08-22，HEAD=936c7b53）。处置列：✓ 保持 = 主张/锚点仍成立；修正于 ADR = 漂移已就地修订进 ADR 活文档。

| # | ADR/位置 | 原文主张（锚点） | 现状 | 处置 |
|---|---|---|---|---|
| 1 | 0042 Decision ¶2 | `ScopeSpec` 树 + 纯 `ScopeTreeValidator` + 纯 `ScopeCompiler` → per-agent `AssemblySpec` | 已交付：`scope/{spec,validator,compiler}.py`；两阶段校验（V1-V11）；编译器字节稳定（spec-hash 输入） | ✓ 保持 |
| 2 | 0042 Decision ¶2 | modex_graph 刻意不复用（声明 vs 执行语义） | 成立；`scope/validator.py` 零 import modex_graph（N3）；modex_graph 全波零改动 | ✓ 保持 |
| 3 | 0042 Decision ¶3 | `MainAgentSpec`/`SubagentSpec`/`main_agent_name` 死亡；根为唯一 in-degree-0 推导 | 已兑现（票据 11 删除；工厂侧 `root_agent_name`；V3 推导根） | ✓ 保持（主张已兑现） |
| 4 | 0042 Decision ¶3 | V6 硬校验消费编译器推导输出 | 已交付：`validate_effective_configs` V6（effective 工具集输入面） | ✓ 保持 |
| 5 | 0042 Decision ¶4 | peer 根对根、v1 同 workspace、声明不带 workspace 硬编码 | 已交付：V5 + `communication/peer_resolution.py`（声明抽取 + 同束实例解析 tree_ref） | ✓ 保持 |
| 6 | 0042 Decision ¶5 | profile 单层引用 + 整列表替换 + 账单可查 | 已交付：`ProfileStore` 构造期拒绝嵌套；`GET /api/scope/bill` 按请求重算（无 boot 缓存） | ✓ 保持（存储位置见 Errata-6） |
| 7 | 0042 Decision ¶6 | O2 源优先级 user > project > entry_points > bundled（反转 first-seen-wins） | 已交付（票据 01：flush 覆盖 + info 日志 + `registration_source` 审计） | ✓ 保持 |
| 8 | 0042 Decision ¶6 | O3 同名产物编译期替换记账（`edit ← aci`） | 已交付：`ToolReplacement`/`AgentProvenance.replacement_of`；账单可见 | ✓ 保持 |
| 9 | 0042 Decision ¶7 | 上下文链三层 frozen 载体；类型即能力边界；吸收 `SubagentInvocationContext` | 已交付：`AgentContext(WorkspaceContext, PoolContext, AssemblyContext)` 菱形（`plugins/assembly/context.py`）；特例类型已删（票据 10） | ✓ 保持 |
| 10 | 0042 Decision ¶8 | "YAML 文件为真源（WebUI 写回——PoolEditor 现有模式）" | **漂移**：写回走 scope 声明编辑器（`PUT /api/scope/declaration`，票据 16）；旧 PoolEditor 的 pool.yml CRUD 面已退役（仅剩只读列举） | 修正于 ADR |
| 11 | 0042 Decision ¶8 | `AssemblySpec` hash + pool generation 计数器预留缝（零消费者） | 已交付：`scope/seam.py`（`spec_hash` + `ScopeGenerationTracker`）；运行时零消费者已验证 | ✓ 保持 |
| 12 | 0042 Decision ¶8 | 动态 scope 创建写回配置文件（未来路径） | 已在 workspace 粒度兑现（票据 17）：WebUI 运行时新建写 `config/scopes/workspaces/<name>.yml` + 复用 boot 路径；泛化 spec store 仍为未来 | ✓ 保持（workspace 粒度兑现） |
| 13 | 0042 Considered Options | "既有泛型 `WorkspaceRegistry[R]` + `ResourceFactory` 缝是未来第二宿主入口" | **漂移**：`WorkspaceRegistry[R]` 已重命名 `ScopeRegistry[R]`（票据 15） | 修正于 ADR |
| 14 | 0042 Consequences | "特化寻址（WorkspaceRegistry LRU / PoolRouter / WorkspacePathResolver）收敛为一条 scope 路径解析" | **锚点漂移**：`ScopeRegistry[R]`（重命名）+ `ScopePath`/`resolve_scope_path`（`workspace/scope_path.py`，替代已删的 `WorkspacePathResolver`）；`PoolRouter` 保留为 session→pool 投递 shell（归属走声明查表 `agent_pool_ownership`，非删除） | 修正于 ADR（兑现结果） |
| 15 | 0042 Consequences | `main_agent_name`/类型分裂/同池 NORMAL peer 概念移除 | 已兑现（票据 11）；已知残留：`AgentCommKind` 枚举 re-export shim 在 ADR-0006 弃用窗口内留存（importers 未清，非本体系票面）；shipped 配置零同池 NORMAL peer | ✓ 保持（残留已记录） |
| 16 | 0042 Consequences | scope-converge §17 骨架决策全部保持；N5 session 级配置延后 | 成立（骨架机器零换装；N13 延后维持） | ✓ 保持 |
| 17 | 0042 Consequences | 会话树机器 = 骨架；会话 ID 格式不动 | 成立（两段制语义见 Errata-1） | ✓ 保持 |
| 18 | 0043 Decision | `BotAgentNode` 是唯一桥 | 成立；前置检查已按票据 08 放松（不存在不报错、照常投递、poller 冷启动物化、描述从编译声明取） | ✓ 保持 |
| 19 | 0043 Decision | modex_graph 零改动 | 成立（全波 `git diff <基线>..HEAD -- src/modex_graph/` 为空） | ✓ 保持 |
| 20 | 0043 Consequences | 声明树与业务图可同画布渲染但永不合并 | 已交付（票据 16：TopologyCanvas 复用 + `SCOPE_NODE_TYPES` 独立常量，图 YAML 校验面不受影响） | ✓ 保持 |

**N 清单（§6）逐条复核**：N1-N15 全部仍成立——N2 缝已预留未激活（`scope/seam.py` 零消费者）；N11 无兼容垫片（旧路删除票 grep clean）；N15 单容器概念（`workspace_layer_present` 替代 `workspace.enabled`，无第二容器）；D1 容量休眠（`max_materialized` 未激活）。

### Errata-8: §9.1-5 净减不变量终账——实际净 +2482，"总账净减或持平"判据未达成（2026-08-22，F1 终验收核验）

**原文**（§9.1-5 / §2 P5 收敛纪律 / 实施计划 Must-have）：逐 wave 删除台账全部兑现，总账净减或持平（上下文链投入在 W3 对冲）；计划纪律进一步要求"豁免仅 plan-W1（上下文链地基一次性投入），plan-W2 末完成对冲，此后每波增量不得为正"。Errata-3 曾记为"net-zero accounting closed at W6……完成兑现"。

**修订后**（真实终账——BASELINE_SHA `4dafb4b` → 收官 HEAD `ead463dc`，`git diff --numstat` 双 pathspec 累计）：

| pathspec（生产 Python） | 增 | 删 | 净 |
|---|---|---|---|
| `:(glob)src/modex_agent/**/*.py` | +4114 | −2390 | **+1724** |
| `:(glob)examples/bot_project/bot/**/*.py` | +2869 | −2111 | **+758** |
| **合计** | **+6983** | **−4501** | **+2482** |

**明文判定：未达成。** §9.1-5 的"总账净减或持平"（P5 收敛纪律、实施计划 Must-have 同款口径）没有兑现；逐波不变量同样未保持（W2-W5 增量均为正，见下表）。六条验收判据中这是唯一未达成项——其余五条（零胶水声明 / split-brain 一致 / 三层树全链路 / V6 孤儿防护 / WebUI）全部交付。

逐波增量（实施计划波次口径，双 pathspec）：

| 波 | 增量 | 内容 |
|---|---|---|
| W1 | +922 | 豁免波——声明基座 + 上下文链一次性投入（票据 01-04 落地段） |
| W2 | +552 | 垂直切片（票据 05） |
| W3 | +801 | ScopeCompiler 汇聚点（票据 06） |
| W4 | +741 | 首个 pool 切换枢纽（票据 07） |
| W5 | +1394 | 07 后波（票据 08/09/10/12/13/14/16/18） |
| W6 | −1928 | contract 波——旧路径全量删除（票据 11/15/17） |
| W7 | 0 | 文档同步（票据 19，纯 .md） |
| **终账** | **+2482** | |

**偏差性质（计划预测失败，而非迁移未完成）**：

1. **删除台账全额兑现**——逐 wave 台账条目 grep 清零（Main/Sub 类型分裂、`main_agent_name`、`tool_preset`、`SubagentInvocationContext`、roster 解析链、`config/pools/`、BIZ wiring 模块、`WorkspacePathResolver`、PoolStore 等；票据 11 AC(b) grep 清单 + 架构守卫 `test_no_legacy_roster_road.py` AST/import 级钉死）。
2. **收敛实质达成**——生产 boot 单路声明驱动（split-brain 家族 05/07/09/10/11 全绿）+ 守卫测试常驻。
3. **净增的构成是新声明子系统的永久面，大于被删胶水（−4501）**——`scope/` 包（~2,350 行，8 模块 + `__init__`：spec / validator / compiler / derivation / defaults / loader / profile / seam）+ 上下文链菱形载体（`plugins/assembly/context.py`）+ 派生通信条目的 FW 工厂（`plugins/defaults/hooks.py` 通信槽）+ scope-path 机器（`ScopePath` / `resolve_scope_path` / `ScopeRegistry` 家族）。它们是新增的框架能力面，不是未迁完的残余。
4. **定性**：P5 的净减预测写于设计期（假设"新面投入 ≤ 旧胶水删除"）；实际两阶段校验器（11 规则）+ 编译器 + 推导核 + 声明编辑/账单 REST 等永久面超出该预测。计划的迁移目标（单路 + 守卫 + 台账删除）全部达成，失败的只是记账判据的量级预测——W6 的 −1928 兑付了对冲账，但从未覆盖 W1-W5 的 +4,410 累计投入。

**理由**：F1 终验收对终账的独立 git 核验；此前 Errata-3 / 票据收官记录将 W6 contract 波转负误读为"总账净减完成兑现"。

**影响**：验收判据 5 拆半判定——删除台账兑现 ✓、总账净减 ✗（如实记录，不为凑平而追补删除）；Errata-3"完成兑现"表述以本条为准（原文保留，末尾已加指向注）；§9.1-5 已加附注；closure-matrix 同步终账记账行。无设计决策变更——收敛方向、删除范围、单路守卫全部维持；净增面即本 SPEC 的交付物本体。

### Errata-9: 声明 overlay 与无池单代理装配缝是已实施体系的加法扩展（2026-08-24，eval-config-convergence 收官同步）

`scope/overlay.py` 增加 loaded declaration → pre-compile declaration 的纯变换面；变换后仍走原有 V1-V11、有效值校验、编译与装配面。`plugins/assembly/single_agent.py` 增加 poolless root-memory-family 单代理装配缝，复用 `assemble_native_agent` 并吸收未启用的 `SubagentAssembleStage` 预留位。

两者均为加法扩展，不推翻 O1-O4、校验分期、编译产物或既有 pool assembly 决策。bot eval 以 overlay 表达组合差异，以 `SingleAgentInfra` 表达类型化基础设施替换；装配后结构保持冻结。

prompt 单源收敛发生在 bot 层：`resolve_declared_root_prompt` 从编译产物的 provider/config 解析生产与 eval 根 prompt，旧 bot resolver 已删除；这不是新的 FW prompt 语义。

架构守卫采用两区精度：装配区禁止直接构造 `ReActAgent`/`AgentContext`，runner/data-injection 区保留计划明确要求的 per-turn `AgentContext` 数据注入；该放行不提供第二条装配路。

**锚点审计**：`src/modex_agent/scope/overlay.py` 与 `src/modex_agent/plugins/assembly/single_agent.py` 均存在；对应 `tests/unit/scope/test_overlay.py` 与 `tests/unit/plugins/test_single_agent.py` 已绿，既有校验/装配回归亦绿。

### Errata-10: EXPERIENCE 补充剂名-合并语义与 overlay tools 追加语义（2026-08-27，glue-tool-roster-convergence 收官同步）

补充剂的贡献集语义按类型分岔：TODO/ACI 保持合并后追加（append-after-merge，实例由 preset 投影预建）；EXPERIENCE 是**名-合并**（name-merge）型——编译器把投影名（`get_supplement_tool_names([ToolSupplement.EXPERIENCE])` → `experience`）注入 `_merge_tools` 的基列表，`+/-` 条目与无前缀整体替换像控制 preset 名一样控制它（整体替换会连带剥除绑定），工具实体由 FW 工厂在装配期从 pool 数据构建（`plugins/defaults/tools.py` `ExperienceToolFactory`，`PoolContext` 能力面，缺供即响亮失败）。补充剂同时贡献 hook：最终工具名册含 experience 且原始声明 hooks 未含 `-experience_review` 时，编译器将 `EXPERIENCE_REVIEW_HOOK_NAME`（`tools/presets.py` 单一权威）后注入 hooks 合并结果（保序去重；minus-wins）。**绑定的后置条件 = 最终工具列表**——`tools: [+experience]` 与补充剂声明等价，overlay 负号条目可在编译期一并剥除工具与注入的 hook。工具溯源按来源分类：补充剂名-合并进入名册的条目记 SUPPLEMENT origin（`ToolEntryProvenance`）；hook 溯源刻意不做（`AgentProvenance.fields` 无 hooks 面，属账单 schema 变更，超出本轮）。

`apply_scope_overlay` 的 agent-tools 应用收敛为**统一追加语义**：声明列表与 overlay 条目直接拼接（声明为 None 时 overlay 条目原样通过），`_merge_tools` 的单次合并拥有全部基名（preset、派生条目、补充剂名）——顺带修复了负号条目对带前缀声明条目静默失效的潜在缺陷（`-x` 匹配不上基列表里的 `+x`，静默丢弃）。无前缀 overlay 条目追加到带前缀声明列表时按混合列表规则作基线注解忽略；无前缀-对-无前缀为字面拼接、允许重复（仓库内 overlay 构造全部为负号条目，无消费者命中）。带前缀 `+/-` overlay 条目作用于无前缀整体（wholesale）声明名册时被显式拒绝（`ValueError`，含空列表 `[]` 的 wholesale"无工具"形态）——拼接会产生混合列表、把整体语义静默翻转为增量并重新引入全部 preset 工具（F2 终审修订：静默能力扩张是最恶劣的失败类，响亮拒绝优于任何静默处置）。

业务侧收口：`send_file_to_user` 成为 bot 插件的 TOOL 槽工厂（`plugins/bot_hooks.py` `SendFileToUserToolFactory`，`SEND_FILE_TO_USER_TOOL_NAME` 常量），按 agent 声明 `tools: [+send_file_to_user]` 启用；`builders.py` 的两条硬编码注册段与 experience 的位置默认面（`PositionDefaults.experience_enabled` 字段 + `pool_config/experience.py` 的 main-agent preset 函数）删除，`ExperienceConfig.enabled` 深绑定到编译名册（`bot/workspace/wiring/stack.py` `declared_assembly_deps`：`EXPERIENCE_TOOL_NAME in root.spec.tools`）。

**锚点审计**：`ToolSupplement.EXPERIENCE` + `EXPERIENCE_REVIEW_HOOK_NAME`（`tools/presets.py`）、FW `ExperienceToolFactory`（`plugins/defaults/tools.py`）、bot `SendFileToUserToolFactory`（`plugins/bot_hooks.py`）、`scope/overlay.py` 追加分支、`scope/compiler.py` 名-合并/后注入段均存在；`tests/unit/scope/test_experience_supplement_binding.py`（8 语义行 + SUPPLEMENT 溯源断言）与 overlay 追加语义回归行已绿；`examples/bot_project/bot/service/builders.py` `_build_tools` 仅剩 KB opt-in 与空基座管理器（Stage 4 在其上注册名册）。
