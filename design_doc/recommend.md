我的推荐可以概括成一句话：

**短期不要继续横向扩功能，而是做一次“运行时与记忆系统收口”：确定唯一主链路、唯一记忆入口、唯一审批/控制路径、唯一示例推荐路径，然后把旧 adapter、重复 config、过渡 API 逐步下线。**

下面我按优先级完整梳理。

---

# 0. 总体目标

当前 ModexAgent 已经有很多能力：Agent、Tool、Memory、Input/Output Adapter、Pipeline、Hook、Interceptor、Control、Plugin、多 Agent、安全、审批等。README 里也明确把这些作为核心能力列出，并且说明项目仍处在早期开发阶段，接口和文档会继续调整。([GitHub][1])

所以现在的关键不是“还能加什么”，而是：

```text
减少多套实现
减少隐式路径
减少兼容包袱
明确推荐路径
把设计文档、代码实现、bot 示例统一起来
```

我建议短期目标定成：

> **把 ModexAgent 从“框架工作台”收口成“有清晰主干的 alpha 框架”。**

---

# 1. 第一优先级：统一运行时主链路

## 推荐主链路

我建议最终主链路固定成：

```text
InputAdapter
  → AgentPipeline
      → 构造 RuntimeServices
      → ContextManager / MemorySystemContextManager
      → ReActAgent
          → ReActGraph
              → StartNode
              → LLMNode
              → ToolNode
              → EndNode
          → InterceptorChain
          → ControlChannel
          → ApprovalPolicy
      → OutputAdapter
      → MemorySystem persist
```

当前 `docs/current-runtime.md` 已经非常接近这个方向：文档明确说 ReAct 是 graph-based，`ReActAgent.run()` 会委托给 `ReActGraph`，节点包括 `StartNode`、`LLMNode`、`ToolNode`、`EndNode`；并且 full mode 下显式接入 hooks、interceptors、control、approval、runtime state。([GitHub][2])

## 我的建议

**Pipeline 不应该拥有 turn 内部语义。**

Pipeline 应该只负责：

```text
平台输入输出
session 定位
runtime services 组装
context load/save
调用 agent.run()
错误兜底
```

ReAct 应该负责：

```text
turn
iteration
LLM call
tool call
approval suspend/resume
cancel/end metadata
resume node
```

这和当前 runtime 文档里的设计方向一致：Pipeline 组装 runtime services 并处理平台 I/O，ReAct 拥有 turn、iteration、LLM、tool、approval、resume 边界，Hook 观察/转换 payload，Interceptor 包裹执行边界，Control 处理运行时命令。([GitHub][2])

---

# 2. Hook / Interceptor / Control / Approval 的收口建议

这是最需要收口的地方。

## 2.1 我推荐的职责划分

| 机制          | 推荐定位                                    | 不应该做什么                      |
| ----------- | --------------------------------------- | --------------------------- |
| Hook        | 生命周期事件，观察或轻量修改 payload                  | 不包裹执行、不做长阻塞、不做审批            |
| Interceptor | 执行边界 wrapper，例如 turn、iteration、LLM、tool | 不持久保存业务记忆、不替代 Control       |
| Control     | 运行时命令面，例如 cancel、inject、steer、resume    | 不当作普通 message history       |
| Approval    | 一种策略，不是独立运行时体系                          | 不再发展成第二套 control/checkpoint |

当前文档也明确区分了这些边界：Hook 是生命周期扩展点，不应该 wrap execution；Interceptor 是 onion-chain wrapper，适合 timeout、approval policy enforcement、result transformation、control drains；Control 是运行时命令平面。([GitHub][2])

## 2.2 审批机制应该怎么放

我的推荐是：

```text
ApprovalPolicy
  → 由 Interceptor 或 ToolNode 调用
  → 需要暂停时写 RuntimeStateStore
  → 通过 ControlChannel 接收 approve/deny/resume
```

也就是说，审批不是一套独立大系统，而是：

```text
approval = policy + suspend/resume + runtime state + control command
```

不要保留多处 approval config、多处 approval workspace、多处 deny/resume 处理逻辑。runtime 文档里也提到，bot_project 不再携带冗余默认 `approval:` 配置，approval wiring 应该来自 runtime construction 和显式 policy object。([GitHub][2])

## 2.3 clean / full 模式建议

