# 核心模块详解

> 本文档深入讲解 ModexAgent 的各个核心模块的设计、接口和实现。

---

## 1. Agent 抽象层 (`core/agent.py`)

### Agent[E] — Agent 泛型基类

所有 Agent 的抽象基类，通过泛型参数 `E` 绑定特定的事件枚举类型。

```python
class Agent(ABC, Generic[E]):
    event_enum: Type[E]  # 子类必须定义事件枚举

    @abstractmethod
    async def run(self, context: AgentContext, emitter: ContentEmitter[E]) -> AgentResult:
        """执行 Agent 推理循环"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent 名称"""
        pass
```

**设计原则**：Agent 只负责推理逻辑，不处理 I/O、不管理历史、不发送消息。

### AgentContext — 执行上下文

```python
@dataclass
class AgentContext:
    system_prompt: str                          # 系统提示词
    history: MessageHistory                     # 对话历史（MessageHistory 协议对象）
    tool_manager: ToolManager                   # 工具管理器
    session_id: str = ""                        # 会话 ID
    max_iterations: int = 10                    # ReAct 最大迭代次数
    max_tools_per_turn: int = 10                # 单轮最大工具调用数
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    on_checkpoint: Callable | None = None       # 检查点回调
    hooks: list[AgentRunHook] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)      # Agent→User 附件路径
    runtime_context_manager: RuntimeContextManager | None = None
    runtime_context: RuntimeContext | None = None              # 会话级运行时状态
```

关键方法：
- `to_messages()` → 将 system_prompt + history 转换为 LLM 消息列表（自动处理 `role: "agent"` → `role: "user"` 转换，注入 Agent 通信说明）
- `get_tool_descriptions()` → 获取注册工具的 OpenAI 格式描述
- `add_attachment(path)` → 添加附件路径到 attachments 列表

**与旧版本差异**：`history` 字段从 `List[Dict]` 变为 `MessageHistory` 协议对象；新增 `session_id`、`attachments`、`runtime_context_manager`、`runtime_context`；移除了 `on_messages` 回调。

**Context Variable**：`current_agent_context` 是一个 `contextvars.ContextVar`，允许在工具执行期间获取当前 Agent 上下文。

---

## 2. ContentEmitter 体系 (`core/emitter.py`)

### 继承层次

```
ContentEmitter[E] (ABC)                # 抽象基类
├── StreamingAwareEmitter[E]           # 流式/非流式自动切换
│   └── QQBotEmitter                   # QQ Bot 业务实现
├── BufferingEmitter[E]                # 缓冲所有内容（测试/非流式）
├── BusEmitter[E]                      # 消息总线发射器
└── LoggingEmitter[E]                  # 纯日志输出（调试）
```

### 核心方法

| 方法 | 说明 | 调用时机 |
|------|------|----------|
| `emit_delta(delta)` | 流式内容片段（增量） | LLM 生成每个 token |
| `emit_content(full)` | 完整内容（非流式） | 非流式 LLM 响应 |
| `emit_stream_end(resuming)` | 一轮 LLM 输出结束 | 每次迭代结束 |
| `emit_tool_error(error_data)` | 工具执行错误 | 工具执行失败时 |
| `emit_complete(result)` | Agent 执行完成 | 最终返回前 |
| `emit_error(error)` | 错误通知 | 异常时 |
| `emit(event, data)` | 通用事件发送 | 业务事件分发 |
| `flush()` | 刷新缓冲区 | 强制发送缓存内容 |

### StreamingAwareEmitter 工作原理

```python
class StreamingAwareEmitter(ContentEmitter[E]):
    def __init__(self, output_adapter, session_id, config=None):
        self.output_adapter = output_adapter
        self.session_id = session_id
        self._content_buffer = ""
        self._reasoning_buffer = ""

    @property
    def is_true_streaming(self) -> bool:
        return self.output_adapter.streaming_mode == StreamingMode.NATIVE

    async def emit_delta(self, delta: str) -> None:
        if self.is_true_streaming:
            # 真流式：立即发送
            await self.output_adapter.send_delta(delta, self.session_id)
        else:
            # 伪流式：缓冲
            self._content_buffer += delta

    async def emit_stream_end(self, resuming=False) -> None:
        if not self.is_true_streaming:
            await self._flush_buffers()  # 伪流式：flush 缓冲区

    async def emit_complete(self, result: AgentResult) -> None:
        if not self.is_true_streaming and self._content_buffer:
            await self._flush_buffers()
        # 转发 result 中的 attachments
        if result.attachments:
            await self.output_adapter.send(
                OutputMessage(content="", attachments=result.attachments),
                self.session_id,
            )
        # 清理缓冲区
        self._content_buffer = ""
        self._reasoning_buffer = ""
```

