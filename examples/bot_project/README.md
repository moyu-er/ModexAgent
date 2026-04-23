# QQ Bot 项目 (ModexAgent)

基于 ModexAgent 框架的完整 QQ Bot 实现，支持 LLM 对话、工具调用、MCP 集成、四层记忆系统等功能。

## 项目简介

本项目展示如何使用 ModexAgent 构建一个功能完善的 QQ 机器人。通过整合 LLM 能力和多种工具系统，实现智能对话、文件操作、MCP 工具调用等能力。

**ModexAgent 特点**：
- 积木式架构：`AgentPipeline` 端到端编排，所有组件可插拔替换
- 基于 `InputAdapter` / `OutputAdapter` 抽象，支持任意 IM 平台接入
- `StreamingAwareEmitter` 统一处理流式与非流式输出
- 独立的 `ContextManager`、`ToolManager`、`Agent` 组件，职责清晰

## 项目结构

```
bot_project/
├── config/
│   ├── bot_config.yml    # 主配置文件
│   └── mcp.json          # MCP 服务器配置
├── utils/
│   ├── __init__.py
│   └── config_loader.py  # 配置加载工具
├── skills/               # Skill 能力目录
├── data/                 # 数据目录 (记忆存储 + MCP 文件系统)
├── logs/                 # 日志目录
├── qq_adapters.py        # QQ 平台 InputAdapter / OutputAdapter 实现
├── bot_service.py     # 主服务入口
└── README.md             # 本文件
```

## 架构概览 (V2)

BotService 支持两种运行时模式，可通过 `mode="pipeline"`（默认）或 `mode="pool"` 切换：

### Pipeline 模式（默认）

适合单 Agent 长运行服务（QQ Bot、CLI 等）。

```
┌─────────────────────────────────────────────────────────────────┐
│                        QQ 用户 / 群聊                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     QQInputAdapter                              │
│              (接收 QQ 消息，转为 InputMessage)                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     AgentPipeline                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ContextManager│  │  ReActAgent  │  │   ToolManager          │  │
│  │ (记忆/上下文) │→│ (推理/循环)  │→│ (工具执行调度)          │  │
│  └─────────────┘  └──────┬──────┘  └─────────────────────────┘  │
│                          │                                      │
│                   ┌──────┴──────┐                               │
│                   │ QQBotEmitter │                               │
│                   │(事件分发/缓冲)│                               │
│                   └──────┬──────┘                               │
└──────────────────────────┼──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    QQOutputAdapter                              │
│              (发送回复到 QQ，支持 C2C / 群聊)                   │
└─────────────────────────────────────────────────────────────────┘
```

### Pool 模式

适合多 Agent 常驻池场景。主 Agent 注册为常驻代理，通过 `MessageBroker` + `BrokerBridgeService` 桥接原生适配器，子 Agent 使用 `AgentMessageBus` 排队分发。

```
┌─────────────────────────────────────────────────────────────────┐
│                        QQ 用户 / 群聊                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     QQInputAdapter                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  BrokerBridgeService                            │
│            (原生适配器 ↔ MessageBroker 桥接)                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     MessageBroker                               │
│  ┌─────────────┐         ┌─────────────┐  ┌─────────────────┐  │
│  │  AgentPool  │←───────→│  Subagent   │  │  BrokerOutput   │  │
│  │ (常驻 Agent) │         │  Manager    │  │   Adapter       │  │
│  └─────────────┘         └─────────────┘  └─────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  BrokerBridgeService                            │
│            (订阅 agent:main:out topic 并转发)                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    QQOutputAdapter                              │
└─────────────────────────────────────────────────────────────────┘
```

### Pipeline vs Pool 模式对比