当前 runtime 文档已经定义了 `clean` 和 `full`：clean mode 应该移除 hooks、approval、interceptor/control services、runtime state store、injection queues；full mode 显式接入这些扩展系统。([GitHub][2])

我建议保留这个设计，并强制执行：

```text
clean mode:
  只允许 ReActGraph + ToolManager + LLMProvider + ContextManager

full mode:
  允许 Hook + Interceptor + Control + Approval + RuntimeStateStore
```

这可以避免代码里到处写：

```python
if runtime.approval_workspace:
if runtime.control_channel:
if runtime.interceptor_chain:
```

而是 turn 入口统一 sanitize。

---

# 3. MemorySystem 的推荐路线

你特别提到 memorySystem 有旧 adapter 等问题，这块我建议要明确做一次迁移收口。

## 3.1 记忆系统应该保留一个主入口

我推荐唯一主入口是：

```python
MemorySystem
MemorySystemContextManager
create_memory_system()
```

文档里已经把 `MemorySystem` 定义为统一入口，三层分别是 Short-Term Memory、History Archive、Long-Term Memory。([GitHub][3])

所以其他旧 adapter、旧 context manager、旧 memory bridge，如果只是兼容历史结构，建议分三类处理：

| 类型                                              | 处理建议                                      |
| ----------------------------------------------- | ----------------------------------------- |
| `MemorySystemContextManager`                    | 保留，作为生产推荐路径                               |
| `InMemoryContextManager` / `FileContextManager` | 保留，作为 dev/test/minimal 示例                 |
| 旧 Memory adapter / legacy bridge                | 标记 deprecated，迁移到 compatibility 目录，下一阶段删除 |

核心原则是：

```text
业务代码不直接碰 memory layer
业务代码只依赖 ContextManager
ContextManager 内部可以用 MemorySystem
```

也就是：

```text
AgentPipeline → ContextManager protocol → MemorySystemContextManager → MemorySystem
```

不要让 Pipeline、Agent、bot_project 同时直接访问 MemorySystem 内部 layer。

---

## 3.2 Working Memory 已移除，代码里不要再保留影子概念

文档明确写了：Working Memory 层已在 v0.3.0+ 中移除，所有消息直接写入 Short-Term Memory，由 `compression_mode` 的 `cursor/delete` 控制可见性。([GitHub][4])

因此建议全仓库搜索并处理这些关键词：

```text
WorkingMemory
working_memory
working memory
visible memory
active memory
memory adapter
legacy memory
```

处理规则：

| 情况                               | 建议                                            |
| -------------------------------- | --------------------------------------------- |
| 文档仍描述 Working Memory             | 删除或改为迁移说明                                     |
| 代码仍有 WorkingMemory class         | 如果无引用，删除；如果有引用，改接 ShortTermMemory             |
| bot_project 配置仍出现 working_memory | 删除配置项                                         |
| 测试仍覆盖旧 working memory            | 改成 compression cursor/delete 测试               |
| 旧 adapter 只为兼容 working memory    | 移入 `framework/compat/` 并打 deprecation warning |

---

## 3.3 MemorySystem 三层建议重新命名为更稳定的概念

当前文档叫三层：

```text
Short-Term Memory
History Archive
Long-Term Memory
```

这是可以的。([GitHub][3])

但代码 API 上建议统一成：

```text
session memory    # 当前会话上下文
archive memory    # 历史摘要/归档
knowledge memory  # 长期知识/用户画像
```

原因是：

* `short_term` 更像策略描述；
* `session` 是作用域描述，更稳定；
* `long_term` 容易和 vector memory、profile memory、knowledge memory 混在一起；
* `knowledge` 更适合 `SOUL.md / USER.md / MEMORY.md` 这类结构化文件。

推荐内部命名：

```python
MemoryLayer.SESSION
MemoryLayer.ARCHIVE
MemoryLayer.KNOWLEDGE
```

外部文档可以继续解释：

```text
Session = short-term conversation memory
Archive = historical summaries
Knowledge = long-term user/agent knowledge
```

---

## 3.4 Memory 不要和 RuntimeStateStore 混淆

这是非常关键的一点。

文档最后已经提醒：Memory docs are separate from ReAct runtime persistence；suspend/resume 和 runtime state 应该使用 `RuntimeStateStore` 命名，不要和 long-term 或 conversation memory providers 混淆。([GitHub][4])