### AgentResult — 执行结果

```python
@dataclass
class AgentResult:
    content: str | None = None              # 最终输出内容
    reasoning: str | None = None            # 推理/思考过程（DeepSeek R1、Kimi 等）
    stop_reason: str = "completed"          # completed / max_iterations / error / cancelled
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    messages: Sequence[ChatMessage | dict] = field(default_factory=list)  # 本次生成的历史消息
    partial_content: str | None = None      # 取消时保留的部分内容
    attachments: list[str] = field(default_factory=list)  # 要发送给用户的附件路径
```

---

## 3. ReActAgent (`agents/react/agent.py`)

### ReActEvent 事件枚举

```python
class ReActEvent(AgentEvent, Enum):
    MODEL_OUTPUT = "model_output"           # 模型文本输出（流式片段）
    MODEL_REASONING = "model_reasoning"     # 推理/思考过程
    TOOL_CALL_START = "tool_call_start"     # 准备调用工具
    TOOL_CALL_END = "tool_call_end"         # 工具调用完成
    ITERATION_START = "iteration_start"     # ReAct 迭代开始
    ITERATION_END = "iteration_end"         # ReAct 迭代结束
    FINAL_OUTPUT = "final_output"           # 确认最终输出
    START = "start"                         # Agent 启动
    ERROR = "error"                         # 错误
    MAX_ITERATIONS = "max_iterations"       # 达到最大迭代
    PROGRESS = "progress"                   # 进度提示
```

### ReAct 循环核心逻辑

```
run(context, emitter):
    emit(START)
    hooks.before_turn(ctx)
    while iteration < max_iterations:
        hooks.before_iteration(ctx)
        messages = context.to_messages()
        response = _request_llm(messages, context, emitter)
        assistant_msg = _build_assistant_message(content, tool_calls)
        context.history.append(assistant_msg)

        if tool_calls:
            hooks.before_tool_execution(ctx, tool_calls)
            for tool_call in tool_calls:
                emit(TOOL_CALL_START)
                result = _execute_tool(tool_call, context)
                emit(TOOL_CALL_END)
                context.history.append(tool_message)
            hooks.after_tool_execution(ctx, results)
            hooks.after_iteration(ctx)
        else:
            result = AgentResult(content, reasoning, attachments)
            emit(FINAL_OUTPUT)
            emit_complete(result)
            hooks.after_turn(ctx, result)
            return result

    result = AgentResult(stop_reason="max_iterations")
    emit_complete(result)
    hooks.after_turn(ctx, result)
    return result
```

### LLM 请求路径选择

```python
async def _request_llm(self, messages, context, emitter):
    wants_streaming = emitter.wants_streaming()
    is_streaming_provider = isinstance(self.provider, StreamingLLMProvider)

    if wants_streaming and is_streaming_provider:
        # 流式路径：通过回调实时传递内容
        response = await self.provider.chat_stream(
            messages=messages,
            on_content_delta=lambda d: emitter.emit_delta(d),
            on_reasoning_delta=lambda d: emitter.emit(MODEL_REASONING, d),
        )
        emitter.emit_stream_end(resuming=bool(response.tool_calls))
    else:
        # 非流式路径：一次性获取完整响应
        response = await self.provider.chat(messages=messages)
        emitter.emit_content(response.content)
        emitter.emit_stream_end(resuming=bool(response.tool_calls))
```

---

## 4. LLM Provider (`core/provider.py` + `extensions/llm/`)

### Provider 继承层次

```
LLMProvider (ABC)
├── chat(messages, ...) -> LLMResponse         # 非流式
├── chat_with_retry(...) -> LLMResponse        # 带重试（指数退避）
├── complete(prompt, ...) -> LLMResponse       # 单提示词模式
└── StreamingLLMProvider (ABC)
    ├── chat_stream(messages, ...) -> LLMResponse  # 流式
    └── chat_stream_with_retry(...)
```

### LiteLLMProvider — 统一 100+ 模型

通过 `litellm.acompletion` 统一调用不同模型提供商。

关键特性：
- **ThinkTagExtractor**：从 `<think...</think*>` 标签中分离推理内容和正式内容，支持不完整标签跨 chunk 边界
- **ToolCallAccumulator**：解析流式 tool_call 片段，累积为完整调用（配合 `parse_tool_call_chunks_from_delta`）
- **自动重试**：指数退避，识别临时性错误（429、500、502、503、504）
- **环境变量抑制**：自动抑制 litellm 和 httpx 的日志输出