| 维度 | Pipeline 模式 | Pool 模式 |
|------|---------------|-----------|
| **适用场景** | 单 Bot 长运行服务（QQ、Discord、CLI） | 多 Agent 常驻协作、动态任务分发 |
| **核心组件** | `AgentPipeline` | `AgentPool` + `BrokerBridgeService` + `AgentMessageBus` |
| **子 Agent 分发** | `SubagentManager(local)` 直接 `asyncio.create_task` | `SubagentManager(queued)` 通过 `AgentMessageBus` 排队 |
| **消息路由** | Pipeline 内部直接处理 | 原生适配器 → Broker → 常驻 Agent → Broker → 输出适配器 |
| **状态保持** | 单一 Pipeline 状态 | 每个常驻 Agent 独立状态，支持 persistent/ephemeral/shared |
| **切换方式** | `mode="pipeline"`（默认） | `mode="pool"` |

**何时选择 Pipeline？**
- 只需要一个主 Agent 处理所有对话
- 追求启动简单、链路短、延迟低
- 子 Agent 使用 `spawn_and_wait` 同步调用即可满足需求

**何时选择 Pool？**
- 需要多个 Agent 常驻并相互通信
- 子 Agent 任务量大，需要排队和异步结果回传
- 希望 Input/Output 与 Agent 逻辑完全解耦，通过 Broker 路由

## V2 核心组件

| 组件 | 说明 |
|------|------|
| **QQInputAdapter** | QQ 消息接收适配器，基于 botpy 实现，继承自 `InputAdapter` |
| **QQOutputAdapter** | QQ 消息发送适配器，继承自 `OutputAdapter`，支持 `send_delta` / `flush_deltas` |
| **QQBotEmitter** | 基于 `StreamingAwareEmitter` 的事件处理器，负责缓冲/发送/日志 |
| **AgentPipeline** | 端到端流程编排器：Input → Context → Agent → Emitter → Output |
| **ReActAgent** | ReAct 执行循环，支持 Thought → Action → Observation 模式 |
| **LiteLLMProvider** | LLM 调用，支持 OpenAI 兼容接口，可接入 100+ 模型 |
| **ToolManager** | 工具注册与执行管理，支持并行/异步执行模式 |
| **MemorySystem** | 四层记忆架构 (Working / Short-term / History / Long-term) |
| **MCPTool** | MCP 工具集成，支持动态加载 MCP 服务器 |

## 快速开始

### 1. 配置

编辑 `config/bot_config.yml`：

```yaml
qq:
  app_id: "YOUR_QQ_BOT_APP_ID"
  secret: "YOUR_QQ_BOT_SECRET"
  sandbox: false
  allow_from:
    - "*"

llm:
  api_key: "YOUR_API_KEY"
  base_url: "https://api.openai.com/v1"
  model: "openai/gpt-4o"
  temperature: 0.7
  max_tokens: 2000
```

### 2. 安装依赖

```bash
pip install qq-botpy pyyaml litellm
```

### 3. 运行

```bash
python bot_service.py
```

## 适配其他 IM 平台

BotService 是通用基类，不绑定任何平台。只需提供对应的 `InputAdapter`、`OutputAdapter` 和 `Emitter` 工厂即可：

```python
from framework import AgentPipeline, InMemoryToolManager
from framework.pipeline.adapters import InputAdapter, OutputAdapter

class DiscordInputAdapter(InputAdapter):
    @property
    def name(self): return "discord"
    async def receive(self):
        # 接收 Discord 消息，yield InputMessage(...)
        ...

class DiscordOutputAdapter(OutputAdapter):
    @property
    def name(self): return "discord"
    @property
    def supports_streaming(self): return False
    async def send(self, message, session_id):
        # 发送 Discord 消息
        ...

def create_discord_service(config_dir: Path) -> BotService:
    config = ConfigLoader(config_dir).load_yaml("bot_config.yml")
    input_adapter = DiscordInputAdapter(token=config['discord']['token'])
    output_adapter = DiscordOutputAdapter(input_adapter)

    def emitter_factory(session_id: str):
        return StreamingAwareEmitter(
            output_adapter=output_adapter,
            session_id=session_id,
        )

    service = BotService(config_dir, input_adapter, output_adapter, emitter_factory)
    service.config = config
    return service
```

## 流式 vs 非流式输出

| 平台 | 输出模式 | 说明 |
|------|----------|------|
| **QQ Bot** | 伪流式 | 网络 API 成本高，`send_delta()` 缓冲内容，`flush_deltas()` 一次性发送 |
| **CLI / WebSocket** | 真流式 | 本地终端支持逐字符输出，可直接调用 `send()` 或 `print()` |

