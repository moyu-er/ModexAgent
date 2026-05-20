# QQ Bot 项目 (ModexAgent)

基于 ModexAgent 框架的完整 QQ Bot 实现，支持 LLM 对话、工具调用、MCP 集成、四层记忆系统、多 Agent 协作、插件扩展等功能。

## 项目简介

本项目展示如何使用 ModexAgent 构建一个功能完善的 QQ 机器人。通过整合 LLM 能力和多种工具系统，实现智能对话、文件操作、MCP 工具调用、多 Agent 协作等能力。

**ModexAgent 特点**：
- 积木式架构：`AgentPipeline` 端到端编排，所有组件可插拔替换
- 基于 `InputAdapter` / `OutputAdapter` 抽象，支持任意 IM 平台接入
- `StreamingAwareEmitter` 统一处理流式与非流式输出
- 独立的 `ContextManager`、`ToolManager`、`Agent` 组件，职责清晰
- 插件系统支持动态扩展工具、记忆和技能来源
- 治理系统（Governance）自动修复工具链、控制上下文长度

## 项目结构

```
bot_project/
├── bot/
│   ├── adapters/
│   │   └── qq.py              # QQ 平台 InputAdapter / OutputAdapter 实现
│   ├── service/
│   │   ├── core.py            # BotService 核心：生命周期、初始化、模式切换
│   │   └── builders.py        # Agent/Peer/Subagent 构建器、工具注册
│   ├── tools/
│   │   └── custom.py          # 自定义工具：spawn_subagent、send_file_to_user
│   ├── plugins/
│   │   └── integration.py     # 插件系统集成封装
│   ├── utils/
│   │   └── config_loader.py   # 配置加载工具
│   └── logging.py             # 日志配置
├── config/
│   ├── bot_config.yml         # 主配置文件
│   └── mcp.json               # MCP 服务器配置
├── skills/
│   ├── main/                  # 主 Agent 的技能（自动发现）
│   ├── peers/                 # Peer Agent 的技能（按 agent name 自动发现）
│   └── subagents/             # 通用子 Agent 技能（通过 skill_dirs 引用）
├── plugins/                   # 本地插件目录
│   ├── mem0_memory/           # Mem0 语义记忆插件
│   └── tool_call_cleanup/     # 工具调用清理插件
├── data/                      # 数据目录
│   ├── memory/                # 记忆存储
│   ├── inbox/                 # 消息收件箱（Pool 模式）
│   └── media/                 # QQ 附件下载目录
├── workspace/                 # 工作区（文件工具默认允许目录）
├── bot_service.py             # 主服务入口
├── create_hermes_ppt.py       # 示例脚本：生成 PPT
├── .env.example               # 环境变量模板
└── README.md                  # 本文件
```

## 架构概览

BotService 支持两种运行时模式，可通过 `mode="pipeline"` 或 `mode="pool"` 切换：

### Pipeline 模式

适合单 Agent 长运行服务（QQ Bot、CLI 等）。

```
┌─────────────────────────────────────────────────────────────────┐
│                        QQ 用户 / 群聊                            │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                     QQInputAdapter                              │
│              (接收 QQ 消息 + 附件下载，转为 InputMessage)       │
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
│  Hooks: InboxFlushHook, PeerAutoSendHook, Plugin hooks         │
│  Governance: ToolChainRepair + Microcompact + TokenBudget      │
└──────────────────────────┼──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│              SessionPrefixStripAdapter → QQOutputAdapter        │
│              (发送回复到 QQ，支持 C2C / 群聊 + 文件上传)        │
└─────────────────────────────────────────────────────────────────┘
```

### Pool 模式（默认）

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
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  AgentMessageBus (LocalAgentMessageBus)                 │   │
│  │  ├─ InboxProducer  → 写入 data/inbox/                   │   │
│  │  ├─ InboxConsumer  → 读取并分发给 Agent                 │   │
│  │  └─ InboxPolling   → 定期扫描 inbox 目录                │   │
│  └─────────────────────────────────────────────────────────┘   │
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
│         SessionPrefixStripAdapter → QQOutputAdapter             │
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
| **切换方式** | `mode="pipeline"` | `mode="pool"`（默认） |

**何时选择 Pipeline？**
- 只需要一个主 Agent 处理所有对话
- 追求启动简单、链路短、延迟低
- 子 Agent 使用 `spawn_and_wait` 同步调用即可满足需求

