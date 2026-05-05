# ModexAgent — 核心架构

> 版本: 0.3.1 | 更新: 2026-05-05

## 1. 设计理念

本框架的核心设计目标：

| 目标 | 实现方式 |
|------|----------|
| **泛型类型安全** | `Agent[E]`、`ContentEmitter[E]` 通过泛型绑定事件枚举，编译期即可发现类型错误 |
| **I/O 无关** | `InputAdapter` / `OutputAdapter` 模式 + `PlatformAdapter` 能力声明，Agent 逻辑不感知消息来源 |
| **流式/非流式统一** | 由 `StreamingAwareEmitter` + `StreamingMode` 枚举实现，Agent 代码无需修改 |
| **多层记忆** | Short-term → History → Long-term 三层架构，每层独立 Scope 和存储后端 |
| **ReAct 推理** | Thought → Action → Observation 迭代执行循环，支持可中断 Runner |
| **多 Agent 协作** | SubagentManager（同步子 Agent）+ AgentPool（常驻 Peer Agent）+ AgentMessageBus 双模式 |
| **可插拔扩展** | Plugin 系统（MemoryProvider、ToolModifier）+ Skill 系统（SKILL.md 驱动） |

---

## 2. 顶层目录结构

```
framework/
├── core/                     # 核心抽象层
│   ├── graph/                # 图执行框架
│   │   ├── node.py           # Node + NodeTransition 抽象
│   │   ├── graph.py          # Graph + Edge 定义
│   │   ├── engine.py         # GraphEngine 执行循环
│   │   └── interrupt.py      # GraphInterrupt + _current_resume
│   ├── agent.py              # Agent[E] 泛型基类 + AgentContext
│   ├── emitter.py            # ContentEmitter[E] + StreamingAwareEmitter + AgentResult
│   ├── events.py             # AgentEvent 标记类 + EmitterConfig
│   ├── provider.py           # LLMProvider / StreamingLLMProvider
│   ├── tool_manager.py       # Tool + ToolManager + InMemoryToolManager
│   ├── context.py            # ContextManager + ContextState (多种实现)
│   ├── types.py              # InputMessage, OutputMessage, ToolCall, LLMResponse
│   ├── hooks.py              # AgentRunHook + RuntimeContextHook
│   ├── strategy.py           # ExecutionStrategy (ReAct / SingleTurn)
│   ├── runtime_context.py    # RuntimeContext + RuntimeContextManager (会话级运行时状态)
│   ├── tool_call_accumulator.py # 流式 ToolCall 片段累积解析
│   ├── message_utils.py      # Agent 消息归一化（role: agent → user 转换）
│   ├── constants.py          # 默认值常量
│   ├── memory.py             # MemoryStore 抽象
│   ├── session.py            # SessionStore 抽象
│   ├── tool.py               # Tool 基类（独立模块）
│   ├── agent_runtime_config.py # AgentRuntimeConfig, RuntimeControl
│   ├── llm_error.py          # RuntimeSafetyPolicy, LLMTimeoutPolicy
│   ├── skills/               # 技能系统
│       ├── manager.py        # SkillManager
│       ├── source.py         # FileSkillSource
│       ├── builder.py        # ProgressiveBuilder (渐进式 Skill 构建)
│       ├── filter.py         # SkillFilter, AllowListFilter, DenyListFilter
│       └── models.py         # Skill 数据模型
│
├── agents/                   # Agent 推理模式实现
│   └── react/
│       ├── agent.py          # ReActAgent + ReActEvent 枚举
│       ├── graph.py          # ReActGraph（图拓扑定义）
│       ├── engine.py         # GraphEngine（图执行引擎）
│       ├── nodes/            # 图节点实现
│       │   ├── start.py      # StartNode（初始化/恢复路由）
│       │   ├── llm.py        # LLMNode（模型调用 + 流式）
│       │   ├── tool.py       # ToolNode（分类 + 审批 + 执行）
│       │   └── end.py        # EndNode（构建 AgentResult）
│       ├── strategy.py       # SuspendResumeStrategy（审批策略）
│       ├── state.py          # TurnResumeState + RuntimeStateStore 别名
│       ├── constants.py      # ReActMetaKey + ExtensionKey
│       ├── assembler.py      # RuntimeAssembler（运行时服务组装）
│       ├── approval.py       # ApprovalClassifier（工具分类）
│       └── builder.py        # Agent 构建器
│
├── control/                  # 运行时控制平面
│   ├── channel.py            # ControlChannel（命令输入队列）
│   ├── event_bus.py          # ControlEventBus（事件输出总线）
│   ├── checkpoint.py         # RuntimeStateStore / JsonFileRuntimeStateStore
│   ├── types.py              # ControlCommand + ControlEvent 类型
│   ├── exceptions.py         # AgentControlError / AgentCancelled / ApprovalDenied
│   └── ui/                   # 用户界面实现（CLI / IM / Noop）
│
├── approval/                 # 审批系统
│   ├── state.py              # ApprovalState + ApprovalRequest + ApprovalTier
│   ├── store.py              # ApprovalStateStore（InMemory / LocalFile）
│   ├── response.py           # 审批命令解析（/approve, /deny）
│   └── constants.py          # ApprovalDecision + ApprovalAction
│
├── pipeline/                 # 端到端流程编排
│   ├── pipeline.py           # AgentPipeline（长期运行服务模式）
│   ├── adapters.py           # InputAdapter / OutputAdapter + 内置实现
│   ├── filters.py            # 内容过滤器（ThinkTag / Whitespace / Reasoning）
│   ├── context_assembler.py  # AgentContext 构建
│   └── approval_renderer.py  # 审批提示渲染
│
├── session/                  # 单次请求模式
│   └── agent_session.py      # AgentSession（HTTP API 风格）
│
├── memory/                   # 多层记忆系统
│   ├── system.py             # MemorySystem + MemorySystemContextManager
│   ├── core/                 # 记忆核心抽象
│   │   ├── scope.py          # MemoryScope (Session/User/Tenant/PeerPair/Composite)
│   │   ├── message.py        # ChatMessage 消息模型
│   │   ├── storage.py        # MemoryStorage 抽象
│   │   ├── base_managers.py  # 各层管理器抽象基类
│   │   ├── compression.py    # 压缩策略接口
│   │   ├── consolidation.py  # 记忆整合接口
│   │   └── lock.py           # 读写锁
│   ├── layers/               # 各层记忆管理器实现
│   │   ├── session.py        # Session Memory（短期记忆）
│   │   ├── archive.py        # Archive Memory（历史归档）
│   │   ├── knowledge.py      # Knowledge Memory（长期记忆）
│   │   ├── config.py         # 各层配置（MemoryLayerConfigSet）
│   │   └── factory.py        # 层工厂
│   ├── compaction/           # 压缩策略（触发 → 计划 → 摘要 → 提交）
│   │   ├── policy.py         # MessageCompactionPolicy + BoundaryPolicy
│   │   ├── boundary.py       # ToolChainBoundaryPolicy + UserTurnToolChainBoundaryPolicy
│   │   └── ...               # 其他策略实现
│   ├── compression/          # 压缩执行策略
│   │   ├── policies.py       # DefaultMemoryCompressionCoordinator
│   │   ├── semantic_filter.py # 语义过滤
│   │   ├── tool_chain.py     # 工具链压缩
│   │   └── ...
│   ├── consolidation/        # 记忆整合
│   │   ├── consolidator.py   # LLM 摘要压缩
│   │   └── dream_engine.py   # DreamEngine 两阶段离线整合
│   ├── stores/               # 存储后端
│   │   ├── file.py           # FileStorage (JSON Lines + KV)
│   │   ├── scoped_file.py    # ScopedFileStorage（按 scope 隔离）
│   │   └── in_memory.py      # InMemoryStorage
│   ├── registry/             # 存储注册表
│   │   ├── base.py           # StoreRegistry 抽象
│   │   ├── file.py           # FileStoreRegistry
│   │   └── in_memory.py      # InMemoryStoreRegistry
│   ├── injection/            # 记忆注入策略
│   │   ├── filter.py         # InjectionFilterStrategy（工具消息过滤）
│   │   └── ...
│   ├── archive/              # 归档策略
│   ├── content_transform.py  # 内容变换器（如 Base64 清洗）
│   ├── context_governance.py # 上下文治理（ToolChainRepair + Microcompact + TokenBudget）
│   ├── history.py            # MessageHistory 协议 + 多种实现
│   ├── history_search.py     # 历史搜索
│   ├── knowledge_search.py   # 知识搜索
│   ├── recorder.py           # 记忆记录器
│   ├── lifecycle.py          # 内存生命周期管理（AutoCompact）
│   └── utils.py              # 工具函数
│
├── multi_agent/              # 多 Agent 协作层
│   ├── descriptor.py         # AgentDescriptor + AgentInstance + AgentLLMConfig
│   ├── address.py            # AgentAddress (Agent 地址)
│   ├── state.py              # AgentState 枚举
│   ├── factory.py            # AgentFactory / DefaultAgentFactory
│   ├── subagent_manager.py   # SubagentManager（同步子 Agent，Queue 串行消费）
│   ├── pool.py               # AgentPool（常驻 Agent 池）
│   ├── registry.py           # AgentRegistry 协议 + AgentDirectory + AgentProfile
│   ├── bus.py                # AgentMessageBus 抽象（可插拔消息门面）
│   ├── router.py             # AgentMessageRouter（消息路由）
│   ├── coordinator.py        # TaskCoordinator（任务状态跟踪）
│   ├── tools.py              # SendMessageTool + SpawnSubagentTool
│   ├── hooks.py              # InboxFlushHook + RuntimeContextHook + PeerAutoSendHook
│   ├── governance.py         # ContextGovernancePolicy（上下文治理）
│   ├── intervention.py       # TaskSupervisor + TimeoutCancellationPolicy
│   ├── sanitizer.py          # ContentSanitizer（输出清洗）
│   ├── deduplicator.py       # MessageDeduplicator（消息去重）
│   ├── discovery.py          # Agent 发现（YAML 配置驱动）
│   ├── envelope.py           # AgentMessageEnvelope（消息信封）
│   ├── event_bus.py          # TaskEventReporter（任务事件报告）
│   ├── commands.py           # 命令系统
│   ├── context_builder.py    # MultiAgentContextBuilder
│   ├── filtered_tool_manager.py # FilteredToolManager（工具过滤）
│   ├── agent_skill_manager.py   # AgentSkillManager（按 Agent 过滤 Skills）
│   ├── assembly_kit.py       # ToolAssemblyKit（原子级工具克隆组装）
│   ├── toolset.py            # AgentToolset（工具集管理）
│   ├── peer_validator.py     # PeerAgentValidator（Peer 配置校验）
│   ├── policy_registry.py    # PolicyRegistry（策略注册表）
│   ├── rpc_broker.py         # RPCBroker（同步 RPC 调用）
│   └── utils.py              # 工具函数
│   ├── inbox/                # Agent 收件箱
│   │   ├── server.py         # InboxServer 抽象
│   │   ├── server_local.py   # LocalFileInboxServer（本地文件持久化）
│   │   ├── server_memory.py  # InMemoryInboxServer（内存实现）
│   │   ├── producer.py       # InboxProducer（消息生产者）
│   │   ├── consumer.py       # InboxConsumer（消息消费者）
│   │   ├── hook.py           # InboxFlushHook（turn 边界注入）
│   │   ├── tracker.py        # InboxTracker（投递追踪）
│   │   └── types.py          # InboxMessage 类型
│   ├── session_id.py         # SessionIdStrategy（会话 ID 生成策略）
│
├── messaging/                # 消息基础设施
│   ├── broker.py             # MessageBroker 抽象（P2P / PubSub / Broadcast）
│   ├── broker_memory.py      # InMemoryMessageBroker
│   └── broker_bridge.py      # BrokerBridgeService（Broker ↔ Adapter 桥接）
│
├── adapters/                 # 平台适配层
│   └── platform.py           # PlatformAdapter + StreamingMode + AdapterRegistry
│
├── tools/                    # 工具系统
│   ├── standard/             # 内置标准工具
│   │   ├── file_tool.py      # ReadFile, WriteFile, EditFile, ListDir
│   │   └── shell_tool.py     # Shell 命令执行
│   ├── mcp/                  # MCP 协议集成
│   │   ├── client.py         # MCP 客户端
│   │   ├── manager.py        # MCPClientManager
│   │   └── tool.py           # MCPTool
│   ├── mcp_adapter.py        # MCP 协议适配器
│   ├── executor.py           # 工具执行器
│   ├── registry.py           # 工具注册表
│   ├── toolkit.py            # 工具包
│   ├── metadata_parser.py    # 工具元数据解析
│   ├── secure_wrapper.py     # 安全包装
│   └── types.py              # 工具类型定义
│
├── plugins/                  # 插件系统
│   ├── abc.py                # MemoryProvider 抽象基类
│   ├── loader.py             # 插件发现与加载
│   ├── manager.py            # PluginManager
│   └── context.py            # PluginContext
│
├── extensions/               # 可选扩展
│   ├── llm/
│   │   └── litellm_provider.py  # LiteLLM 统一 LLM 调用（100+ 模型）
│   ├── memory/
│   │   ├── faiss_store.py    # FAISS 向量存储
│   │   ├── chroma.py         # ChromaDB 向量存储
│   │   ├── archive.py        # 归档策略
│   │   ├── config.py         # 扩展配置
│   │   ├── embedding_config.py # Embedding 配置
│   │   └── lifecycle.py      # 生命周期管理
│   └── session/
│       ├── sqlite_store.py   # SQLite 会话存储
│       ├── sqlalchemy_store.py # SQLAlchemy 会话存储
│       └── memory_store.py   # 内存会话存储
│
├── sandbox/                  # 代码执行沙箱
│   ├── adapters/
│   │   ├── base.py           # 沙箱适配器基类
│   │   ├── subprocess.py     # 子进程沙箱
│   │   ├── landlock.py       # Landlock 沙箱（Linux）
│   │   ├── docker.py         # Docker 沙箱
│   │   └── e2b.py            # E2B 云沙箱
│   ├── factory.py            # SandboxFactory
│   ├── config.py             # 沙箱配置
│   ├── types.py              # 沙箱类型
│   ├── enums.py              # 沙箱枚举
│   ├── validation.py         # 沙箱验证
│   ├── isolation.py          # 隔离策略
│   ├── platform.py           # 平台检测
│   └── exceptions.py         # 沙箱异常
│
├── security/                 # 安全策略
│   ├── policy.py             # 安全策略
│   ├── validators.py         # 验证器
│   ├── handlers.py           # 安全处理器
│   ├── local_executor.py     # 本地执行器
│   └── exceptions.py         # 安全异常
│
├── registry/                 # 通用注册表
├── utils/                    # 工具函数
│   ├── helpers.py            # 通用助手
│   ├── media_utils.py        # 媒体文件处理
│   └── tokenizer.py          # Token 计数
│
├── docs/                     # 项目文档
├── examples/                 # 示例项目
│   └── bot_project/          # QQ Bot 完整示例
├── tests/                    # 测试套件
│   ├── unit/                 # 单元测试
│   ├── integration/          # 集成测试
│   └── e2e/                  # 端到端测试
├── ARCHITECTURE.md           # 架构总览（快速参考）
└── CLAUDE.md                 # Claude Code 开发指引
```