我的建议是强制区分：

| 类型                | 用途              | 保存什么                                           | 是否给 LLM 看 |
| ----------------- | --------------- | ---------------------------------------------- | --------- |
| MemorySystem      | 对话/用户/知识记忆      | messages、summary、profile、knowledge             | 是         |
| RuntimeStateStore | 运行时恢复           | suspended tool call、resume node、approval state | 否         |
| CheckpointStore   | 通用控制 checkpoint | control/checkpoint primitives                  | 通常否       |

也就是说：

```text
MemorySystem 不是 checkpoint
RuntimeStateStore 不是 memory
Approval state 不进 long-term memory
Control command 不进 user history，除非明确需要审计
```

当前 runtime 文档也说 `RuntimeStateStore`、`JsonFileRuntimeStateStore`、`NoOpRuntimeStateStore` 是现在推荐的 runtime-state 命名，旧 `CheckpointStore` 名字只是兼容。([GitHub][2])

所以我建议：

```text
P0: 新代码只用 RuntimeStateStore
P1: CheckpointStore 只保留在 control/checkpoint 泛用模块
P2: ReAct/runtime 集成代码禁止再 import CheckpointStore
```

---

## 3.5 MemoryProvider 插件机制要降级为扩展，不要成为核心路径

MemorySystem 文档支持通过 `MemoryProvider` 插件扩展 add/search/prefetch/on_pre_compress/shutdown。([GitHub][3])

我建议：

```text
MemoryProvider = optional external memory/search provider
MemoryLayer = built-in canonical storage layer
```

不要让 provider 反过来成为核心记忆系统的主干，否则会产生两套写入链路：

```text
MemorySystem.add_message()
Provider.add()
ContextManager.save()
Plugin hook save()
```

最终应该是：

```text
ContextManager.save()
  → MemorySystem.add_messages()
      → built-in layers write
      → optional providers receive event/copy
```

Provider 可以搜索、补充、预取，但不应该决定 session history 的 canonical state。

---

# 4. ContextManager / MemorySystemContextManager 的推荐关系

当前 core 文档里列出了 `ContextManager` 继承层次，包括：

```text
InMemoryContextManager
FileContextManager
EphemeralContextManager
MemorySystemContextManager
```

并明确说 `MemorySystemContextManager` 是适配 MemorySystem 的生产用实现。([GitHub][5])

我建议保留这条抽象，但做强约束：

## 推荐分层

```text
ContextManager:
  面向 AgentPipeline 的上下文协议

MemorySystem:
  面向记忆系统内部的存储、压缩、归档、长期记忆协议

MemorySystemContextManager:
  二者之间唯一桥接
```

## 禁止模式

```python
# 不推荐
pipeline.memory_system.add_message(...)
agent.memory_system.get_long_term(...)
bot_service.memory_layer.session.append(...)
```

## 推荐模式

```python
state = await context_manager.load(session_id)
...
await context_manager.save(session_id, user_message, assistant_result, metadata)
```

这样后面无论是 FileContextManager、MemorySystemContextManager、测试用 InMemoryContextManager，都不会影响 Agent/Pipeline 主逻辑。

---

# 5. ToolManager 与 Tool 执行治理

ToolManager 现在是比较成熟的一块。core 文档显示它支持 `execute`、`execute_batch`、schema、timeout、execution mode、retry、enabled 等配置。([GitHub][5])

我的建议不是重写 ToolManager，而是把 tool 治理边界定清楚。

## 5.1 ToolManager 只负责执行，不负责审批

推荐：

```text
ToolManager:
  registry
  validation
  timeout
  retry
  execution mode
  result normalization

Interceptor / ApprovalPolicy:
  是否允许执行
  是否需要审批
  是否脱敏
  是否限流
  是否截断结果
```

也就是说：

```text
是否执行工具 = policy/interceptor
如何执行工具 = ToolManager
工具结果如何给 LLM = ReAct ToolNode
```

不要把 approval 逻辑塞进 ToolManager，否则 ToolManager 会被平台/用户/权限/审批状态污染。

---

## 5.2 Tool timeout 应留在 ReAct tool execution path