---

## 5. ToolManager (`core/tool_manager.py`)

### 继承层次

```
Tool (基类)
├── name, description, parameters
├── execute(**kwargs) -> Any
├── get_schema() -> Dict  (OpenAI 格式)
├── validate_params(params) -> List[str]
├── clone() -> Tool  (支持有状态工具的克隆)
└── 支持两种构造方式:
    ├── 新方式: Tool(name="...", description="...", parameters={...})
    └── 旧方式: 继承后通过 @property 定义

ToolManager (ABC)
└── InMemoryToolManager
    ├── register(tool, config)
    ├── execute(tool_name, arguments) -> ToolResult
    ├── execute_batch(calls, parallel=True) -> List[ToolResult]
    ├── get_tool_descriptions() -> List[Dict]
    ├── get_tool(name) -> Tool | None
    ├── list_tools() -> List[str]
    └── unregister(name) -> Tool | None
```

### 三种执行模式

| 模式 | 说明 | 适用场景 |
|------|------|----------|
| `SEQUENTIAL` | 同步阻塞，一个接一个 | 有依赖关系的工具 |
| `PARALLEL` | 线程池并行执行 | CPU 密集型工具 |
| `ASYNC` | 协程异步执行（默认） | I/O 密集型工具 |

### 批量执行

```python
results = await tool_manager.execute_batch(
    tool_calls=[
        {"tool_name": "weather", "arguments": {"city": "北京"}},
        {"tool_name": "stock", "arguments": {"symbol": "AAPL"}},
    ],
    parallel=True,  # 自动并行执行
)
```

### ToolConfig 和 ToolManagerConfig

```python
@dataclass
class ToolConfig:
    timeout: float = 30.0
    execution_mode: ToolExecutionMode = ToolExecutionMode.ASYNC
    retry_count: int = 0
    retry_delay: float = 1.0
    enabled: bool = True

@dataclass
class ToolManagerConfig:
    max_workers: int = 10
    default_timeout: float = 30.0
    default_execution_mode: ToolExecutionMode = ToolExecutionMode.ASYNC
    enable_parallel: bool = True
    parallel_max_workers: int = 5
```

### ToolResult — 统一结果

```python
class ToolResult:
    tool_name: str
    result: Any
    error: Optional[str]
    execution_time: float
    call_id: Optional[str]

    @property
    def success(self) -> bool: ...

    def to_message(self) -> Dict: ...  # 转为 LLM tool message 格式
```

---

## 6. ContextManager (`core/context.py`)

### 继承层次

```
ContextManager (ABC)
├── load(session_id) -> ContextState
├── save(session_id, user_message, assistant_result, metadata)
├── build_system_prompt(tool_manager, skill_manager, runtime_info)
├── clear(session_id)
│
├── InMemoryContextManager    # 内存存储，开发/测试用
├── FileContextManager        # JSON 文件持久化
├── EphemeralContextManager   # 纯瞬时，单轮执行后丢弃
└── MemorySystemContextManager # 适配 MemorySystem（生产用）
```

### ContextState

```python
@dataclass
class ContextState:
    system_prompt: str = ""
    history: MessageHistory = field(default_factory=ListMessageHistory)
    metadata: dict[str, Any] = field(default_factory=dict)
```

**重要**：`history` 字段现在是 `MessageHistory` 协议对象（而非旧的 `list[dict]`）。`ContextState.__post_init__` 会自动将 `list` 转换为 `ListMessageHistory`。

`ContextState.to_messages()` 方法自动处理 `role: "agent"` → `role: "user"` 的转换，并在存在 agent 消息时追加 Agent 通信说明到系统提示词。

### 系统提示词构建

`build_system_prompt()` 自动组合以下内容：

```
base_system_prompt       # 基础系统提示词（来自配置）
+ tools_section          # 工具描述（来自 ToolManager）
+ skills_prompt          # Skill 提示词（来自 SkillManager）
+ runtime_info           # 运行时信息（时间、平台等）
```

---

## 7. I/O 适配器 (`pipeline/adapters.py` + `adapters/platform.py`)

### InputAdapter — 输入适配器

```python
class InputAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def receive(self) -> AsyncIterator[InputMessage]: ...
```

### OutputAdapter — 输出适配器

