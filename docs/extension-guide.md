# 扩展开发指南

> 本文档讲解如何扩展 Agent Framework V2，包括添加新平台、新工具、新 Agent 类型、MemoryProvider 插件等。

---

## 1. 添加新平台支持

### 1.1 实现 InputAdapter

以 Discord 为例：

```python
from typing import AsyncIterator
from framework.pipeline.adapters import InputAdapter
from framework.core.types import InputMessage

class DiscordInputAdapter(InputAdapter):
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self._message_queue = asyncio.Queue()
        self._bot = None

    @property
    def name(self) -> str:
        return "discord"

    async def start(self) -> None:
        import discord
        
        class DiscordBot(discord.Client):
            def __init__(inner_self):
                intents = discord.Intents.default()
                intents.message_content = True
                super().__init__(intents=intents)
            
            async def on_message(inner_self, message):
                if message.author == inner_self.user:
                    return
                
                input_msg = InputMessage(
                    content=message.content,
                    session_id=str(message.channel.id),
                    source="discord",
                    sender_id=str(message.author.id),
                    metadata={
                        "channel_id": message.channel.id,
                        "guild_id": message.guild.id if message.guild else None,
                    }
                )
                await self._message_queue.put(input_msg)
        
        self._bot = DiscordBot()
        asyncio.create_task(self._bot.start(self.bot_token))

    async def stop(self) -> None:
        if self._bot:
            await self._bot.close()

    async def receive(self) -> AsyncIterator[InputMessage]:
        while True:
            msg = await self._message_queue.get()
            yield msg
```

### 1.2 实现 OutputAdapter

```python
from framework.pipeline.adapters import OutputAdapter
from framework.core.types import OutputMessage

class DiscordOutputAdapter(OutputAdapter):
    supports_streaming = False  # Discord 不支持真流式
    
    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self._bot = None
        self._channels = {}
        self._delta_buffers = {}

    async def send(self, message: OutputMessage, session_id: str) -> None:
        import discord
        
        channel = self._get_channel(int(session_id))
        if channel:
            content = message.content or ""
            if message.reasoning:
                content += f"\n\n*Reasoning: {message.reasoning[:200]}...*"
            await channel.send(content[:2000])  # Discord 限制 2000 字符

    async def send_delta(self, delta: str, session_id: str, metadata=None) -> None:
        # 伪流式：缓冲到缓冲区
        if session_id not in self._delta_buffers:
            self._delta_buffers[session_id] = ""
        self._delta_buffers[session_id] += delta

    async def flush_deltas(self, session_id: str) -> None:
        # 合并缓冲内容后一次性发送
        if session_id in self._delta_buffers:
            content = self._delta_buffers[session_id]
            await self.send(
                OutputMessage(content=content),
                session_id
            )
            del self._delta_buffers[session_id]
```

### 1.3 实现 Emitter

```python
from framework.core.emitter import StreamingAwareEmitter
from enum import Enum

class DiscordEvent(Enum):
    MODEL_OUTPUT = "model_output"
    MODEL_REASONING = "model_reasoning"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_COMPLETE = "tool_call_complete"
    ERROR = "error"

class DiscordEmitter(StreamingAwareEmitter[DiscordEvent]):
    async def _on_event(self, event: DiscordEvent, data: Any) -> None:
        event_name = event.value if isinstance(event, Enum) else str(event)
        
        if event_name == "model_reasoning":
            # Discord 可以发送 reasoning 到单独线程
            logger.info(f"[Reasoning] {data}")
        elif event_name == "tool_call_start":
            # 发送 typing indicator
            pass
        
        # 调用基类处理 content
        await super()._on_event(event, data)
```

### 1.4 组装服务

```python
from framework.pipeline.pipeline import AgentPipeline

# 创建适配器
discord_input = DiscordInputAdapter(bot_token="...")
discord_output = DiscordOutputAdapter(bot_token="...")

# 创建 Pipeline
pipeline = AgentPipeline(
    agent=agent,
    context_manager=context_manager,
    tool_manager=tool_manager,
    input_adapter=discord_input,
    output_adapter=discord_output,
    emitter_factory=lambda sid: DiscordEmitter(discord_output, sid),
)

# 运行
await pipeline.run()
```

---

## 2. 添加新工具