**何时选择 Pool？**
- 需要多个 Agent 常驻并相互通信
- 子 Agent 任务量大，需要排队和异步结果回传
- 希望 Input/Output 与 Agent 逻辑完全解耦，通过 Broker 路由

## 核心组件

| 组件 | 说明 |
|------|------|
| **QQInputAdapter** | QQ 消息接收适配器，基于 botpy 实现，支持接收附件（图片/文件）下载到本地 |
| **QQOutputAdapter** | QQ 消息发送适配器，支持 `send_delta` / `flush_deltas`，支持文件上传 |
| **SessionPrefixStripAdapter** | 包装层，自动去除回复中的 "Agent:" 等前缀 |
| **QQBotEmitter** | 基于 `StreamingAwareEmitter` 的事件处理器，负责缓冲/发送/日志 |
| **AgentPipeline** | 端到端流程编排器：Input → Context → Agent → Emitter → Output |
| **ReActAgent** | ReAct 执行循环，支持 Thought → Action → Observation 模式 |
| **LiteLLMProvider** | LLM 调用，支持 OpenAI 兼容接口，可接入 100+ 模型 |
| **ToolManager** | 工具注册与执行管理，支持并行/异步执行模式 |
| **MemorySystem** | 三层记忆架构 (Short-term / History / Long-term) |
| **Governance** | 上下文治理链：ToolChainRepair + Microcompact + TokenBudget |
| **AutoCompact** | 后台自动压缩服务，按空闲阈值扫描并压缩过长的 short-term 记忆 |
| **DreamEngine** | 离线记忆整合引擎，定期将历史记忆归档到长期记忆 |
| **MCPTool** | MCP 工具集成，支持动态加载 MCP 服务器 |
| **PluginSystem** | 插件系统，支持动态加载工具、记忆提供者和技能来源 |
| **AgentMessageBus** | 消息总线，管理 Agent 间的 inbox 消息队列 |
| **SkillManager** | 技能管理，从 Markdown 文件动态构建系统提示词 |

## 效果预览

### 工具审批

敏感工具调用时，ReAct 循环暂停，框架渲染审批提示并等待用户确认或拒绝。拒绝时支持级联取消或返回错误：

<img src="../../assets/approval.jpg" alt="工具审批流程" width="800">

### 多 Agent 协作

主 Agent 通过 `send_message` 将文档生成任务分发给 office-expert peer，peer 完成后通过 `send_message_async` 将结果回复到主 Agent 的 inbox：

<img src="../../assets/office_peer.jpg" alt="多 Agent Peer 协作" width="800">

## 快速开始

### 1. 安装框架依赖