```python
class OutputAdapter(ABC):
    content_filter: Optional[ContentFilter] = None

    @property
    def streaming_mode(self) -> StreamingMode:
        """流式输出模式，默认 PSEUDO。"""
        return StreamingMode.PSEUDO

    @abstractmethod
    async def send(self, message: OutputMessage, session_id: str) -> None: ...

    async def send_delta(self, delta: str, session_id: str, metadata=None) -> None:
        """默认实现：缓冲到 _delta_buffers"""

    async def flush_deltas(self, session_id: str) -> None:
        """默认实现：合并缓冲内容后一次性 send()"""
```

**注意**：`supports_streaming` 布尔属性已被 `streaming_mode` 枚举属性替代。`StreamingAwareEmitter` 通过检查 `streaming_mode == StreamingMode.NATIVE` 来判断是否真流式。

### StreamingMode 枚举

```python
class StreamingMode(str, Enum):
    NATIVE = "native"    # 平台原生支持流式
    PSEUDO = "pseudo"    # 伪流式（缓冲后一次发送）
    NONE = "none"        # 不支持流式
```

### PlatformAdapter — 平台适配器

```python
class PlatformAdapter(ABC):
    @property
    @abstractmethod
    def platform_name(self) -> str: ...

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.PSEUDO

    @property
    def supports_message_edit(self) -> bool:
        return False

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

### 内置适配器实现

| 适配器 | 说明 | 流式模式 |
|--------|------|----------|
| `CLIOutputAdapter` | 终端输出 | NATIVE（逐字打印） |
| `HTTPOutputAdapter` | SSE 输出 | NATIVE（Server-Sent Events） |
| `QQOutputAdapter` | QQ Bot 输出 | PSEUDO（缓冲后发送） |
| `CompositeOutputAdapter` | 组合多个适配器 | 取决于子适配器 |
| `SessionPrefixStripAdapter` | 装饰器，剥离 session 前缀 | 透传 |

### InputMessage / OutputMessage

```python
@dataclass
class InputMessage:
    content: str                               # 消息内容（唯一必填字段）
    session_id: str = "default"                # 会话 ID
    channel: str = "default"                   # 消息渠道
    sender_id: str = "default"                 # 发送者 ID
    chat_id: str = "default"                   # 聊天/群组 ID
    source: str = "unknown"                    # 来源标识
    msg_type: MessageType = MessageType.TEXT   # 消息类型枚举
    metadata: dict[str, Any] = field(...)      # 额外元数据
    attachments: list[str] = field(...)        # 附件本地文件路径列表
    timestamp: datetime = field(...)           # 时间戳

@dataclass
class OutputMessage:
    content: str                               # 消息内容（唯一必填字段）
    session_id: str = "default"
    channel: str = "default"
    recipient_id: str = "default"
    chat_id: str = "default"
    message_type: str = "text"                 # text, image, file, error
    reasoning: str | None = None               # 推理/思考过程
    metadata: dict[str, Any] = field(...)
    attachments: list[str] = field(...)        # 附件本地文件路径列表
    timestamp: datetime = field(...)
```

**新增字段**：`attachments` 支持发送图片、文件等附件；`MessageRole` 枚举包含 `SYSTEM`、`USER`、`ASSISTANT`、`TOOL`、`AGENT` 五种角色。

---

## 8. 内容过滤器 (`pipeline/filters.py`)

| 过滤器 | 功能 |
|--------|------|
| `ThinkTagFilter` | 移除 `<think...</think*>` 标签 |
| `ReasoningContentFilter` | 控制推理内容可见性 |
| `WhitespaceFilter` | 清理多余空白 |
| `ChainedContentFilter` | 链式组合多个过滤器 |

```python
filter = ChainedContentFilter([
    ThinkTagFilter(),
    WhitespaceFilter(),
])
filtered_message = await filter.apply(message)
```

---

## 9. 事件配置 (`core/events.py`)

### EmitterConfig

```python
@dataclass
class EmitterConfig:
    enabled_events: Optional[Set[str]] = None    # 启用的事件（None=全部）
    disabled_events: Set[str] = field(...)        # 禁用的事件（优先级更高）
    content_filter: Optional[Callable] = None     # 模型内容过滤器
    max_tool_result_length: int = 2000            # 工具结果最大长度

    def is_enabled(self, event_name: str) -> bool: ...
    def filter_content(self, content: str) -> str: ...
    def truncate_tool_result(self, result: str) -> str: ...