### 2.1 继承 Tool 基类

```python
from framework.core.tool_manager import Tool, ToolResult, ToolConfig, ToolExecutionMode

class WeatherTool(Tool):
    def __init__(self, api_key: str):
        super().__init__(
            name="get_weather",
            description="获取指定城市的天气信息",
            parameters={
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，如 '北京'、'Shanghai'"
                    },
                    "days": {
                        "type": "integer",
                        "description": "预报天数",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 7
                    }
                },
                "required": ["city"]
            },
            config=ToolConfig(
                timeout=30.0,
                execution_mode=ToolExecutionMode.ASYNC,
                retry_count=2,
            )
        )
        self.api_key = api_key
    
    async def execute(self, city: str, days: int = 1) -> dict:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.weather.com/v1/current?city={city}&key={self.api_key}"
            async with session.get(url) as response:
                data = await response.json()
                
        return {
            "city": city,
            "temperature": data["temp"],
            "condition": data["condition"],
            "humidity": data["humidity"],
        }
```

### 2.2 注册到 ToolManager

```python
from framework.core.tool_manager import InMemoryToolManager

tool_manager = InMemoryToolManager()

# 注册工具
tool_manager.register(WeatherTool(api_key="..."))

# 获取工具描述（用于 LLM）
descriptions = tool_manager.get_tool_descriptions()

# 批量执行工具
tool_calls = [
    {"tool_name": "get_weather", "arguments": {"city": "Beijing"}},
    {"tool_name": "get_weather", "arguments": {"city": "Shanghai"}},
]
results = await tool_manager.execute_batch(tool_calls, parallel=True)
```

### 2.3 工具执行模式

```python
from framework.core.tool_manager import ToolExecutionMode

# 顺序执行 - 同步阻塞
config = ToolConfig(execution_mode=ToolExecutionMode.SEQUENTIAL)

# 并行执行 - 在线程池中
config = ToolConfig(execution_mode=ToolExecutionMode.PARALLEL)

# 异步执行 - 协程（推荐）
config = ToolConfig(execution_mode=ToolExecutionMode.ASYNC)
```

---

## 3. 添加新 Agent 类型

### 3.1 继承 Agent 基类

```python
from typing import TypeVar, Generic
from enum import Enum
from framework.core.agent import Agent, AgentContext
from framework.core.emitter import ContentEmitter, AgentResult
from framework.core.events import AgentEvent

# 定义事件枚举
class PlanExecuteEvent(AgentEvent, Enum):
    PLAN_CREATED = "plan_created"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    EXECUTION_COMPLETE = "execution_complete"
    ERROR = "error"

# Plan-Execute Agent 实现
class PlanExecuteAgent(Agent[PlanExecuteEvent]):
    event_enum = PlanExecuteEvent
    
    def __init__(self, provider: LLMProvider, max_steps: int = 10):
        self.provider = provider
        self.max_steps = max_steps
    
    @property
    def name(self) -> str:
        return "plan_execute_agent"
    
    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[PlanExecuteEvent]
    ) -> AgentResult:
        # 1. 创建计划
        plan = await self._create_plan(context)
        await emitter.emit(PlanExecuteEvent.PLAN_CREATED, {"plan": plan})
        
        # 2. 执行计划
        results = []
        for i, step in enumerate(plan.steps):
            if i >= self.max_steps:
                break
            
            await emitter.emit(PlanExecuteEvent.STEP_STARTED, {"step": i, "description": step})
            
            # 执行步骤
            result = await self._execute_step(step, context)
            results.append(result)
            
            await emitter.emit(PlanExecuteEvent.STEP_COMPLETED, {"step": i, "result": result})
        
        # 3. 完成
        final_result = AgentResult(
            content=self._synthesize_results(results),
            stop_reason="completed"
        )
        await emitter.emit(PlanExecuteEvent.EXECUTION_COMPLETE, {"results": results})
        await emitter.emit_complete(final_result)
        
        return final_result
```

---

## 4. 添加自定义 Emitter

### 4.1 继承 ContentEmitter