框架通过 `StreamingAwareEmitter` 统一处理这两种模式：

- `streaming=True` + `adapter.supports_streaming=True` → 真流式，逐片段立即发送
- `streaming=False` 或 adapter 不支持流式 → 伪流式，内容缓冲后在段落边界 flush

## 配置说明

### QQ Bot 配置

从 [QQ 开放平台](https://q.qq.com/) 获取 App ID 和 Secret。

```yaml
qq:
  app_id: "YOUR_APP_ID"        # QQ 开放平台应用 ID
  secret: "YOUR_SECRET"       # QQ 开放平台应用密钥
  sandbox: false              # 是否使用沙箱环境
  allow_from:                 # 允许的用户列表
    - "*"                     # "*" 表示允许所有人
```

### LLM 配置

支持任何 OpenAI 兼容的 API：

```yaml
llm:
  api_key: "your-api-key"
  base_url: "https://api.openai.com/v1"
  model: "openai/gpt-4o"
  temperature: 0.7            # 采样温度 (0-2)
  max_tokens: 2000            # 最大生成 token 数
```

### 记忆配置

QQ Bot 使用 `MemorySystem` 四层记忆架构，默认采用**单用户文件存储配置**：

```python
from framework.memory.system import MemorySystem, MemorySystemContextManager

memory_system = MemorySystem(workspace=Path("./data/memory"))
await memory_system.initialize()
context_manager = MemorySystemContextManager(memory_system)
```

#### 默认记忆分层

| 层级 | 存储 | 默认分组维度 | 说明 |
|------|------|--------------|------|
| **Working** | 内存 | `SessionScope` | 当前会话热缓存 |
| **Short-term** | 文件 | `SessionScope` | 当前会话近期对话历史 |
| **History** | 文件 | `UserScope` | 用户级别的长期历史归档 |
| **Long-term** | 文件 | `UserScope` | SOUL.md / USER.md / MEMORY.md |

#### 自定义分组策略（多租户 / 多平台）

`MemorySystem` 支持按 `Session`、`User`、`Tenant`、`Agent`、`Global` 以及 `Composite` 组合灵活配置每层的分组维度：

```python
from framework.memory.system import MemorySystem, LayerConfig
from framework.memory.core.scope import CompositeScope, TenantScope, UserScope, SessionScope
from framework.memory.stores.file import FileStorage
from framework.memory.stores.in_memory import InMemoryStorage

file_store = FileStorage(Path("./data/memory"))

layers = {
    "working": LayerConfig(scope=CompositeScope(TenantScope(), UserScope(), SessionScope()), storage=InMemoryStorage()),
    "short_term": LayerConfig(scope=CompositeScope(TenantScope(), UserScope(), SessionScope()), storage=file_store),
    "history": LayerConfig(scope=CompositeScope(TenantScope(), UserScope()), storage=file_store),
    "long_term": LayerConfig(scope=CompositeScope(TenantScope(), UserScope()), storage=file_store),
}

memory_system = MemorySystem(workspace=Path("./data/memory"), layers=layers)
```

内置便捷构造函数：
- `MemorySystem.default_single_user_layers(workspace)` — 单用户桌面场景
- `MemorySystem.default_multi_tenant_layers(workspace)` — 多租户 SaaS 场景

### MCP 配置

编辑 `config/mcp.json`：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./data"]
    }
  }
}
```

### 平级 Agent (Peer) 配置

Pool 模式下可配置多个常驻平级 Agent，各自拥有独立的记忆和工具集。示例配置：

```yaml
multi_agent:
  enabled: true
  parent_agent_name: "main"
  peers:
    - name: "doc-expert"
      role: "document"
      role_description: "Office document specialist"
      specialties: ["word", "excel", "powerpoint", "pdf"]
      system_prompt: "..."
      context_strategy: "persistent"
      skill_dirs:
        - "skills/subagents/docx"
        - "skills/subagents/pdf"
        - "skills/subagents/pptx"
        - "skills/subagents/xlsx"
      tools:
        file_tools:
          enabled: true
          allowed_directories:
            - "./data"
    - name: "query-12306"
      role: "travel"
      role_description: "China railway ticket query assistant"
      specialties: ["12306", "train_ticket", "itinerary"]
      system_prompt: "..."
      context_strategy: "persistent"
      tools:
        mcp_tools:
          enabled: true
          server_filter: ["12306-mcp"]  # 必须与 mcp.json 中的 server 名一致