---

## 3. 核心架构图

### 3.1 组件关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                        AgentPipeline                            │
│                                                                 │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────┐  │
│  │ InputAdapter │───>│   ReActAgent     │───>│ OutputAdapter│  │
│  │ (QQ/CLI/HTTP)│    │  ┌────────────┐  │    │ (QQ/CLI)     │  │
│  └──────────────┘    │  │  Emitter   │  │    └──────────────┘  │
│                      │  │ (桥梁角色) │  │                       │
│                      │  └────────────┘  │                       │
│                      └───────┬──────────┘                       │
│                              │                                   │
│  ┌───────────────┐   ┌──────┴──────┐   ┌───────────────────┐  │
│  │ ContextManager│   │ ToolManager │   │   MemorySystem    │  │
│  │ (对话上下文)  │   │ (工具执行)  │   │   (多层记忆)      │  │
│  └───────────────┘   └─────────────┘   └───────────────────┘  │
│                                                                 │
│  ┌───────────────┐   ┌──────────────┐   ┌──────────────────┐  │
│  │  SkillManager │   │ RuntimeCtx   │   │  AgentRunHooks   │  │
│  │ (技能系统)    │   │ (运行时状态) │   │  (生命周期钩子)  │  │
│  └───────────────┘   └──────────────┘   └──────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 完整数据流