```python
from framework.core.emitter import ContentEmitter, AgentResult

class WebSocketEmitter(ContentEmitter[ReActEvent]):
    def __init__(self, websocket, session_id: str):
        self.websocket = websocket
        self.session_id = session_id
        self._content_buffer = ""
    
    async def emit_delta(self, delta: str) -> None:
        self._content_buffer += delta
        # 实时发送给 WebSocket
        await self.websocket.send_json({
            "type": "delta",
            "content": delta,
            "session_id": self.session_id
        })
    
    async def emit_content(self, content: str) -> None:
        await self.websocket.send_json({
            "type": "content",
            "content": content,
            "session_id": self.session_id
        })
    
    async def emit_tool_call(self, tool_call: Dict) -> None:
        await self.websocket.send_json({
            "type": "tool_call",
            "tool": tool_call,
            "session_id": self.session_id
        })
    
    async def emit_complete(self, result: AgentResult) -> None:
        await self.websocket.send_json({
            "type": "complete",
            "result": {
                "content": result.content,
                "reasoning": result.reasoning,
                "stop_reason": result.stop_reason
            },
            "session_id": self.session_id
        })
```

---

## 5. 添加 MemoryProvider 插件

MemoryProvider 是框架的主要扩展点，用于增强四层记忆系统。

### 5.1 实现 MemoryProvider

```python
from framework.plugins import MemoryProvider
from framework.memory.core.scope import MemoryContext

class CustomMemoryProvider(MemoryProvider):
    """自定义记忆提供者示例"""
    
    @property
    def name(self) -> str:
        return "custom_provider"
    
    def is_available(self) -> bool:
        """检查依赖是否已安装"""
        try:
            import some_dependency
            return True
        except ImportError:
            return False
    
    async def initialize(self, **kwargs) -> None:
        """初始化 provider"""
        # kwargs 可能包含：
        # - llm_provider: LLM Provider 实例
        # - workspace: 工作目录 Path
        # - config: bot_config.yml 中的插件配置
        self.config = kwargs.get("config", {})
        self.llm_provider = kwargs.get("llm_provider")
        # 初始化连接等
    
    async def shutdown(self) -> None:
        """清理资源"""
        pass
    
    async def add(
        self,
        messages: list[dict[str, Any]],
        context: MemoryContext,
    ) -> dict[str, Any]:
        """添加消息到记忆后端"""
        # 处理消息
        return {"status": "ok", "memories": []}
    
    async def search(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """搜索相关记忆"""
        return [
            {"memory": "相关记忆", "score": 0.95, "metadata": {}}
        ]
    
    async def prefetch(
        self,
        query: str,
        context: MemoryContext,
    ) -> str | None:
        """每轮对话前预取相关记忆"""
        # 返回要注入系统提示词的文本块
        return "相关背景信息..."
    
    async def on_pre_compress(
        self,
        messages: list[dict[str, Any]],
        context: MemoryContext,
    ) -> None:
        """压缩前回调，可提取即将被丢弃的消息中的信息"""
        pass
    
    def system_prompt_block(self) -> str:
        """静态系统提示词块"""
        return ""
    
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """暴露给 Agent 的工具 schema"""
        return []
```

### 5.2 注册 MemoryProvider

```python
from framework.memory.system import MemorySystem

memory_system = MemorySystem(...)

# 注册 provider
memory_system.add_provider(CustomMemoryProvider())

# 搜索所有 provider
results = await memory_system.search_memories("query", context, limit=5)

# 预取记忆
prefetch = await memory_system.prefetch_memories("query", context)
```

### 5.3 通过配置加载

在 `bot_config.yml` 中配置：

```yaml
plugins:
  - name: custom_provider
    module: my_plugin.provider
    class: CustomMemoryProvider
    config:
      api_key: "..."
      endpoint: "..."
```

---

## 6. 添加自定义 ContextManager

### 6.1 实现 ContextManager

