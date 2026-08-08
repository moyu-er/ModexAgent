# Agent role descriptors + role-contract system prompt provider

## Status

Accepted (2026-07-19; implemented). `AgentRole` enum, `AgentRoleContractProvider`, and `roles` field on agent specs are all shipped.

## Context

`examples/bot_project/` 的 `coder` pool 当前有 7 个 in-process subagent
(planner/worker/reviewer/scout/oracle/delegate/context-builder)，但**编排靠
prompt**：main agent 自行决定派谁、何时派、派几个。日常使用中最常见的失败
模式是 **A1 — 派发决策失误**（如复杂任务一把派给 worker 不先 planner；
reviewer 派得太晚或根本不派；reviewer 报了问题但 main agent 不派 worker 修）。

一种思路是用 `verify-on-stop` hook（agent 想结束 + 无新鲜验证证据时注入
合成消息逼它跑验证）解决"防虚假完成"。但深入分析后发现：

1. **"reviewer 未通过但 orchestrator 仍想结束"** 这个场景下，`SubagentAutoSendHook`
   已经把 reviewer 回报 fold-in 给 orchestrator，信息已在上下文里。再注入一条
   system_reminder 是重复信息——LLM 连 system prompt 里的硬性规则都不遵守，
   再加一条 reminder 它照样可能忽略。注入消息解决不了 A1 根因（LLM 决策错误）。
2. **"派了 worker 但没派 reviewer 就想结束"** 这个场景下，注入消息是新信息，
   但 provider 事前注入"必须派 reviewer"的契约 + prompt 编排决策树已经覆盖。
   如果 LLM 仍不遵守，事后拦截也救不了——根因是模型能力或 prompt 工程，
   不是"信息缺失"。

因此**事后拦截路径（`BeforeEndHook` + `StopVerifier` + 注入消息）全部砍掉**。
本 ADR 只做两件事：(1) 引入 **agent 角色描述符**作为通用元数据；(2) 引入
**`AgentRoleContractProvider`** 基于 roles 注入运行时契约到 system prompt。
编排决策树等 prompt 优化在 `agents/*.md` 重写中单独处理，不属于本 ADR。

同时记录一个**延后待办 D1**：external coding agent 作为 subagent 接入使用。
当前 external（OpenCode/Pi）只能作为 pool 的 main agent，不能作为 subagent。
当 coder pool 的 orchestrator 模式稳定后，D1 成为优先级。

## Decision

### 1. 新增 `AgentRole` 预置常量

```python
# src/modex_agent/core/constants.py
class AgentRole(str, Enum):
    """预置 agent 角色常量。bot 可通过字符串扩展任意自定义角色。"""
    PLANNER = "planner"
    IMPLEMENTER = "implementer"   # 实施角色（worker 的框架层抽象名）
    REVIEWER = "reviewer"         # 验证角色
    SCOUT = "scout"
    ORACLE = "oracle"
    COORDINATOR = "coordinator"   # main agent
    COMMUNICATOR = "communicator"
```

- **类型**：`AgentRole` 是 StrEnum，但配置字段是 `roles: list[str] = []`（**不是**
  `list[AgentRole]`）——允许 bot 完全自定义角色字符串（如 `"office-expert"`），
  框架只对预置常量做特殊处理。
- **预置清单开放**：bot 可自定义任意角色字符串，框架不维护业务角色。

### 2. `roles` 字段加到 agent 描述符层

`roles` 作为通用 agent 元数据，加到三个数据结构：

| 数据结构 | 位置 | 用途 |
|---|---|---|
| `AgentConfig` | `src/modex_agent/ioc/configs/` | main agent 配置，从 `config/bot_config.yml` 加载 |
| `AgentTemplateSpec` | `src/modex_agent/multi_agent/template.py` | subagent 配置，从 `config/pools/<pool>/pool.yml` 加载 |
| `AgentDescriptor` | `src/modex_agent/multi_agent/descriptor.py` | 运行时描述符，main + subagent 共用 |

- **字段**：`roles: list[str] = []`（默认空，不假设角色）
- **透传链**：`AgentConfig`/`AgentTemplateSpec.roles` → `AgentDescriptor.roles` →
  运行时消费者（`AgentRoleContractProvider` 及未来扩展）
- **`AgentDescriptor.roles` 不参与 equality/hash**：roles 是元数据，不影响 agent
  身份（pool 注册去重不受影响）。

### 3. 新增 `AgentRoleContractProvider(SystemPromptProvider)`

注入运行时契约到 system prompt，基于当前 agent 的 `roles`。

- **挂载点**：`SystemPromptPipeline` 的 provider 链（main + subagent 共用路径，
  参考 `MemorySystemContextManager.load()`）。
- **位置**：在 `ExperienceProvider`/`SkillProvider` 等业务 provider 之后，保证
  契约文本出现在 system prompt 末尾（优先级高）。