```
用户消息 (QQ/CLI/HTTP)
     │
     ▼
InputAdapter.receive()  ──>  InputMessage (含 attachments)
     │
     ▼
AgentPipeline._process_message()
     │
     ├──> ContentSanitizer.sanitize()       ──>  清洗用户输入
     ├──> MessageDeduplicator.check()        ──>  消息去重
     ├──> CommandInterceptor.check()         ──>  命令拦截
     │
     ├──> ContextManager.load(session_id)
     │    └──> ContextState (system_prompt + MessageHistory)
     │
     ├──> SkillManager.build_prompt()        ──>  注入 skills 到系统提示词
     ├──> InboxFlushHook.before_turn()       ──>  拉取收件箱异步消息
     ├──> RuntimeContextManager.get_context() ──>  初始化会话级运行时状态
     │
     ├──> AgentContext 构建 (history + tool_manager + hooks + runtime_context)
     │
     ▼
ReActAgent.run(context, emitter)
     │
     │  图引擎执行 (GraphEngine.run):
     │    0. before_turn hook
     │    1. StartNode ──> 路由到 LLMNode 或 ToolNode（恢复路径）
     │    2. LLMNode:
     │       ├── before_iteration hook
     │       ├── _stream_with_control() ──> LLM 响应 (流式/非流式)
     │       ├── after_llm_response hook
     │       └── 路由: HAS_TOOLS → ToolNode, NO_TOOLS → EndNode
     │    3. ToolNode:
     │       ├── _classify_all() ──> 工具分类（NORMAL/SENSITIVE/DANGEROUS/HARDLINE）
     │       ├── 审批策略 (SuspendResumeStrategy) ──> 可能触发 GraphInterrupt
     │       ├── before_tool_execution hook
     │       ├── 执行工具 (批量并行，经 InterceptorChain 包裹)
     │       ├── after_tool_execution hook
     │       ├── after_iteration hook
     │       └── 路由: TOOLS_DONE → LLMNode（循环）, TURN_CANCELLED → EndNode
     │    4. EndNode ──> 构建 AgentResult
     │    5. after_turn hook
     │
     ├──> emitter.emit_delta()        ──>  流式内容片段
     ├──> emitter.emit()              ──>  推理过程 (MODEL_REASONING)
     ├──> emitter.emit()              ──>  工具调用通知 (TOOL_CALL_START/END)
     ├──> emitter.emit_stream_end()   ──>  一轮 LLM 输出结束
     └──> emitter.emit_complete()     ──>  AgentResult (含 attachments)
              │
              ▼
         StreamingAwareEmitter
              │
              ├── NATIVE 流式: output_adapter.send_delta() 实时发送
              ├── PSEUDO 流式: 缓冲后 output_adapter.send() 一次性发送
              └── NONE: 不支持流式，仅完整发送
              │
              ▼
         OutputAdapter.send()  ──>  发送到目标平台 (含附件转发)
              │
              ▼
         ContextManager.save()  ──>  保存对话结果到 MemorySystem
```