runtime 文档里已经写了：`TurnTimeoutInterceptor` 和 `ToolTimeoutInterceptor` 当前不是 bot 默认 runtime chain 的一部分；Tool timeout 属于 ReAct tool execution path 和 runtime safety policy；turn timeout 只有当 ReAct runtime 完全拥有 turn/iteration interceptor scope 后再加入。([GitHub][2])

我同意这个方向。

建议：

```text
工具级 timeout:
  ToolConfig.timeout + ReAct ToolNode safety policy

回合级 timeout:
  TurnTimeoutInterceptor，但等 turn scope 完全归 ReAct 后再默认启用

平台级 timeout:
  Pipeline / service 外层兜底，不影响 runtime state
```

---

# 6. Adapter 体系建议

这里要区分两类 adapter：

```text
I/O Adapter
Memory adapter
```

## 6.1 I/O Adapter：保留，方向是对的

core 文档里 `InputAdapter` / `OutputAdapter` / `PlatformAdapter` 的抽象比较清楚，而且已经引入 `StreamingMode` 替代旧的 `supports_streaming` 布尔属性。([GitHub][5])

这个方向应该继续：

```text
InputAdapter: 平台输入转 InputMessage
OutputAdapter: OutputMessage 转平台输出
PlatformAdapter: 平台能力抽象
```

建议做的收口：

| 项                    | 建议                                                       |
| -------------------- | -------------------------------------------------------- |
| `supports_streaming` | 全部删除，统一用 `streaming_mode`                                |
| 平台专有字段               | 全部放进 `metadata`，不要污染 InputMessage 主字段                    |
| 附件处理                 | 保持 `attachments` 字段，媒体 base64 不直接进 memory                |
| 发送能力                 | 用 `StreamingMode.NATIVE / PSEUDO / NONE` 表达，不再散落 bool 判断 |

---

## 6.2 Memory adapter：旧的要退役

如果当前还有旧 Memory adapter，我建议按下面规则处理：

| 旧 adapter 类型                       | 推荐处理                                        |
| ---------------------------------- | ------------------------------------------- |
| 旧 `ContextManager → list[dict]` 适配 | 保留自动转换，但不再作为公开推荐 API                        |
| 旧 `MemorySystemAdapter`            | 合并进 `MemorySystemContextManager` 或移入 compat |
| 旧 provider-style memory 主链路        | 改成 MemoryProvider 插件                        |
| 旧 working memory adapter           | 删除或 deprecated                              |
| bot_project 内部 memory glue         | 尽量改为直接构造 `MemorySystemContextManager`       |

`ContextState.history` 现在已经是 `MessageHistory` 协议对象，旧 `list` 会自动转换为 `ListMessageHistory`，这本身就是一个兼容层。([GitHub][5])
所以再保留更多 adapter 层没有太大必要。

我的建议是新增一个明确目录：

```text
framework/compat/
  memory_legacy.py
  context_legacy.py
  adapter_legacy.py
```

里面所有类都加：

```python
warnings.warn(
    "... is deprecated; use MemorySystemContextManager instead",
    DeprecationWarning,
    stacklevel=2,
)
```

然后设定删除时间，例如：

```text
v0.4: deprecated
v0.5: removed from docs
v0.6: removed from code
```

---

# 7. bot_project 的推荐定位

`examples/bot_project` 现在覆盖 QQ Bot、LLM 对话、工具、MCP、四层记忆、多 Agent、插件等能力。([GitHub][6])
它还支持 Pipeline 和 Pool 两种模式：Pipeline 适合单 Agent 长运行服务，Pool 适合多 Agent 常驻协作和动态任务分发。([GitHub][6])

我的建议是：

## 7.1 bot_project 保留为“集成示例”，不要作为唯一入门示例

它太复杂，不适合当框架最小学习路径。

建议示例矩阵改成：

```text
examples/
  minimal_cli_react/
  minimal_tool_call/
  minimal_memory_system/
  minimal_approval_control/
  minimal_interceptor/
  bot_project/
  multi_agent_pool/
```

其中：

| 示例                         | 目的                               |
| -------------------------- | -------------------------------- |
| `minimal_cli_react`        | 20～50 行跑通 Agent                  |
| `minimal_tool_call`        | 展示 ToolManager 和 ReAct tool call |
| `minimal_memory_system`    | 展示 MemorySystemContextManager    |
| `minimal_approval_control` | 展示审批、暂停、恢复                       |
| `minimal_interceptor`      | 展示工具结果截断、日志、限流                   |
| `bot_project`              | 作为完整集成参考                         |
| `multi_agent_pool`         | 单独展示 AgentPool，不和 QQ 强绑定         |