```

**关键概念**：
- `send_message`：主动唤醒目标 peer 立即处理（同步）
- `send_message_async`：将消息放入目标 peer 的 inbox，等待其下轮处理（异步）
- `view_peer_history`：查看与指定 peer 的最近通信记录（默认最近 5 条），自动排除 tool-call 链
  - `mode="bilateral"`（默认）：包含收发双边记录，适合主动调用 peer 后回顾完整上下文
  - `mode="receiver_only"`：仅包含从 peer 接收到的消息，适合 peer 异步回复后快速查看 inbox

Peer 的 `send_message` 工具描述会**动态注入**当前可见的 peer 列表及其 specialties，帮助 LLM 判断应该联系谁。

### Agent Tool/Skill 配置指南

#### 当前 Agent 能力矩阵

| Agent | 文件 | Shell | MCP | 通信工具 | Skills |
|-------|:----:|:-----:|:---:|----------|--------|
| **main** | ✅ | ✅ | ✅ (全部) | send_message, view_peer_history | skills/main/* (9个) |
| **doc-expert** | ✅ | ✅ | — | send_message_async(→main), view_peer_history | skill_dirs→docx,pdf,pptx,xlsx |
| **query-12306** | ✅ | — | ✅ (12306-mcp) | send_message_async(→main), view_peer_history | — |
| **helper-sync** | ✅ | ✅ | — | — (spawn 同步返回) | skill_dirs→docx,pdf,pptx,xlsx |
| **helper-async** | ✅ | ✅ | — | — (inbox 异步回传) | skill_dirs→docx,pdf,pptx,xlsx |

#### 如何配置 Agent 的工具

每个 agent（peer / subagent）通过 `tools` 字段独立配置工具集：

```yaml
tools:
  # 文件工具：读写文件、列出目录
  file_tools:
    enabled: true
    allowed_directories:
      - "./data"
      - "./workspace"

  # Shell 工具：执行命令
  shell_tools:
    enabled: true
    timeout: 60
    restrict_to_workspace: true
    enable_safety_guard: false

  # MCP 工具：按 server 过滤，server 名必须与 mcp.json 中的 key 一致
  mcp_tools:
    enabled: true
    server_filter:
      - "12306-mcp"    # ← 对应 mcp.json 中的 "12306-mcp" key
```

**注意事项**：
- `mcp_tools.server_filter` 中的名称必须与 `config/mcp.json` 中的 server key 完全一致
- `mcp_tools.enabled: false` 时不会加载任何 MCP 工具
- 省略 `tools` 配置时，agent 将获得一个空的工具管理器

#### 如何配置 Agent 的技能

通过 `skill_dirs` 字段引用技能目录（相对于项目根目录）：

```yaml
# 方式 1：引用多个技能目录
skill_dirs:
  - "skills/subagents/docx"
  - "skills/subagents/pdf"
  - "skills/subagents/pptx"
  - "skills/subagents/xlsx"

# 方式 2：自动发现（无需配置 skill_dirs）
# 系统会自动搜索 skills/peers/{agent_name}/ 和 skills/subagents/{agent_name}/
# 例如 query-12306 → 自动搜索 skills/peers/query-12306/
```

**技能目录结构**：

```
skills/
├── main/                  # 主 Agent 的技能（自动发现）
│   ├── weather/SKILL.md
│   ├── github/SKILL.md
│   └── ...
├── peers/                 # Peer Agent 的技能（按 agent name 自动发现）
│   └── query-12306/
│       └── travel/SKILL.md
└── subagents/             # 通用技能（通过 skill_dirs 引用）
    ├── docx/SKILL.md
    ├── pdf/SKILL.md
    ├── pptx/SKILL.md
    └── xlsx/SKILL.md