---

## 4. 两种使用模式

### 4.1 AgentPipeline — 长期运行服务

适用于 Bot 服务、CLI 交互等需要持续接收消息的场景。

```python
pipeline = AgentPipeline(
    agent=ReActAgent(provider=llm),
    context_manager=context_manager,
    tool_manager=tool_manager,
    input_adapter=qq_input,
    output_adapter=qq_output,
    emitter_factory=lambda sid: QQBotEmitter(qq_output, sid),
    skill_manager=skill_manager,
    dream_engine=dream_engine,
    hooks=[inbox_flush_hook],
    subagent_manager=subagent_manager,
    runtime_context_manager=runtime_ctx_mgr,
)
await pipeline.run()  # 持续运行，循环接收消息
```

### 4.2 AgentSession — 单次请求/响应

适用于 HTTP API、子 Agent 调用等场景。

```python
session = AgentSession(
    agent=ReActAgent(provider=llm),
    context_manager=context_manager,
    tool_manager=tool_manager,
    skill_manager=skill_manager,
)
result = await session.process_message(
    message=InputMessage(content="Hello"),
    emitter=BufferingEmitter(),
    session_id="user_123",
)
```

### 4.3 AgentPool — 常驻多 Agent 池

适用于多 Agent 常驻协作的场景。每个 Agent 拥有独立的 Pipeline 和上下文。