```

---

## 10. AgentRunHook (`core/hooks.py`)

### 生命周期钩子

```python
class AgentRunHook:
    async def before_turn(self, ctx: AgentContext) -> None:
        """Agent.run() 开始时调用，只调用一次。"""

    async def before_iteration(self, ctx: AgentContext) -> None:
        """每次迭代开始前调用。"""

    async def before_tool_execution(self, ctx: AgentContext, tool_calls: list) -> None:
        """工具执行前调用。"""

    async def after_tool_execution(self, ctx: AgentContext, results: list) -> None:
        """工具执行后调用。"""

    async def after_iteration(self, ctx: AgentContext) -> None:
        """每次迭代结束后调用。"""

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        """Agent.run() 结束后调用，只调用一次。"""

    def finalize_content(self, ctx: AgentContext, content: str | None) -> str | None:
        """最终内容调整。"""
```

### RuntimeContextHook

自动注入到 AgentPipeline / AgentSession，管理 `RuntimeContext` 的 per-turn 生命周期：

- `before_turn`：解析并缓存会话的 `RuntimeContext`，然后清除
- `before_tool_execution`：暂存原始 tool_calls
- `after_tool_execution`：匹配 tool_calls 到结果，记录 `ToolCallRecord`

---

## 11. ExecutionStrategy (`core/strategy.py`)

```python
class ExecutionStrategy(ABC):
    async def execute(self, agent, context, emitter) -> AgentResult: ...

class ReActStrategy(ExecutionStrategy):
    """ReAct 执行策略（默认）。"""

class SingleTurnStrategy(ExecutionStrategy):
    """单轮执行策略（直接 LLM 调用，无迭代循环）。"""
```

---

## 12. InterruptibleRunner (`core/runner.py`)

包装 `Agent.run()` 以支持 graceful cancellation：

```python
class InterruptibleRunner:
    async def run(self, agent, context, emitter) -> AgentResult:
        try:
            return await agent.run(context, emitter)
        except asyncio.CancelledError:
            partial = emitter.get_content() or ""
            return AgentResult(
                content=partial or "Task was cancelled before completion.",
                stop_reason="cancelled",
                messages=await context.history.to_list(),
                partial_content=partial,
            )
```

---

## 13. RuntimeContext (`core/runtime_context.py`)

会话级运行时状态容器，用于在 hooks 和 tools 之间共享数据：

```python
class RuntimeContext(ABC):
    async def clear(self) -> None: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def get(self, key: str, default=None) -> Any: ...
    async def has(self, key: str) -> bool: ...
    async def record_tool_call(self, tool_name, arguments, result) -> None: ...
    async def get_tool_calls(self) -> list[ToolCallRecord]: ...

class RuntimeContextManager(ABC):
    async def get_context(self, session_id, metadata) -> RuntimeContext: ...
```

`ToolCallRecord` 是不可变数据类，记录单次工具调用的名称、参数、结果和时间戳。

---

## 14. Skill 系统 (`core/skills/`)

### 组件

| 模块 | 说明 |
|------|------|
| `SkillManager` | 技能管理器，协调技能发现和构建 |
| `FileSkillSource` | 从文件系统加载 SKILL.md |
| `ProgressiveBuilder` | 渐进式技能提示词构建 |
| `models.py` | Skill 数据模型 |

### 使用方式

```python
source = FileSkillSource(directories=[Path("skills/main")], cache=True)
skill_manager = SkillManager(sources=[source])

# 构建技能提示词
resolution_ctx = ResolutionContext(
    tool_manager=tool_manager,
    runtime_info={"platform": "qq"},
)
skills_prompt = await skill_manager.build_prompt(resolution_ctx)
```

---

## 15. 工具系统 (`tools/`)

### 标准工具

| 工具 | 文件 | 说明 |
|------|------|------|
| `ReadFileTool` | `standard/file_tool.py` | 读取文件内容 |
| `WriteFileTool` | `standard/file_tool.py` | 写入文件 |
| `EditFileTool` | `standard/file_tool.py` | 编辑文件（查找替换） |
| `ListDirTool` | `standard/file_tool.py` | 列出目录内容 |
| `ShellTool` | `standard/shell_tool.py` | 执行 Shell 命令 |

### MCP 集成 (`tools/mcp/`)

通过 MCP Protocol 对接外部工具服务器：

```python
# MCP 客户端管理器
mcp_manager = MCPClientManager(config=mcp_config)
await mcp_manager.initialize()

# 注册 MCP 工具到 ToolManager
for tool in mcp_manager.get_tools():
    tool_manager.register(tool)
```

### 工具辅助模块

| 模块 | 说明 |
|------|------|
| `executor.py` | 工具执行器 |
| `registry.py` | 工具注册表 |
| `toolkit.py` | 工具包 |
| `metadata_parser.py` | 工具元数据解析 |
| `secure_wrapper.py` | 安全包装 |
| `types.py` | 工具类型定义 |