```

#### 如何配置 Agent 间通信

通信工具由框架自动注册，不需要在 config 中手动配置：

| 通信工具 | 谁拥有 | 说明 |
|----------|--------|------|
| `send_message` | main | 同步唤醒指定 peer，等待其处理完并返回结果 |
| `send_message_async` | 所有 peer | 异步发送到 main 的 inbox，不唤醒 main |
| `view_peer_history` | main + 所有 peer | 查看与指定 agent 的最近通信记录 |

**控制通信权限**：

```yaml
multi_agent:
  # send_message 的允许调用者（null = 允许所有）
  allowed_callers: null

  peers:
    - name: "query-12306"
      # 限制此 peer 的工具访问
      denied_tools:
        - "spawn_subagent_sync"
```

#### 添加新 Peer Agent 的步骤

1. 在 `bot_config.yml` 的 `multi_agent.peers` 中添加配置：

```yaml
peers:
  - name: "my-new-agent"
    role: "custom"
    role_description: "What this agent does"
    specialties: ["keyword1", "keyword2"]
    system_prompt: |
      你是一个...的 Agent。
      完成后必须通过 send_message_async 将结果回复给主 Agent（target_agent="main"）
    context_strategy: "persistent"
    memory:
      enabled: true
      short_term:
        max_messages: 30
        budget_ratio: 0.4
    tools:
      file_tools:
        enabled: true
        allowed_directories: ["./data"]
      shell_tools:
        enabled: false
      mcp_tools:
        enabled: false
    # 可选：引用现有技能
    skill_dirs:
      - "skills/subagents/pdf"
```

2. （可选）创建专属技能目录 `skills/peers/my-new-agent/`，放入 SKILL.md

3. 重启服务，新 peer 会自动注册到 AgentPool

## 工具系统

### 内置工具

项目自带以下内置工具：

| 工具 | 功能 |
|------|------|
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件内容 |
| `edit_file` | 编辑文件内容 |
| `list_dir` | 列出目录内容 |
| `shell` | 执行 shell 命令 |

### MCP 工具

MCP 工具通过 `MCPTool` 动态加载，支持：
- 文件系统服务器
- GitHub API
- 数据库操作
- 自定义 MCP 服务器

## 功能特性

- ✅ QQ 消息收发 (C2C 私聊 + 群聊)
- ✅ LLM 对话 (支持流式/非流式输出)
- ✅ 工具调用 (内置 + MCP)
- ✅ 四层记忆系统 (Working / Short-term / History / Long-term)
- ✅ 用户白名单
- ✅ ReAct 执行模式
- ✅ 多平台抽象适配（可扩展 Discord / 飞书 / 钉钉 / Telegram / CLI 等）
- ✅ Skill 系统动态加载
- ✅ 平级 Agent (Peer) 协作与动态发现
- ✅ Peer 通信历史查看 (`view_peer_history`)

## 消息处理流程

```
收到消息
    │
    ▼
┌─────────────┐
│ 消息验证    │ ← 白名单检查 + 去重
└─────────────┘
    │
    ▼
┌─────────────┐
│ InputMessage│ ← QQInputAdapter 封装
└─────────────┘
    │
    ▼
┌─────────────┐
│ 构建上下文  │ ← MemorySystem 加载历史 + Skill 构建系统提示词
└─────────────┘
    │
    ▼
┌─────────────┐
│ ReAct 循环  │ ← Thought → Action → Observation
└─────────────┘
    │
    ├──→ 工具调用 ──→ ToolManager 执行 ──→ 返回结果
    │
    └──→ 生成回复 ──→ QQBotEmitter ──→ QQOutputAdapter 发送
```

## 安全配置

```yaml
qq:
  allow_from:           # 白名单用户
    - "123456789"
    - "987654321"
```

## 日志

日志文件位于 `logs/bot_project.log`，包含：
- 消息收发记录
- 工具调用记录
- LLM 调用记录
- 错误日志

## 相关文档

- [ModexAgent 文档](../../CLAUDE.md)