```python
pool = AgentPool(
    broker=broker,
    agent_factory=factory,
    agent_bus=agent_bus,
    inbox_consumer=inbox_consumer,
    enable_inbox_polling=True,
)
await pool.register(descriptor, session_id)
await pool.start()
```

---

## 5. 泛型事件系统

每个 Agent 定义自己的事件枚举，通过泛型实现编译期类型安全：

```python
# 1. 定义事件枚举
class ReActEvent(AgentEvent, Enum):
    MODEL_OUTPUT = "model_output"
    MODEL_REASONING = "model_reasoning"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    FINAL_OUTPUT = "final_output"
    START = "start"
    ERROR = "error"
    MAX_ITERATIONS = "max_iterations"
    PROGRESS = "progress"

# 2. Agent 绑定事件枚举
class ReActAgent(Agent[ReActEvent]):
    event_enum = ReActEvent

# 3. Emitter 也绑定同一枚举
class QQBotEmitter(StreamingAwareEmitter[ReActEvent]):
    ...

# 4. 配置可以精确控制启用/禁用的事件
config = EmitterConfig(
    enabled_events={"model_output", "final_output"},
    disabled_events={"model_reasoning"},
    max_tool_result_length=2000,
)
```

**为什么用泛型？**
- 编译期类型安全：不能发射错误类型的事件
- IDE 自动补全正确工作
- 每个 Agent 的事件命名空间相互隔离