## 7.2 bot_project 默认模式建议改回 pipeline

现在文档里说 Pool 模式是默认，适合多 Agent 常驻池。([GitHub][6])

但对一个框架示例来说，我建议默认改成：

```text
默认 pipeline
显式 --mode pool 才进入多 Agent
```

理由：

* Pipeline 是主干，更适合验证基础链路；
* Pool 依赖 broker、message bus、inbox、persistent agent，复杂度更高；
* 新用户先看 Pool 会误以为框架必须这么重。

---

# 8. Governance / Safety / Interceptor 的关系

bot_project README 里提到治理系统会做 ToolChainRepair、Microcompact、TokenBudget 等。([GitHub][6])

这里也容易和 Memory compression、Interceptor、ToolManager 混起来。

我建议这样切：

| 能力                    | 推荐归属                                   |
| --------------------- | -------------------------------------- |
| token budget          | ContextGovernance / ContextBuilder     |
| message repair        | ContextGovernance                      |
| tool result limit     | Interceptor                            |
| tool call pair repair | Memory compression 或 ContextGovernance |
| approval policy       | ApprovalPolicy + Interceptor           |
| sandbox policy        | SecurityPolicy + Tool execution        |
| runtime cancel/inject | Control                                |

不要让一个 `Governance` 变成第二个 Pipeline。

---

# 9. Plugin 系统建议

Plugin 应该是扩展机制，不应影响核心链路的可理解性。

建议插件只允许注册这些类型：

```text
ToolProvider
MemoryProvider
HookProvider
InterceptorProvider
SkillProvider
```

但主链路必须能在无插件状态下完整运行：

```text
LLM + ReActAgent + ToolManager + ContextManager + Pipeline
```

Plugin 初始化顺序建议固定：

```text
load config
load plugin manifest
register tools/providers/hooks/interceptors
freeze runtime registry
start pipeline
```

不要让插件在 turn 中途修改核心 registry，除非显式支持 hot reload。

---

# 10. Superpower / Skill / Agent 能力建议

你之前提到 superpower 设计文档、hook、interceptor、control 还没有完整集成。我建议不要把 superpower 做成又一套独立运行时。

推荐定位：

```text
Superpower = 一组可组合的能力声明
Skill = prompt / tool / policy / memory prefetch 的组合
Plugin = 分发/安装机制
```

也就是说：

```text
superpower 不直接执行
superpower 不直接持有 runtime state
superpower 不直接控制 tool call
```

它应该编译/装配成：

```text
tools
system prompt fragments
hooks
interceptors
memory providers
approval policies
```

这样不会和 Hook/Interceptor/Control 抢职责。

---

# 11. 具体重构路线

我建议分四个阶段。

---

## Phase 1：冻结主链路，禁止继续扩散

目标：明确唯一推荐路径。

### 要做

1. 写一个 `docs/runtime-contract.md` 或更新 `current-runtime.md`。
2. 明确：

   ```text
   Pipeline owns I/O and runtime assembly
   ReAct owns turn execution
   Interceptor owns wrappers
   Control owns commands
   Approval is policy + suspend/resume
   MemorySystem owns conversation/user memory
   RuntimeStateStore owns suspend/resume state
   ```
3. 新代码禁止直接使用旧 adapter。
4. bot_project 先不新增能力，只改 wiring。

### 验收标准

```text
pytest tests/unit
pytest tests/integration
minimal pipeline bot 能跑
minimal clean ReAct 能跑
full runtime ReAct 能跑
```

---

## Phase 2：MemorySystem 收口

目标：干掉旧 memory adapter 和 working memory 残留。

### 要做

1. 全仓库 grep：

   ```text
   working_memory
   WorkingMemory
   MemoryAdapter
   legacy memory
   CheckpointStore in memory context
   ```
2. `MemorySystemContextManager` 作为唯一生产推荐路径。
3. 旧 adapter 移到 `framework/compat/`。
4. 文档统一：

   ```text
   Session / Archive / Knowledge
   ```
