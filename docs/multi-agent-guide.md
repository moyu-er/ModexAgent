# 多 Agent 协作指南

> 本文档详细讲解 ModexAgent 的多 Agent 协作架构和实现。

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                      Multi-Agent System                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌───────────────┐     ┌───────────────┐     ┌──────────────┐ │
│   │   Parent      │────▶│   Inbox       │◀────│   Subagent   │ │
│   │   Agent       │     │ (LocalFile)   │     │   (Async)    │ │
│   └───────────────┘     └───────────────┘     └──────────────┘ │
│           │                     ▲                              │
│           │ spawn_sync          │ result (via InboxProducer)   │
│           ▼                     │                              │
│   ┌───────────────┐     ┌───────┴───────┐                      │
│   │  Subagent     │     │  MessageBroker│◀── send_message     │
│   │  (Sync/RPC)   │     │  (InMemory)   │                     │
│   └───────────────┘     └───────────────┘                      │
│           │                                                      │
│           ▼                                                      │
│   ┌───────────────┐                                              │
│   │  AgentPool    │  ── 常驻 Agent 池                            │
│   │  (Resident)   │                                              │
│   └───────────────┘                                              │
│                                                                 │
├─────────────────────────────────────────────────────────────────┤
│                      SubagentManager                            │
│   - 生命周期管理 (spawn, spawn_and_wait, cancel)                │
│   - 同步调用：通过 RPCBroker 阻塞等待结果                       │
│   - 异步调用：直接创建后台 Task，完成后写入 Inbox               │
├─────────────────────────────────────────────────────────────────┤
│                      AgentPool                                  │
│   - 常驻 Agent 生命周期管理                                     │
│   - 消息路由与分发                                              │
│   - 状态机管理 (IDLE, WORKING, ERROR, etc.)                     │
├─────────────────────────────────────────────────────────────────┤
│                      AgentFactory                               │
│   - 按描述符动态组装 Agent (pipeline / session / ephemeral)     │
│   - 自动注入 InboxFlushHook（当配置了 inbox_server 时）         │
│   - 支持不同配置、工具集、提示词                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 核心组件

### 2.1 AgentDescriptor — Agent 描述符

定义 Agent 的配置模板：

```python
from dataclasses import dataclass
from framework.multi_agent import AgentAddress, AgentLLMConfig

@dataclass
class AgentDescriptor:
    address: AgentAddress                           # Agent 地址（含 name, capabilities）
    llm_config: AgentLLMConfig                      # LLM 配置
    system_prompt_template: str = ""                # 系统提示词模板
    max_iterations: int = 10                        # ReAct 最大迭代
    max_tools_per_turn: int = 10                    # 每轮最大工具调用数
    allowed_tools: list[str] | None = None          # 白名单工具
    denied_tools: list[str] | None = None           # 黑名单工具
    allowed_skills: list[str] | None = None         # 白名单 Skills
    execution_strategy: str = "react"               # 执行策略
    context_strategy: str = "persistent"            # persistent | ephemeral | shared
    inbox_strategy: str = "default"                 # default | none
    context_manager: Any | None = None              # 可选的上下文管理器
    role_description: str = ""                      # 角色描述
    specialties: list[str] = field(default_factory=list)  # 专长列表
    exposed_to_peers: bool = True                   # 是否对其他 Agent 可见
```

### 2.2 AgentAddress — Agent 地址

用于消息路由的唯一标识：

```python
from framework.multi_agent.address import AgentAddress

address = AgentAddress(
    kind="agent",              # "agent", "user", "channel", "system"
    name="helper",
    role="assistant",          # 可选角色
    capabilities=["analysis", "coding"],  # 能力列表
)
```

### 2.3 AgentState — Agent 状态

```python
class AgentState(Enum):
    INITIALIZING = "initializing"      # 初始化中
    IDLE = "idle"                      # 空闲
    WORKING = "working"                # 工作中
    ERROR = "error"                    # 错误状态
    SHUTTING_DOWN = "shutting_down"    # 关闭中
    SHUTDOWN = "shutdown"              # 已关闭
```

---

## 3. MessageBroker & AgentMessageBus — 消息总线

### 3.1 MessageBroker 接口