- **注入逻辑**：基于 `roles` 列表判断当前 agent 的角色，注入对应的契约文本：
  - `REVIEWER` → "你是验证角色，最终回复必须包含
    `<verification status="passed|failed" reason="..."/>` 标记"
  - `IMPLEMENTER` → "你是实施角色，代码修改后必须跑验证命令或解释为何不能跑"
  - `COORDINATOR` → "验证角色回报会包含 `<verification status="..."/>` 标记，
    未通过时你必须派实施角色修复，不得直接结束"
  - 其他预置角色（`PLANNER`/`SCOUT`/`ORACLE`/`COMMUNICATOR`）→ 各自对应职责契约
  - 自定义角色字符串（如 `"office-expert"`）→ 不注入（框架不认识）
- **不依赖 `agents/*.md`**：provider 独立注入，不读取也不依赖 .md 文件内容。
  .md 文件继续承载角色身份+通用能力描述，provider 承载运行时契约。
  两者职责分离，避免双源维护。
- **缓存友好**：`roles` 在 agent 实例生命周期内不变，provider 输出字节稳定，
  不会破坏 system prompt 缓存。

### 4. 前端配置

`examples/bot_project/webui/` 的 `PoolEditor`：

- `MainAgentFields` 和 `SubagentCard` 加"角色"多选下拉框
- 下拉框展示 `AgentRole` 预置清单（带 i18n 标签）+ "自定义..."输入框
- 后端 save 端点接受 `list[str]`，预置清单只是 UI sugaring
- TS 类型加 `roles?: string[]` 字段

### 5. bot wiring（pool.yml 改造）

`examples/bot_project/config/pools/coder/pool.yml` 显式声明角色：

```yaml
main_agent_name: orchestrator   # 原 coder main agent 改名
subagents:
  - agent_name: planner
    roles: [planner]
  - agent_name: worker          # 保留 worker 名字，但角色标 implementer
    roles: [implementer]
  - agent_name: reviewer
    roles: [reviewer]
  - agent_name: scout
    roles: [scout]
  - agent_name: oracle
    roles: [oracle]
  # 砍掉 delegate（编排归 orchestrator）+ context-builder（scout 覆盖）
```

### 6. prompt 优化（独立于本 ADR，但顺带执行）

`agents/*.md` 重写，承载角色身份+编排决策树：

- `coder.md` → `orchestrator.md`：改名为编排者定位，加编排决策树
  （Step 1-5：是否涉及代码修改 → 是否 well-specified → 上下文是否 clear →
  代码修改后必须派 reviewer → reviewer 未通过必须派 worker 修，max 2 cycles）
- `reviewer.md`：保持现有内容（`<verification status="..."/>` 格式由 provider
  注入，.md 不重复）
- `worker.md`：强化"代码修改后必须验证"的硬性要求
- `delegate.md` / `context-builder.md`：废弃（pool.yml 不再引用）

**编排决策树归属**：放 `orchestrator.md`（静态方法论），不放 provider
（provider 只注入短契约，决策树太长）。

### 7. 延后待办 D1：external coding agent as subagent

**状态**：Deferred (重要能力，未排期)。

**当前架构**：ADR-0022 集成 external coding agent（Pi/OpenCode）作为**独立
pool 的 main agent**（`pool_opencode`）。orchestrator 要委托编码工作给 OpenCode，
当前只能通过 peer 通信（`send_to_agent` 到 `opencode` pool）——但 peer 通信
没有 `SubagentAutoSendHook` 自动回报，体验不如 subagent。

**D1 能力**：扩展 subagent materialize 路径，允许 `AgentTemplate` 声明
`execution_strategy: external` + `provider_kind: opencode`。materialize
时构造 `ExternalAgent` 而非 `ReActAgent`，对父 agent 而言与其他
subagent 无区别（同样的 `send_to_agent` 接口、`SubagentAutoSendHook` 回报、
`invocation_id` 续接语义）。

**为什么延后**：
- 触及 `SubagentDispatchStrategy` / `AgentTemplate.materialize`（新 strategy 分支）
- 触及 subagent 生命周期（外部 subagent 拥有 workdir/CLI 进程/provider session
  等资源，session 驱逐要 reap 这些；session 续接要复用 provider session）
- 触及 stop-event 翻译（external backend 的 stop 事件 → ModexAgent StopReason）

**何时重启**：当 orchestrator 模式（react main agent + planner/reviewer/scout/
oracle subagents）稳定，且下一个瓶颈是"编码委托质量/成本"时，D1 成为优先级。
过渡期间 orchestrator 通过 peer 通信委托 opencode pool 是可接受的。

**优先级调整**：6a（辅助模型路由）在 D1 之后**大幅降低优先级**——因为 D1
落地后，编码工作主要由 external subagent 承担，ModexAgent 内部 subagent
（planner/reviewer/scout/oracle）的 LLM 调用量相对降低，辅助模型路由的
收益边际递减。