5. 测试补齐：

   ```text
   add_message
   get_history
   compression cursor
   compression delete
   tool call pair compression
   long-term get/update
   provider search/prefetch
   ```

### 验收标准

```text
bot_project 不直接操作 MemoryLayer
Pipeline 不直接操作 MemorySystem 内部
WorkingMemory 不再出现在非兼容代码中
RuntimeStateStore 不写入 MemorySystem
```

---

## Phase 3：审批/控制/拦截器收口

目标：审批只有一套路径。

### 要做

1. 明确 `ApprovalPolicy` 接口：

   ```python
   async def check_tool_call(ctx, tool_call) -> ApprovalDecision
   ```
2. `ToolNode` 遇到 `requires_approval`：

   ```text
   save RuntimeStateStore
   emit approval required
   suspend turn
   ```
3. Control 接收：

   ```text
   approve
   deny
   resume
   cancel
   ```
4. Interceptor 做：

   ```text
   tool result limit
   audit
   policy wrapper
   control drain at safe boundary
   ```

### 验收标准

```text
同一个 tool call 不会被两套 approval 重复拦截
deny 后不会写入 assistant final answer 伪成功
resume 后从 ToolNode 继续，而不是重跑整个 turn
cancel 后 turn_cancelled = true
```

---

## Phase 4：示例和 API 清理

目标：让用户知道该怎么用。

### 要做

1. 新增最小示例。
2. README 改成：

   ```text
   5 分钟 minimal
   15 分钟 tool call
   30 分钟 memory
   bot_project 完整集成
   ```
3. bot_project 默认 pipeline。
4. Pool 示例单独拆出来。
5. Python 版本统一。
6. 所有 deprecated API 加 warning。

### 验收标准

```text
新用户不读 bot_project 也能跑通最小 Agent
bot_project README 不再承担所有概念解释
文档里没有互相矛盾的运行时描述
```

---

# 12. 推荐删除 / 保留 / 重构清单

## 应该保留

```text
ReActGraph
ReActAgent
ToolManager
MemorySystem
MemorySystemContextManager
InputAdapter / OutputAdapter
StreamingMode
InterceptorChain
ControlChannel
RuntimeStateStore
bot_project
```

## 应该重构

```text
AgentPipeline
Approval wiring
Memory adapter compatibility
ContextManager 与 MemorySystem 的边界
bot_project builders
Governance 与 compression 的边界
Plugin registration lifecycle
```

## 应该 deprecated

```text
WorkingMemory 相关概念
旧 Memory adapter
旧 supports_streaming
ReAct/runtime 内使用 CheckpointStore 命名
重复 approval config
Pipeline 内部直接处理过深的 turn semantics
```

## 可以暂缓

```text
DreamEngine 自动长期整理
复杂多 Agent shared memory
active-operation registry
turn timeout 默认启用
复杂 sandbox 策略
热加载 plugin
```

---

# 13. 我建议你现在立刻做的事情

按收益排序：

## 第一件：写一份“唯一主链路 contract”

文件可以叫：

```text
docs/runtime-contract.md
```

内容只写：

```text
谁拥有输入输出
谁拥有 turn
谁拥有 tool execution
谁拥有 approval
谁拥有 control
谁拥有 memory
谁拥有 runtime state
```

这份文档会成为你后面删重复实现的依据。

---

## 第二件：整理 MemorySystem 旧 adapter

直接建一个 issue/checklist：

```text
MemorySystem 收口
- [ ] 搜索 WorkingMemory 残留
- [ ] 搜索 MemoryAdapter 残留
- [ ] 确认 MemorySystemContextManager 是唯一生产推荐路径
- [ ] 旧 adapter 移入 framework/compat
- [ ] 文档删除旧路径
- [ ] bot_project 改成只依赖 ContextManager
- [ ] 测试 cursor/delete compression
```

---

## 第三件：审批机制只保留一套

明确：

```text
ApprovalPolicy + RuntimeStateStore + ControlCommand
```

然后删除：

```text
重复 approval config
重复 approval workspace
重复 deny/resume handler
```

---

## 第四件：bot_project 降低默认复杂度

把默认模式改成 pipeline，Pool 显式启用。

```bash
python bot_service.py --mode pipeline
python bot_service.py --mode pool
```

README 里说明：

```text
pipeline 是推荐入门模式
pool 是多 Agent 常驻模式
```

---