先在仓库根目录按 [根 README 的快速开始](../../README.md#快速开始) 使用 Python 3.12+ 和 `uv` 创建虚拟环境，并安装 `uv pip install -e ".[all]"`；本节只说明 bot_project 自己的配置和启动。

### 2. 配置 `.env`

`.env` 必须在 `examples/bot_project/` 下单独创建并填写真实值；`.env.example` 只是字段参考，不是可直接运行的配置。

macOS / Linux:

```bash
cd examples/bot_project
cp .env.example .env
# 运行前必须编辑 .env
```

Windows PowerShell:

```powershell
cd examples\bot_project
Copy-Item .env.example .env
# 运行前必须编辑 .env
```

编辑 `.env`：

```env
# QQ Bot credentials
QQ_APP_ID=your_qq_app_id
QQ_SECRET=your_qq_bot_secret

# LLM provider (MiniMax / OpenAI / Anthropic / Kimi / etc.)
LLM_API_KEY=your_llm_api_key
LLM_BASE_URL=https://api.minimaxi.com/v1
LLM_MODEL=openai/MiniMax-M2.5

# MCP servers — ModelScope SSE endpoints
MCP_BEARER_TOKEN=your_modelscope_bearer_token

# MiniMax MCP coding tool
MINIMAX_MCP_API_KEY=your_minimax_api_key
```

### 3. 配置 `bot_config.yml` 和 MCP

编辑 `config/bot_config.yml`（支持 `${ENV_VAR}` 环境变量插值）：

```yaml
qq:
  app_id: "${QQ_APP_ID}"
  secret: "${QQ_SECRET}"
  sandbox: false
  allow_from:
    - "*"

llm:
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:-https://api.minimaxi.com/v1}"
  model: "${LLM_MODEL:-openai/MiniMax-M2.5}"
  temperature: 0.7
  max_tokens: 80000

mcp:
  enabled: true
  config_file: "mcp.json"

tools:
  mcp_tools:
    enabled: true
    server_filter:
      - "fetch"
```

`config/mcp.json` 也需要按你的 MCP server 自行配置，当前文件只是示例；`bot_config.yml` 中通过 `mcp.enabled` 和 `mcp.config_file: "mcp.json"` 启用 MCP，并让 `tools.mcp_tools.server_filter` 或 peer 的 `tools.mcp_tools.server_filter` 匹配 `mcp.json` 里的 server key。

### 4. 运行

macOS / Linux:

```bash
cd examples/bot_project

# Default: Pool mode (multi-agent collaboration)
python bot_service.py

# Or explicitly choose a mode
python bot_service.py --mode pool
python bot_service.py --mode pipeline
```

Windows PowerShell:

```powershell
cd examples\bot_project

# Default: Pool mode (multi-agent collaboration)
python bot_service.py

# Or explicitly choose a mode
python bot_service.py --mode pool
python bot_service.py --mode pipeline
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
  app_id: "${QQ_APP_ID}"      # QQ 开放平台应用 ID
  secret: "${QQ_SECRET}"      # QQ 开放平台应用密钥
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
  max_tokens: 80000           # 最大生成 token 数
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
| **Short-term** | 文件 | `SessionScope` | 当前会话近期对话历史 |
| **History** | 文件 | `UserScope` | 用户级别的长期历史归档 |
| **Long-term** | 文件 | `UserScope` | SOUL.md / USER.md / MEMORY.md |

#### 自定义分组策略（多租户 / 多平台）

`MemorySystem` 支持按 `Session`、`User`、`Tenant`、`Agent`、`Global` 以及 `Composite` 组合灵活配置每层的分组维度：

```python
from framework.memory.system import MemorySystem
from framework.memory.layers import MemoryLayerConfigSet, SessionMemoryConfig, ArchiveMemoryConfig, KnowledgeMemoryConfig
from framework.memory.core.scope import CompositeScope, TenantScope, UserScope, SessionScope
from framework.memory.stores.file import FileStorage
from framework.memory.stores.in_memory import InMemoryStorage

file_store = FileStorage(Path("./data/memory"))

layers = MemoryLayerConfigSet(
    session=SessionMemoryConfig(scope=CompositeScope(TenantScope(), UserScope(), SessionScope())),
    archive=ArchiveMemoryConfig(scope=CompositeScope(TenantScope(), UserScope())),
    knowledge=KnowledgeMemoryConfig(scope=CompositeScope(TenantScope(), UserScope())),
)

memory_system = create_memory_system(workspace=Path("./data/memory"), layer_config=layers)
```

内置便捷构造函数：
- `MemorySystem.default_single_user_layers(workspace)` — 单用户桌面场景
- `MemorySystem.default_multi_tenant_layers(workspace)` — 多租户 SaaS 场景

#### 自动压缩 (Auto Compact)

后台服务定期检查空闲会话，自动压缩过长的 short-term 记忆：

```yaml
memory:
  main:
    auto_compact:
      enabled: true
      idle_threshold_seconds: 1800    # 30 分钟无活动则触发
      keep_recent_messages: 8         # 保留最近 8 条消息
      scan_interval: 300              # 扫描间隔（秒）
```

#### 梦境引擎 (Dream Engine)

离线记忆整合，定期将历史归档压缩为长期知识：

```yaml
memory:
  main:
    dream_engine:
      enabled: true
      interval: 300                   # 触发间隔
      threshold: 5                    # 最小历史条目数
```

#### 治理系统 (Governance)

自动修复和优化上下文：

```yaml
memory:
  main:
    governance:
      enabled: true
      tool_chain_repair: true         # 修复断裂的工具调用链
      microcompact:
        enabled: true
        keep_recent: 10               # 保留最近 10 条
      token_budget:
        enabled: true
        budget_ratio: 0.5             # LLM max_tokens 的 50%
        safety_buffer: 1024           # 安全缓冲
```

### MCP 配置

`config/mcp.json` 中的 server、URL、命令和凭据需要按你的环境自行填写，下面只是结构示例：

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./data"]
    },
    "fetch": {
      "type": "sse",
      "url": "https://mcp.api-inference.modelscope.net/.../sse",
      "headers": {
        "Authorization": "Bearer ${MCP_BEARER_TOKEN}"
      }
    }
  }
}
```

### 插件配置

框架支持动态加载插件，每个插件可扩展工具、记忆提供者和技能来源：

```yaml
plugins:
  enabled: true

  configurations:
    tool_call_cleanup:
      enabled: true                   # 清理冗余的工具调用记录

    mem0_memory:
      enabled: false                  # Mem0 语义记忆（需额外依赖）
      workspace: "./data/vector_memory"
      vector_store: "chroma"
      collection_name: "bot_memories"
      embedding_provider: "openai"
      embedding_model: "${MEM0_EMBEDDING_MODEL}"
      embedding_base_url: "${MEM0_EMBEDDING_BASE_URL}"
      embedding_api_key: "${MEM0_EMBEDDING_API_KEY}"
      prefetch_top_k: 5
      search_top_k: 5
```

### 平级 Agent (Peer) 配置

Pool 模式下可配置多个常驻平级 Agent，各自拥有独立的记忆和工具集。示例配置：

```yaml
multi_agent:
  enabled: true
  parent_agent_name: "main"
  peers:
    - name: "office-expert"
      role: "document"
      capabilities: ["document", "office"]
      system_prompt: "..."
      skill_dirs:
        - "skills/peers/docx"
        - "skills/peers/pdf"
        - "skills/peers/pptx"
        - "skills/peers/xlsx"
      tools:
        file_tools:
          enabled: true
        shell_tools:
          enabled: true
          timeout: 60
```

**关键概念**：
- `send_message`：主动唤醒目标 peer 立即处理（同步）
- `send_message_async`：将消息放入目标 peer 的 inbox，等待其下轮处理（异步）
- `view_peer_history`：查看与指定 peer 的最近通信记录（默认最近 5 条），自动排除 tool-call 链
  - `mode="bilateral"`（默认）：包含收发双边记录，适合主动调用 peer 后回顾完整上下文
  - `mode="receiver_only"`：仅包含从 peer 接收到的消息，适合 peer 异步回复后快速查看 inbox

Peer 的 `send_message` 工具描述会**动态注入**当前可见的 peer 列表，帮助 LLM 判断应该联系谁。

### Agent Tool/Skill 配置指南

#### 当前 Agent 能力矩阵

| Agent | 文件 | Shell | MCP | 通信工具 | Skills |
|-------|:----:|:-----:|:---:|----------|--------|
| **main** | ✅ | ✅ | ✅ (全部) | send_message, view_peer_history | skills/main/* (11个) |
| **office-expert** | ✅ | ✅ | — | send_message_async(→main), view_peer_history | skills/peers/docx,pdf,pptx,xlsx |
| **query-12306** | ✅ | ✅ | ✅ (12306-mcp, fetch) | send_message_async(→main), view_peer_history | — |
| **helper-sync** | ✅ | ✅ | — | — (spawn 同步返回) | skills/subagents/* |

#### 如何配置 Agent 的工具

每个 agent（peer / subagent）通过 `tools` 字段独立配置工具集：

```yaml
tools:
  # 文件工具：读写文件、列出目录
  file_tools:
    enabled: true

  # Shell 工具：执行命令
  shell_tools:
    enabled: true
    timeout: 60
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
  - "skills/peers/docx"
  - "skills/peers/pdf"

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
    capabilities: ["capability1"]
    system_prompt: |
      你是一个...的 Agent。
      完成后必须通过 send_message_async 将结果回复给主 Agent（target_agent="main"）
    tools:
      file_tools:
        enabled: true
      shell_tools:
        enabled: false
      mcp_tools:
        enabled: false
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
| `send_file_to_user` | 发送本地文件给用户（QQ 支持文件上传） |

### MCP 工具

MCP 工具通过 `MCPTool` 动态加载，支持：
- 文件系统服务器
- GitHub API
- 数据库操作
- 网页抓取 (fetch)
- 12306 火车票查询
- 自定义 MCP 服务器

### 自定义工具

项目自定义工具：

| 工具 | 功能 |
|------|------|
| `spawn_subagent` | 同步创建子 Agent 并等待结果 |
| `send_file_to_user` | 发送本地文件到当前对话 |

## 插件系统

框架支持通过插件动态扩展功能，无需修改核心代码：

### 内置插件

| 插件 | 功能 | 状态 |
|------|------|------|
| **tool_call_cleanup** | 清理冗余工具调用记录，优化上下文 | 默认启用 |
| **mem0_memory** | Mem0 语义记忆，向量检索增强对话记忆 | 需手动启用 |

### 插件加载机制

```
1. 扫描 plugins/ 目录
2. 读取每个插件的 plugin.yml 配置
3. 根据 enabled 配置决定是否加载
4. 注入工具、记忆提供者、技能来源到对应管理器
5. 收集 hooks 并注入到 Pipeline
```

## 功能特性

- ✅ QQ 消息收发 (C2C 私聊 + 群聊 + 附件接收)
- ✅ LLM 对话 (支持流式/非流式输出)
- ✅ 工具调用 (内置 + MCP + 自定义)
- ✅ 三层记忆系统 (Short-term / History / Long-term)
- ✅ 用户白名单
- ✅ ReAct 执行模式
- ✅ 多平台抽象适配（可扩展 Discord / 飞书 / 钉钉 / Telegram / CLI 等）
- ✅ Skill 系统动态加载
- ✅ 平级 Agent (Peer) 协作与动态发现
- ✅ Peer 通信历史查看 (`view_peer_history`)
- ✅ 自动记忆压缩 (Auto Compact)
- ✅ 离线记忆整合 (Dream Engine)
- ✅ 上下文治理 (Governance)
- ✅ 插件系统 (Plugin System)
- ✅ 文件发送给用户 (`send_file_to_user`)
- ✅ 后台 Agent 安全网 (PeerAutoSendHook)
- ✅ Slash 指令系统 (`/approve`, `/deny`, `/continue`, 技能指令)

## Slash 指令

Bot 支持以 `/` 开头的特殊指令，用于控制对话流程、审批工具调用或触发技能。

### 内置指令

| 指令 | 说明 | 使用场景 |
|------|------|----------|
| `/approve` | 批准待审批的工具调用 | Agent 调用敏感工具后，系统暂停等待用户确认 |
| `/deny` | 拒绝待审批的工具调用 | 不同意 Agent 调用某工具 |
| `/continue` | 继续对话，不将指令本身加入上下文 | 让 Agent 继续生成，避免干扰 |

### 技能指令 (Skill Commands)

非内置指令会被解析为技能调用。框架自动在 `skills/main/` 目录下查找同名技能：

```
/weather 明天上海天气如何
```

如果找到 `skills/main/weather/SKILL.md`，技能内容会被包装为结构化上下文注入对话，帮助 Agent 理解如何调用天气相关工具。

如果指令或技能不存在，Bot 会回复提示：
```
Unknown command: /xxx. No such command or skill is available.
```

### 指令与审批的正交性

- `/approve` 和 `/deny` **仅在存在待审批请求时有效**。无待审批时发送 `/approve`，会收到提示："No pending approval request."
- `/continue` **不会自动拒绝审批**。如果存在待审批请求时发送 `/continue`，会收到提示："A pending approval request exists. Use /approve or /deny first." —— 审批状态保持不变。
- Agent 正在运行中（如 LLM 流式输出期间）发送 slash 指令，Bot 会回复忙提示，而非将指令当作文本注入对话。

### 指令语法规则

- 必须以 `/` 开头且 `/` 必须是第一个字符
- 指令名只能包含小写字母、数字、`-`、`_`
- 指令名与参数之间用空格分隔
- 示例：`/skill-name arg1 arg2`

## 消息处理流程

```
收到消息
    │
    ▼
┌─────────────┐
│ 消息验证    │ ← 白名单检查 + 去重 + 附件下载
└─────────────┘
    │
    ▼
┌─────────────┐
│ InputMessage│ ← QQInputAdapter 封装（含附件路径）
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
    └──→ 生成回复 ──→ QQBotEmitter ──→ SessionPrefixStripAdapter ──→ QQOutputAdapter 发送
```

## 安全配置

```yaml
qq:
  allow_from:           # 白名单用户
    - "123456789"
    - "987654321"
```

## 日志

日志文件位于 `bot/logs/bot.log`，包含：
- 消息收发记录
- 工具调用记录
- LLM 调用记录
- 错误日志
- Agent 间通信记录

## 相关文档

- [ModexAgent 文档](../../CLAUDE.md)
- [AGENTS.md](../../AGENTS.md)
## Current Runtime Status

This example demonstrates full-mode ReAct runtime assembly. The default
interceptor chain contains `ControlDrainInterceptor` and `ToolResultLimitInterceptor`
only. Redundant default `approval:` config has been removed; approval policy
belongs to runtime construction. See `docs/current-runtime.md`.