---

## 6. 流式与非流式

框架通过 `StreamingMode` 枚举和 `StreamingAwareEmitter` 统一处理：

```
StreamingMode:
  NATIVE  → 平台原生支持流式（CLI、WebSocket、SSE）
  PSEUDO  → 平台不支持流式，缓冲后一次发送（QQ Bot、Discord）
  NONE    → 不支持流式

Agent (ReActAgent)            # 逻辑不变
    │
    ▼
ContentEmitter.emit()
    │
    ▼
StreamingAwareEmitter
    ├── NATIVE: emit_delta() → send_delta() → 立即发送每个片段
    └── PSEUDO/NONE:
        ├── emit_delta() → 缓冲到 _content_buffer
        ├── emit_content() → 缓冲完整内容
        └── emit_stream_end() / emit_complete() → flush → 一次性发送
```

**ThinkTagExtractor**：自动从 `<think...</think*>` 标签中分离推理内容和正式内容，支持 DeepSeek R1、Kimi 等推理模型。

---

## 7. 关键设计模式

| 模式 | 应用位置 | 说明 |
|------|----------|------|
| **泛型事件** | Agent[E] + ContentEmitter[E] | 每个 Agent 有独立事件枚举 |
| **策略模式** | ExecutionStrategy | ReAct / SingleTurn 执行策略 |
| **策略模式** | ToolManager 执行模式 | SEQUENTIAL / PARALLEL / ASYNC |
| **策略模式** | Memory 压缩策略 | Consolidator + 多种 Compression 策略 |
| **工厂模式** | emitter_factory(session_id) | 每个会话创建独立 Emitter |
| **工厂模式** | AgentFactory | 按描述符动态组装 Agent |
| **适配器模式** | MemorySystemContextManager | 将 MemorySystem 包装为 ContextManager |
| **观察者模式** | AgentRunHook | before/after_turn, before/after_iteration, before/after_tool_execution |
| **管道模式** | AgentPipeline | 编排 Input → Context → Agent → Emitter → Output |
| **过滤器链** | ChainedContentFilter | ThinkTag + Whitespace 等过滤器链式组合 |
| **队列消费** | SubagentManager | asyncio.Queue 串行消费子 Agent 请求 |
| **对象池** | AgentPool | 常驻 Agent 生命周期管理 |
| **协议** | AgentRegistry | Protocol 定义的只读发现层 |
| **插件** | MemoryProvider | 可插拔记忆后端（Mem0、ChromaDB 等） |
| **克隆组装** | ToolAssemblyKit | 按名称/谓词克隆工具到新 ToolManager |