底层消息总线抽象：

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

class MessageBroker(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_to(self, recipient: Address, message: BrokerMessage) -> None: ...

    @abstractmethod
    async def publish(self, topic: str, message: BrokerMessage) -> None: ...

    @abstractmethod
    async def broadcast(self, message: BrokerMessage) -> None: ...

    @abstractmethod
    async def register_consumer(self, address: Address) -> None: ...

    @abstractmethod
    async def unregister_consumer(self, address: Address) -> None: ...

    @abstractmethod
    async def consume(self, address: Address) -> BrokerMessage: ...

    @abstractmethod
    def consume_stream(self, address: Address) -> AsyncIterator[BrokerMessage]: ...

    @abstractmethod
    def subscribe(self, topics: list[str]) -> AsyncIterator[BrokerMessage]: ...
```

### 3.2 InMemoryMessageBroker — 内存实现

```python
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.messaging.broker import Address, BrokerMessage

broker = InMemoryMessageBroker()
await broker.start()

# 注册消费者
addr = Address(kind="agent", name="helper")
await broker.register_consumer(addr)

# 发送消息
await broker.send_to(
    addr,
    BrokerMessage(
        payload={"content": "Please analyze this file"},
        sender=Address(kind="agent", name="main"),
    ),
)

# 接收消息
async for msg in broker.consume_stream(addr):
    print(f"Received: {msg.payload}")
```

### 3.3 AgentMessageBus — Agent 消息总线

上层封装，解耦 Inbox 和 MessageBroker：

```python
from framework.multi_agent.bus import LocalAgentMessageBus
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.consumer import InboxConsumer

bus = LocalAgentMessageBus(
    producer=inbox_producer,
    consumer=inbox_consumer,
    broker=broker,  # 可选，用于跨进程唤醒
)

# 发送消息
from framework.multi_agent.envelope import AgentMessageEnvelope
envelope = AgentMessageEnvelope(
    payload={"content": "Hello"},
    sender=AgentAddress(name="agent_a"),
    target=AgentAddress(name="agent_b"),
)
await bus.send("session_id", envelope)

# 消费消息
messages = await bus.consume("session_id", limit=10, block=True)
```

---

## 4. SubagentManager — 子 Agent 管理器

### 4.1 创建与配置

```python
from framework.multi_agent.subagent_manager import SubagentManager, TaskCoordinationConfig

manager = SubagentManager(
    broker=broker,
    agent_factory=factory,
    coordination_config=TaskCoordinationConfig(
        enable_for_subagent=True,
        default_timeout_seconds=180.0,
    ),
    inbox_producer=inbox_producer,  # 异步结果回传用到
)
```

### 4.2 同步调用 — spawn_and_wait

主 Agent 阻塞等待子 Agent 完成。内部通过 `RPCBroker` 实现：

```python
result = await manager.spawn_and_wait(
    parent_address=parent_address,
    descriptor=AgentDescriptor(
        address=AgentAddress(name="analyzer"),
        llm_config=AgentLLMConfig(model="gpt-4o"),
        system_prompt_template="Analyze the following code...",
        allowed_tools=["read_file", "list_dir"],
        max_iterations=10,
    ),
    task_prompt="Please analyze main.py and report issues",
    conversation_id="conv_123",
    timeout=120.0,
)

print(result.content)
```

**适用场景**：需要立即获得结果的任务。

### 4.3 异步调用 — spawn

启动子 Agent 后立即返回，结果通过 **Inbox** 异步传递：

```python
subagent_id = await manager.spawn(
    parent_address=parent_address,
    descriptor=AgentDescriptor(
        address=AgentAddress(name="batch_processor"),
        llm_config=AgentLLMConfig(model="gpt-4o"),
        system_prompt_template="Process all files in the directory...",
    ),
    task_prompt="Process all CSV files in ./data/",
    conversation_id="conv_123",
    timeout=300.0,
)

print(f"Subagent {subagent_id} started")
```

**注意**：子 Agent 完成后会通过 `InboxProducer` 把结果写入 `LocalFileInboxServer`。父 Agent 通过 `InboxFlushHook` 在下一次 turn 或 iteration 前拉取结果。

### 4.4 取消与终止

```python
# 取消特定子 Agent
await manager.cancel(subagent_id)

# 取消会话下的所有子 Agent（默认不取消异步任务）
await manager.cancel_by_session(conversation_id, include_async=False)
```

---

## 5. AgentPool — 常驻 Agent 池

AgentPool 管理常驻（resident）Agent 的生命周期，支持消息路由和状态管理。

### 5.1 创建 AgentPool

```python
from framework.multi_agent.pool import AgentPool

pool = AgentPool(
    broker=broker,
    agent_factory=factory,
    agent_bus=agent_bus,                    # AgentMessageBus 实例
    inbox_consumer=inbox_consumer,          # InboxConsumer 实例
    enable_inbox_polling=True,              # 启用 inbox 轮询
    inbox_poll_interval=10.0,               # 轮询间隔（秒）
)
```

### 5.2 注册常驻 Agent

```python
from framework.multi_agent.descriptor import AgentDescriptor

descriptor = AgentDescriptor(
    address=AgentAddress(name="helper", capabilities=["analysis"]),
    llm_config=AgentLLMConfig(model="gpt-4o"),
    system_prompt_template="You are a helpful assistant.",
)

# 注册常驻 Agent
instance = await pool.register_resident(
    descriptor=descriptor,
    context_manager=context_manager,
    tool_manager=tool_manager,
)
```

### 5.3 启动和停止

```python
# 启动 Pool
await pool.start()

# 停止 Pool
await pool.stop()

# 注销特定 Agent
await pool.unregister("helper")
```

### 5.4 状态查询

```python
# 获取 Agent 状态
state = pool.get_status("helper")  # AgentState.IDLE

# 获取所有 Agent 名称
names = pool.list_agents()

# 获取 Agent 统计信息
stats = pool.get_stats("helper")
```

---

## 6. AgentFactory — Agent 工厂

### 6.1 DefaultAgentFactory — 默认实现

```python
from framework.multi_agent.factory import DefaultAgentFactory

factory = DefaultAgentFactory(
    default_llm_provider=provider,
    default_tool_manager=tool_manager,
    skill_manager=skill_manager,
    inbox_server=inbox_server,  # 配置后会自动注入 InboxFlushHook
)

instance = await factory.create_agent(
    descriptor=descriptor,
    mode="session",  # "pipeline" | "session" | "ephemeral"
    conversation_id="conv_123",
)
```

工厂会根据描述符自动：
1. 创建 `FilteredToolManager`（按 `allowed_tools` / `denied_tools` 过滤）
2. 创建 `AgentSkillManager`（按 `allowed_skills` 过滤）
3. 组装 `ReActAgent`
4. 按 `mode` 创建 `AgentPipeline` 或 `AgentSession`
5. 自动注入 `InboxFlushHook`（当提供了 `inbox_server` 且 `inbox_strategy != "none"`）

---

## 7. 多 Agent 工具

### 7.1 SpawnSubagentTool — 同步子 Agent

```python
from framework.multi_agent.tools import SpawnSubagentTool

tool = SpawnSubagentTool(
    manager=subagent_manager,
    parent_address=parent_address,
    descriptor=helper_descriptor,
)
```

### 7.2 SpawnSubagentAsyncTool — 异步子 Agent

```python
from framework.multi_agent.tools import SpawnSubagentAsyncTool

tool = SpawnSubagentAsyncTool(
    manager=subagent_manager,
    parent_address=parent_address,
    descriptor=batch_descriptor,
)
```

### 7.3 SendMessageTool — Agent 间消息

```python
from framework.multi_agent.tools import SendMessageTool

tool = SendMessageTool(
    broker=broker,
    self_address=parent_address,
    allowed_callers=["helper"],
)
```

---

## 8. Inbox 系统 — 异步结果回传

### 8.1 组件

```python
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.hook import InboxFlushHook

inbox_dir = Path("./data/inbox")
server = LocalFileInboxServer(workspace=inbox_dir)
producer = InboxProducer(server=server)
consumer = InboxConsumer(server=server)
hook = InboxFlushHook(consumer=consumer, agent_name="main")
```

### 8.2 exactly-once 语义

`LocalFileInboxServer` 保证消息只被消费一次：
1. `receive()` 幂等接收：检查 `pending.jsonl` 和 `delivered_ids.json`，重复则忽略
2. `consume()` 原子性消费：把消息从 `pending.jsonl` 中切走，同时将 `message_id` 写入 `delivered_ids.json`
3. `InboxConsumer` 还有一层内存缓存作为安全网

### 8.3 InboxFlushHook — 收件箱注入

在 turn 边界和每次 ReAct 迭代前将 inbox 消息 flush 到上下文中：

```python
hook = InboxFlushHook(
    consumer=consumer,
    agent_name="main",
    max_messages_per_flush=10,
)
```

**注意**：由于 `InboxFlushHook` 只在 `before_turn` / `before_iteration` 时被调用，父 Agent 必须进入新的 turn 才能看到 Subagent 结果。在 `AgentPipeline` 模式下，这意味着通常要等**下一条用户消息**到来。

---

## 9. TaskCoordinator — 任务协调器

TaskCoordinator 提供中央任务管理和策略干预能力。

### 9.1 TaskRecord — 任务记录

```python
from framework.multi_agent.coordinator import TaskRecord

record = TaskRecord(
    task_id="task_123",
    task_type="analysis",
    created_at=time.time(),
    conversation_id="conv_123",
    source_agent="main",
    target_agent="helper",
    status="pending",
)
```

### 9.2 InMemoryTaskCoordinator — 内存实现

```python
from framework.multi_agent.coordinator import InMemoryTaskCoordinator

coordinator = InMemoryTaskCoordinator()

# 注册任务
await coordinator.register_task("task_123", record)

# 更新状态
await coordinator.update_task_status("task_123", "running")

# 绑定策略
from framework.multi_agent.intervention import TimeoutPolicy
policy = TimeoutPolicy(timeout_seconds=60.0)
await coordinator.bind_policy("task_123", policy)

# 撤销任务
await coordinator.revoke_task("task_123")
```

---

## 10. AgentDiscovery — Agent 发现

### 10.1 FileAgentDiscovery — 文件系统发现

```python
from framework.multi_agent.discovery import FileAgentDiscovery
from pathlib import Path

discovery = FileAgentDiscovery(
    agents_dir=Path("./agents"),
    defaults={"llm_config": {"model": "gpt-4o"}},
    filename="AGENT.yaml",
)

# 发现所有 Agent
descriptors = await discovery.discover()
```

### 10.2 AGENT.yaml 配置示例

```yaml
# agents/helper/AGENT.yaml
name: helper
role: assistant
capabilities:
  - analysis
  - coding

llm_config:
  model: gpt-4o
  temperature: 0.7

system_prompt: |
  You are a helpful coding assistant.
  You specialize in Python and TypeScript.

allowed_tools:
  - read_file
  - write_file
  - list_dir

max_iterations: 15
context_strategy: persistent
exposed_to_peers: true
```

---

## 11. 完整示例

### 11.1 主 Agent 配置

```yaml
# bot_config.yml
multi_agent:
  enabled: true
  parent_agent_name: "main"

  subagent_sync:
    enabled: true
    name: "helper-sync"
    system_prompt: |
      你是一个快速执行 Agent，专门处理即时子任务。
      你的响应应该简洁、直接。
    max_iterations: 10
    tools:
      file_tools: { enabled: true }
      shell_tools: { enabled: false }

  subagent_async:
    enabled: true
    name: "helper-async"
    system_prompt: |
      你是一个后台工作 Agent，负责处理复杂子任务。
      你可以使用所有可用工具，包括文件操作和 Shell。
    max_iterations: 20
    tools:
      file_tools: { enabled: true }
      shell_tools: { enabled: true }
```

### 11.2 代码实现

```python
import asyncio
from pathlib import Path
from framework.multi_agent import (
    SubagentManager,
    DefaultAgentFactory,
    AgentDescriptor,
    AgentAddress,
    AgentLLMConfig,
    TaskCoordinationConfig,
    AgentPool,
    LocalAgentMessageBus,
)
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.extensions.llm.litellm_provider import LiteLLMProvider

async def main():
    # 初始化基础设施
    broker = InMemoryMessageBroker()
    await broker.start()

    inbox_dir = Path("./data/inbox")
    inbox_server = LocalFileInboxServer(workspace=inbox_dir)
    inbox_producer = InboxProducer(server=inbox_server)
    inbox_consumer = InboxConsumer(server=inbox_server)
    
    # 创建 AgentMessageBus
    agent_bus = LocalAgentMessageBus(
        producer=inbox_producer,
        consumer=inbox_consumer,
        broker=broker,
    )

    # 创建工厂
    factory = DefaultAgentFactory(
        llm_provider=LiteLLMProvider(model="gpt-4o"),
        default_tool_manager=tool_manager,
        inbox_server=inbox_server,
    )

    # 创建 SubagentManager
    manager = SubagentManager(
        broker=broker,
        agent_factory=factory,
        inbox_producer=inbox_producer,
    )
    await manager.start()

    # 创建 AgentPool
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=agent_bus,
        inbox_consumer=inbox_consumer,
        enable_inbox_polling=True,
    )

    parent_address = AgentAddress(kind="agent", name="main")

    # 注册常驻 Agent
    await pool.register_resident(
        descriptor=AgentDescriptor(
            address=AgentAddress(name="code-analyzer"),
            llm_config=AgentLLMConfig(model="gpt-4o"),
            system_prompt_template="You are a code analyzer.",
            allowed_tools=["read_file", "list_dir"],
        ),
    )

    # 同步调用
    result = await manager.spawn_and_wait(
        parent_address=parent_address,
        descriptor=AgentDescriptor(
            address=AgentAddress(name="analyzer"),
            llm_config=AgentLLMConfig(model="gpt-4o"),
            system_prompt_template="You are a code analyzer.",
            allowed_tools=["read_file", "list_dir"],
            max_iterations=10,
        ),
        task_prompt="Analyze main.py for potential bugs",
        conversation_id="conv_123",
    )
    print(f"Analysis result: {result.content}")

    # 异步调用
    subagent_id = await manager.spawn(
        parent_address=parent_address,
        descriptor=AgentDescriptor(
            address=AgentAddress(name="batch-processor"),
            llm_config=AgentLLMConfig(model="gpt-4o"),
            system_prompt_template="Process all files and generate a summary report.",
            max_iterations=20,
        ),
        task_prompt="Process all CSV files in ./data/ and create summary.json",
        conversation_id="conv_123",
    )
    print(f"Batch processor started: {subagent_id}")
    
    # 启动 Pool
    await pool.start()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 12. 设计模式