# 14. 最终推荐结论

我建议 ModexAgent 短期规划定为：

```text
v0.3.x: runtime + memory 收口版本
```

核心目标：

1. **MemorySystem 成为唯一记忆入口。**
2. **MemorySystemContextManager 成为唯一生产 ContextManager。**
3. **RuntimeStateStore 与 MemorySystem 严格分离。**
4. **Hook / Interceptor / Control / Approval 明确职责。**
5. **Approval 收敛为 policy + suspend/resume，而不是独立系统。**
6. **Pipeline 只装配和处理 I/O，ReAct 拥有 turn 内部。**
7. **旧 adapter、WorkingMemory、重复 approval config 全部 deprecated。**
8. **bot_project 作为完整集成示例，新增 minimal 示例作为推荐入门路径。**

我的优先级判断是：

```text
P0: runtime contract
P0: MemorySystem 旧 adapter 清理
P0: approval/control/interceptor 收口
P1: Pipeline 瘦身
P1: bot_project 默认 pipeline
P1: minimal examples
P2: DreamEngine / multi-agent memory / active-operation registry
```

当前最重要的不是继续实现更多功能，而是把已有能力压成一条清楚、稳定、可解释的主干, 且保持各个组件能自由拆装, 选择性加入产品架构中。


---

# 15. Status Audit (2026-05-04)

> 以下为对本建议文档的状态审计。仅标注各条建议的当前落地状态及对应 git commit, **不修改原文任何内容**。

| recommend.md 主张 | 当前状态 | 证据 / commit |
|---|---|---|
| §1 Pipeline 不拥有 turn 语义 | ✅ 已基本落地 | ReActAgent 拥有 turn/iteration; `pipeline.py` 仍待拆类 (Phase 5) |
| §2.1 Hook/Interceptor/Control/Approval 职责边界 | ✅ 已落地 | 四层独立目录 + ABC |
| §2.2 Approval = policy + suspend/resume + runtime state + control command | ✅ 已落地 | `22f1749` ApprovalClassifier + ApprovalRuntime; `6df89a6` 移除 ReActRuntime.suspend_strategy |
| §2.3 clean / full 模式 | ✅ 已落地 | `825896f` ReActRuntime; `runtime.py:42-127` |
| §3.1 MemorySystem 唯一入口 | ✅ 已落地 | `MemorySystemContextManager` 存在且被广泛引用 |
| §3.2 WorkingMemory 移除 | ✅ 已清空 | 全仓 grep 仅本文命中, 代码 0 残留 |
| §3.3 Memory 三层重命名 Session/Archive/Knowledge | ✅ 已落地 | `framework/memory/layers/{session,archive,knowledge}.py` |
| §3.4 Memory ≠ RuntimeStateStore | ✅ 已落地 | `e7fb8b3` 移除 checkpoint alias 冗余 |
| §3.5 MemoryProvider 降级为扩展 | ℹ️ 维持现状 | `MemoryProvider` 已是可选扩展 (prefetch/search), 非 canonical 写入入口 |
| §5.1 ToolManager 不负责审批 | ✅ 已落地 | `3185df6` 删除 ToolPolicyGuardHook |
| §6.1 supports_streaming → StreamingMode | ⚠️ 半迁移 | `StreamingMode` 枚举已存在; `pipeline/adapters.py` 仍有 7 处 `supports_streaming` — Phase 1 收尾 |
| §6.2 Memory adapter → compat/ | ❌ 取消 | 代码中无旧 adapter 需要 compat, 此建议不再执行 |
| §7.2 bot_project 默认 pipeline | ⏹ 待 Phase 1 | 当前 `bot_service.py:89` 写死 `pool` |
| §8 Governance / Safety / Interceptor 拆分 | ⏹ 待评估 | 未在本次 Phase 0-5 路线图中排入 |
| Phase 4 minimal examples | ⏹ 待 Phase 4 | `examples/` 仅 `bot_project/`, `sandbox/`, `security/` |
| TURN/ITERATION interceptor inert | ✅ 已过时 | `dc49b80` / `6832fc0` 已接入 |
| Control drain 5 安全边界 | ✅ 已落地 | `f20a15f` |
| Pipeline decomposition | ⏳ 部分 | `a173bca` 提取了 6 个私有方法, 未拆类 — Phase 5 |