---

## 8. 与 nanobot 的架构对比

| 特性 | nanobot | 本框架 (V2) |
|------|---------|------------|
| 事件系统 | `AgentHook` 回调 | 泛型 `Agent[E]` + `ContentEmitter[E]` |
| 输出处理 | `BaseChannel.send()` | `ContentEmitter` + `OutputAdapter` + `PlatformAdapter` 三层 |
| 工具执行 | `ToolRegistry` 顺序 | `ToolManager` 支持并行/异步 + `ToolAssemblyKit` 克隆组装 |
| 会话管理 | `SessionManager` | `ContextManager` + `MemorySystem` 三层 |
| I/O 抽象 | `BaseChannel` (每平台一个类) | `InputAdapter` / `OutputAdapter` 分离 + `StreamingMode` 枚举 |
| 类型安全 | 运行时检查 | 泛型编译期类型安全 |
| 多 Agent | `SubagentManager` + `SpawnTool` | SubagentManager + AgentPool + AgentMessageBus + AgentFactory |
| 记忆系统 | `MemoryStore` + `Dream` | 三层 MemorySystem + DreamEngine + Plugin MemoryProvider |
| Provider | 原生 openai/anthropic SDK | LiteLLM 统一 100+ 模型 |
| 沙箱 | 无 | Subprocess / Landlock / Docker / E2B |
| 安全 | 基础 | SecurityPolicy + Validators + SecureWrapper |
| 插件 | 无 | PluginManager + MemoryProvider + ToolModifier |
| 运行时上下文 | 无 | RuntimeContext + RuntimeContextManager (会话级状态) |

---

## 9. 多 Agent 通信架构（当前实现）

当前框架支持**两种多 Agent 模式**：

### 9.1 同步子 Agent（SubagentManager）

通过 `asyncio.Queue` 实现单线程串行消费：
- `spawn_and_wait()` 将请求打包入队，通过 `asyncio.Future` 等待结果
- 独立消费者协程从队列中串行取出并执行
- 执行完成后直接返回 `AgentResult`

```python
result = await manager.spawn_and_wait(
    parent_address=parent_address,
    descriptor=descriptor,
    task_prompt="Analyze this file",
    conversation_id="conv_123",
)
```

### 9.2 常驻 Peer Agent（AgentPool）

每个 Peer Agent 拥有独立的 AgentPipeline 和 ContextManager：
- 通过 `AgentMessageBus` 接收消息
- `SendMessageTool` 跨 Agent 主动发送消息并唤醒目标 Agent
- 支持 inbox 轮询（idle 时定期检查 pending 消息）
- Peer Agent 通过 `send_message_async` 回传结果

### 9.3 Agent 间消息

`SendMessageTool` 使用 `AgentMessageBus.send()` 进行消息投递：
- 受 `allowed_callers` / `allowed_targets` ACL 保护
- 动态构建 available peers 描述（从 `AgentRegistry` 查询）
- 支持同步（`send_message`）和异步（`send_message_async`）两种通信方式