### 12.1 父子 Agent 模式

```
Parent Agent (协调者)
    ├── Spawn Subagent A (分析)
    ├── Spawn Subagent B (搜索)
    ├── Wait for results (通过 InboxFlushHook)
    └── Synthesize final answer
```

### 12.2 并行子 Agent 模式

```
Parent Agent
    ├── Spawn Subagent A ──┐
    ├── Spawn Subagent B ──┼──> 并行执行
    ├── Spawn Subagent C ──┘
    └── Aggregate results (通过 Inbox)
```

### 12.3 常驻 Agent 池模式

```
AgentPool
    ├── Resident Agent A (IDLE) ──┐
    ├── Resident Agent B (WORKING)├──> 消息路由
    └── Resident Agent C (IDLE)   ──┘
```

---

## 13. 最佳实践

1. **合理划分职责**：每个子 Agent 应该有明确的单一职责
2. **控制工具集**：子 Agent 只授予必要的工具权限
3. **设置超时**：为子 Agent 设置合理的超时时间
4. **错误处理**：始终处理子 Agent 失败的情况
5. **资源清理**：会话结束时调用 `cancel_by_session(..., include_async=False)` 清理子 Agent
6. **conversation_id 一致性**：确保 `AgentPipeline` / `AgentSession` 注册的 `conversation_id` 与 `SessionMappingRegistry` 的 key 一致
7. **注意 Inbox 延迟**：在 `AgentPipeline` 模式下，异步 Subagent 结果通常要等**下一条用户消息**才能被父 Agent 消费到
8. **使用 AgentPool**：对于需要长期运行的 Agent，使用 `AgentPool` 管理生命周期
9. **状态监控**：定期检查 Agent 状态，处理 ERROR 状态的 Agent