```python
from framework.core.context import ContextManager, ContextState

class RedisContextManager(ContextManager):
    def __init__(self, redis_client, base_system_prompt: str = ""):
        self.redis = redis_client
        self.base_system_prompt = base_system_prompt
    
    async def load(self, session_id: str) -> ContextState:
        key = f"context:{session_id}"
        data = await self.redis.get(key)
        
        if data:
            parsed = json.loads(data)
            return ContextState(
                system_prompt=parsed.get("system_prompt", self.base_system_prompt),
                history=parsed.get("history", []),
                metadata=parsed.get("metadata", {})
            )
        
        return ContextState(system_prompt=self.base_system_prompt)
    
    async def save(
        self,
        session_id: str,
        user_message: Optional[str],
        assistant_result: Optional[AgentResult],
        metadata: Optional[Dict] = None
    ) -> None:
        key = f"context:{session_id}"
        
        # 先加载现有状态
        state = await self.load(session_id)
        
        # 添加新消息
        if user_message:
            state.history.append({"role": "user", "content": user_message})
        if assistant_result:
            state.history.append({"role": "assistant", "content": assistant_result.content})
        
        # 保存到 Redis
        await self.redis.setex(
            key,
            ttl=3600,  # 1 小时过期
            value=json.dumps({
                "system_prompt": state.system_prompt,
                "history": state.history,
                "metadata": metadata or state.metadata
            })
        )
```

---

## 7. 添加自定义 Provider

### 7.1 实现 LLMProvider

```python
from framework.core.provider import LLMProvider, LLMResponse

class CustomProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
    
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> LLMResponse:
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature or 0.7,
                "max_tokens": max_tokens or 2000
            }
            
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload
            ) as response:
                data = await response.json()
                
                return LLMResponse(
                    content=data["choices"][0]["message"]["content"],
                    tool_calls=self._parse_tool_calls(data),
                    usage=data.get("usage", {})
                )
```

---

## 8. 最佳实践

### 8.1 错误处理

```python
async def execute(self, **kwargs) -> dict:
    try:
        result = await self._do_work(**kwargs)
        return {"success": True, "data": result}
    except aiohttp.ClientError as e:
        # 网络错误
        return {"success": False, "error": f"Network error: {e}"}
    except TimeoutError:
        # 超时
        return {"success": False, "error": "Operation timed out"}
    except Exception as e:
        # 未知错误
        logger.exception("Unexpected error in tool execution")
        return {"success": False, "error": f"Internal error: {e}"}
```

### 8.2 日志记录

```python
import logging

logger = logging.getLogger(__name__)

class MyTool(Tool):
    async def execute(self, **kwargs) -> dict:
        logger.info(f"Executing {self.name} with args: {kwargs}")
        
        start_time = time.time()
        try:
            result = await self._execute(**kwargs)
            logger.info(f"{self.name} completed in {time.time() - start_time:.2f}s")
            return result
        except Exception as e:
            logger.error(f"{self.name} failed: {e}")
            raise
```

### 8.3 配置管理

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class MyAdapterConfig:
    api_key: str
    timeout: float = 30.0
    max_retries: int = 3
    retry_delay: float = 1.0
    proxy: Optional[str] = None

class MyInputAdapter(InputAdapter):
    def __init__(self, config: MyAdapterConfig):
        self.config = config
        self._client = None
    
    async def start(self) -> None:
        self._client = MyClient(
            api_key=self.config.api_key,
            timeout=self.config.timeout,
            proxy=self.config.proxy
        )
```

### 8.4 测试

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_weather_tool():
    # 创建 mock
    mock_response = MagicMock()
    mock_response.json = AsyncMock(return_value={
        "temp": 25,
        "condition": "Sunny",
        "humidity": 60
    })
    
    # 测试工具
    tool = WeatherTool(api_key="test_key")
    
    with patch("aiohttp.ClientSession.get", return_value=mock_response):
        result = await tool.execute(city="Beijing")
    
    assert result["success"]
    assert result["data"]["city"] == "Beijing"
    assert result["data"]["temperature"] == 25
```

---

## 9. 贡献指南

### 9.1 代码风格

- 遵循 PEP 8
- 使用类型注解
- 编写 docstring
- 保持函数简洁（< 50 行）

### 9.2 提交规范

```
feat: 添加新功能
fix: 修复 bug
docs: 更新文档
refactor: 重构代码
test: 添加测试
chore: 构建/工具变更
```

### 9.3 目录结构

```
my_extension/
├── __init__.py
├── adapter.py          # 平台适配器
├── tools.py            # 自定义工具
├── emitter.py          # 自定义 Emitter
├── provider.py         # MemoryProvider 插件
├── tests/
│   ├── __init__.py
│   ├── test_adapter.py
│   └── test_tools.py
└── README.md
```