### 9.4 Broker、Bus 与 Inbox 的关系

| 能力 | MessageBroker | AgentMessageBus | LocalFileInboxServer |
|------|--------------|-----------------|---------------------|
| 持久化 | 否（内存） | 取决于实现 | 是（本地文件） |
| exactly-once | 否 | 取决于实现 | 是（`delivered_ids.json`） |
| 唤醒消费者 | 是（阻塞消费） | 是（signal + poll） | 否（被动拉取） |
| 消息信封 | `BrokerMessage` | `AgentMessageEnvelope` | `InboxMessage` |
| 当前用途 | 底层传输 | Agent 间消息门面 | 异步结果持久化 |

---

## 10. 运行时上下文（RuntimeContext）

每个 Agent turn 拥有独立的 `RuntimeContext`，用于在 hooks 和 tools 之间共享状态：

```python
# RuntimeContextManager 按 session_id 隔离
runtime_ctx = await manager.get_context(session_id, metadata)

# 清除（turn 开始时）
await runtime_ctx.clear()

# 读写状态
await runtime_ctx.set("key", value)
value = await runtime_ctx.get("key")

# 记录工具调用
await runtime_ctx.record_tool_call(tool_name, arguments, result)
```

`RuntimeContextHook` 自动注入到 AgentPipeline / AgentSession，管理 per-turn 生命周期。

---

## 11. 安全与沙箱

### 安全层

- **SecurityPolicy**：统一安全策略定义
- **Validators**：路径验证、命令白名单、输入校验
- **SecureWrapper**：工具安全包装，限制危险操作
- **LocalExecutor**：安全的本地命令执行

### 沙箱层

| 沙箱类型 | 适用场景 | 平台 |
|----------|----------|------|
| `SubprocessSandbox` | 基本隔离 | 全平台 |
| `LandlockSandbox` | 文件系统沙箱 | Linux |
| `DockerSandbox` | 容器级隔离 | 全平台（需 Docker） |
| `E2BSandbox` | 云端沙箱 | 全平台（需 API Key） |

通过 `SandboxFactory` 根据平台和配置自动选择合适的沙箱。

---

## 12. 插件系统

框架通过 `PluginManager` 支持可插拔扩展：

### MemoryProvider

可插拔记忆后端，作为三层记忆的增强层：

```python
class MemoryProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    async def initialize(self, **kwargs) -> None: ...
    async def shutdown(self) -> None: ...

    async def add(self, messages, context) -> dict: ...
    async def search(self, query, context) -> list: ...
    async def modify_memory_system(self, memory_system) -> None: ...
```

内置插件示例：
- `mem0_memory`：基于 Mem0 的语义记忆
- `tool_call_cleanup`：工具调用结果清理策略

---

## 13. 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| LLM 调用 | LiteLLM（统一 100+ 模型） |
| QQ Bot | qq-botpy（腾讯官方 SDK） |
| MCP | MCP Protocol（工具服务器） |
| 存储 | JSON Lines 文件 / SQLite / 内存 |
| 向量存储 | FAISS / ChromaDB（可选） |
| 沙箱 | Subprocess / Landlock / Docker / E2B |
| 异步 | asyncio |
| 包管理 | uv（推荐） |
## Current Runtime Status

The ReAct runtime is now graph-based. `ReActAgent` delegates turn execution to
`ReActGraph`, whose durable nodes are `StartNode`, `LLMNode`, `ToolNode`, and
`EndNode`. Hook, interceptor, and control integration is layered around those
runtime boundaries rather than implemented as ordinary graph nodes only.

`clean` mode should sanitize hook/approval/interceptor/control/runtime-store
services at turn entry. `full` mode wires those services explicitly. The current
bot project default interceptor chain contains `ControlDrainInterceptor` and
`ToolResultLimitInterceptor`; timeout interceptors are not default wiring. See
`docs/current-runtime.md` for the up-to-date runtime summary.