## Considered Options

### 注入机制：provider vs hook

1. **`SystemPromptProvider`（chosen）**：符合现有 system 注入体系
   （`SystemPromptPipeline` + N 个 provider），缓存/压缩/截断处理一致。
2. `BeforeTurnHook` 注入 `ctx.system_prompt`：hook 是生命周期观察者，注入
   system 是 prompt 构造职责，语义错位。
3. 硬编码到 `agents/*.md`：零框架改动，但契约散在多个 .md 文件里，没有
   单一来源，且 .md 是静态身份文档，运行时契约（如 `<verification status="..."/>`
   格式）应该是框架提供的一致约定。

### 事后拦截：保留 vs 砍掉

1. **砍掉（chosen）**：`SubagentAutoSendHook` 已覆盖 reviewer 回报路径，
   注入消息是重复信息；LLM 不遵守 prompt 时事后拦截也救不了。
2. 保留 `BeforeEndHook` + `StopVerifier`：能检测"没派 reviewer 就结束"，
   但注入消息价值有限（见 Context 部分），且引入新 HookPoint + ABC + verdict
   类型 + `system_reminder` role + XML tag，复杂度高收益低。

### 角色配置形态

1. **预置常量 + 自定义字符串（chosen）**：`AgentRole` StrEnum 提供常见值，
   配置字段用 `list[str]` 接受任意值。前端有预置清单 + 自定义入口。
2. 封闭 StrEnum（`list[AgentRole]`）：类型安全但框架外角色无法添加。
3. 纯字符串无预置：最灵活但前端无清单，体验差。

## Consequences

### 正面

- **框架通用**：`AgentRole` + `roles` 字段是通用元数据，不假设任何 bot 具体角色名。
- **注入统一**：契约注入走 `SystemPromptProvider`，和现有 system 注入体系一致，
  缓存/压缩/截断处理统一。
- **单一来源**：`<verification status="..."/>` 格式契约由 provider 注入，
  `agents/*.md` 不重复，避免双源维护。
- **开箱即用**：bot 配置 `roles: [reviewer]` 即可启用契约注入，零代码。
- **可扩展**：bot 可自定义角色字符串，框架预置清单不封闭。
- **前端可配**：PoolEditor 加多选下拉框，用户在 UI 上选角色，回写 pool.yml。
- **方案简化**：砍掉事后拦截后，框架改动只剩 `AgentRole` + `roles` 字段 +
  一个新 provider，复杂度低、风险低。

### 负面

- **`AgentDescriptor` 加字段**：所有 agent 实例多一个 `roles: list[str]` 字段
  （默认 `[]`）。轻微内存开销，可忽略。
- **A1 根因未完全根治**：本 ADR 是事前指导（prompt 增强），不是事后拦截。
  如果 LLM 仍不遵守 prompt 规则，本方案无法强制纠正。但事后拦截也救不了
  （见 Considered Options），所以这是可接受的局限。
- **前端三处改动**：TS 类型 + 表单组件 + save 端点，工作量中等。
- **契约靠 prompt 约束 LLM 输出**：reviewer 可能不按
  `<verification status="..."/>` 格式输出。orchestrator 解析失败时按
  "未通过"处理（保守策略），可能误报。可观察后再决定是否升级为
  `SubagentAutoSendHook` 加结构化字段（框架层强制）。

### 风险

- **D1 延后期间 orchestrator 委托编码靠 peer 通信**：体验不如 subagent（无
  自动回报），但可接受。D1 落地后升级为 subagent 自动委托。

## Open questions

- `roles` 字段在 `AgentConfig`/`AgentTemplateSpec` 的 Pydantic 验证是否要
  限制预置角色字符串的拼写（如 warn 未知角色）？倾向不限制——自定义角色
  是合法的，框架不该对未知角色报警。
- `AgentRoleContractProvider` 的注入顺序（在 provider 链中的位置）：当前
  选择"在业务 provider 之后"，但具体 priority 值需在实现时确认现有
  provider 的 priority 体系。
- `AgentRoleContractProvider` 是否应该支持 bot 自定义契约文本（如 bot 给
  `"office-expert"` 角色配自己的契约）？当前不支持（框架只识别预置角色）。
  如果未来需要，可扩展为 provider 构造参数接受 `custom_contracts: dict[str, str]`。

## Related

- ADR-0015（subagent materialize 路径，本 ADR 不改其语义）
- ADR-0019（cross-pool peer，本 ADR 不触及 peer 通信）
- ADR-0022（external coding agent，D1 待办与其相关）
- `docs/design/external-agent-integration/deferred.md`（D1 详细记录）
- `agents/orchestrator.md`（待重写，承载编排决策树，独立于本 ADR）
