<p align="center">
  <img src="../../assets/logo-wordmark-dark.svg" alt="ModexAgent" width="300">
</p>

<p align="center">
  <strong>ModexAgent QQ Bot 示例 — 全栈 Agent 应用</strong>
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

本项目是 ModexAgent 框架的**生产级示例**，展示如何构建一个支持 LLM 对话、工具调用、MCP 集成、四级记忆、多 Agent 协作和插件扩展的 QQ 机器人。

通过 **Pipeline** 和 **Pool** 双模式运行时，覆盖从单 Agent 长运行服务到多 Agent 常驻协作的全场景。

> [!TIP]
> QQ Bot 平台只是众多适配器之一。同样的架构可以接入 Discord、飞书、钉钉、Telegram、CLI 或任何实现了 `InputAdapter`/`OutputAdapter` 的平台。

## 能力一览

| 能力 | 说明 |
|------|------|
| **QQ 消息收发** | C2C 私聊 + 群聊，支持附件（图片/文件）自动下载 |
| **LLM 对话** | 流式/非流式输出，支持 OpenAI 兼容接口的 100+ 模型 |
| **ReAct 执行** | Thought → Action → Observation 图驱动循环 |
| **工具调用** | 内置文件/Shell 工具 + MCP 动态工具 + 自定义工具 |
| **四级记忆** | Short-term / Archive / Knowledge / UserRetentionBuffer |
| **Dream 引擎** | 离线记忆整合，定期将历史归档压缩为长期知识 |
| **上下文治理** | ToolChainRepair + Microcompact + TokenBudget 自动优化 |
| **工具审批** | 可中断执行，支持分级策略（NORMAL / HARDLINE / PENDING） |
| **多 Agent 协作** | 主 Agent + 多个常驻 Subagent，星型拓扑通信 |
| **技能系统** | 从 Markdown 文件动态构建系统提示词 |
| **插件系统** | 动态扩展工具、记忆提供者和技能来源 |
| **Slash 指令** | `/approve`、`/deny`、`/continue` 及技能触发指令 |
| **双模式运行** | Pipeline（单 Agent）/ Pool（多 Agent 常驻池） |
| **自主部署** | Agent 通过 SSH 连接远程服务器，拉取代码并重启自身服务 |

## 架构概览

### Pipeline 模式

适合单 Agent 长运行服务。链路最短，延迟最低。

```
QQ 用户 / 群聊
    │
    ▼
┌─────────────────┐
│ QQInputAdapter  │  ← 接收消息 + 附件下载
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│ AgentPipeline                                   │
│  ┌─────────────┐  ┌─────────┐  ┌─────────────┐ │
│  │ContextManager│→│ReActAgent│→│ ToolManager │ │
│  │ (记忆/上下文) │  │(图引擎)  │  │(工具调度)   │ │
│  └─────────────┘  └────┬────┘  └─────────────┘ │
│                        │                       │
│                 ┌──────┴──────┐                │
│                 │QQBotEmitter │                │
│                 │(缓冲/发送)  │                │
│                 └──────┬──────┘                │
│  Hooks / Interceptors / Control / Approval     │
└────────────────────────┼───────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────┐
│ SessionPrefixStripAdapter → QQOutputAdapter     │
│ 发送回复到 QQ（支持 C2C / 群聊 + 文件上传）     │
└─────────────────────────────────────────────────┘
```

### Pool 模式（默认）

适合多 Agent 常驻协作。Input/Output 与 Agent 逻辑完全解耦，通过 Broker 路由消息。

```
QQ 用户 / 群聊
    │
    ▼
┌─────────────────┐
│ QQInputAdapter  │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│ BrokerBridgeService                             │
│ 原生适配器 ↔ MessageBroker 桥接                  │
└────────┬────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│ MessageBroker                                     │
│  ┌─────────┐  ┌─────────┐  ┌─────────────────┐  │
│  │AgentPool│  │Subagent │  │ BrokerOutput    │  │
│  │(常驻Agent)│  │Manager  │  │   Adapter       │  │
│  └─────────┘  └─────────┘  └─────────────────┘  │
│                                                  │
│  ┌───────────────────────────────────────────┐  │
│  │ AgentMessageBus (InboxProducer/Consumer)  │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│ BrokerBridgeService → QQOutputAdapter           │
└─────────────────────────────────────────────────┘
```

### 模式对比

| 维度 | Pipeline | Pool |
|------|----------|------|
| **适用场景** | 单 Bot 长运行 | 多 Agent 常驻协作 |
| **核心组件** | `AgentPipeline` | `AgentPool` + `BrokerBridgeService` + `AgentMessageBus` |
| **子 Agent 分发** | `SubagentManager(local)` 直接 `asyncio.create_task` | `SubagentManager(queued)` 通过 `AgentMessageBus` 排队 |
| **消息路由** | Pipeline 内部直接处理 | 原生适配器 → Broker → Agent → Broker → 输出适配器 |
| **状态隔离** | 单一 Pipeline 状态 | 每个常驻 Agent 独立状态（persistent/ephemeral/shared） |
| **切换方式** | `python bot_service.py --mode pipeline` | `python bot_service.py --mode pool`（默认） |

## 快速开始

### 1. 安装依赖

在仓库根目录创建虚拟环境并安装全部 extras：

```bash
cd /path/to/ModexAgent

# 创建虚拟环境
uv venv --python 3.12

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# 安装完整依赖（包含 terminal、gateway、skills、MCP 等）
uv pip install -e ".[all,dev]"
```

> [!IMPORTANT]
> `terminal` extra 是交互式 Shell 工具（bot 中的 `shell` 工具）必需的。Windows 上安装 `pywinpty`；Linux/macOS 上安装 `pexpect` 和 `libtmux`。

### 2. 配置环境变量

```bash
cd examples/bot_project
cp .env.example .env
# 编辑 .env 填写真实值
```

`.env` 关键字段：

```env
# QQ Bot 凭证（从 https://q.qq.com/ 获取）
QQ_APP_ID=your_qq_app_id
QQ_SECRET=your_qq_bot_secret

# LLM 提供者（支持任何 OpenAI 兼容接口）
LLM_API_KEY=your_llm_api_key
LLM_BASE_URL=https://api.minimaxi.com/v1
LLM_MODEL=openai/MiniMax-M2.5

# MCP 服务凭证
MCP_BEARER_TOKEN=your_modelscope_bearer_token
MINIMAX_MCP_API_KEY=your_minimax_api_key
```

### 3. 配置 Bot 设置

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
  base_url: "${LLM_BASE_URL}"
  model: "${LLM_MODEL}"
  temperature: 0.7
  max_tokens: 80000

mcp:
  enabled: true
  config_file: "mcp.json"
```

`config/mcp.json` 需要按你的 MCP 服务器环境配置。

### 4. 运行

**一键启动（推荐）：**

```bash
# 任意目录 — 后台运行，关闭终端不影响
python examples/bot_project/scripts/botctl.py restart
```

脚本会先停止已有 bot，再以脱离子进程启动新的，写入 PID 后立即返回。

停止 bot：

```bash
python examples/bot_project/scripts/botctl.py stop
```

使用 `--help` 查看所有选项：

```bash
python examples/bot_project/scripts/botctl.py --help
```

**手动启动（调试用）：**

```bash
# Pool 模式（默认，多 Agent 协作）
python bot_service.py

# Pipeline 模式（单 Agent）
python bot_service.py --mode pipeline
```

## 核心特性详解

### 工具审批

敏感工具调用时，ReAct 图引擎自动挂起，渲染审批提示等待用户确认。拒绝时支持级联取消或返回错误继续循环：

<img src="../../assets/approval.jpg" alt="工具审批" width="800">

### 多 Agent 协作

主 Agent 通过 `send_to_agent` 同步唤醒子 Agent，或通过 `send_to_agent_async` 异步投递到 inbox。`list_communication_targets` 动态注入当前可见子 Agent 列表，帮助 LLM 判断联系目标：

<img src="../../assets/office_subagent.jpg" alt="多 Agent 协作" width="800">

### 自主部署

Agent 通过 SSH 连接远程服务器，执行 `git pull` 并重启自身服务 —— 展示了深度交互式终端能力：

<img src="../../assets/self_deployment.png" alt="通过终端自主部署" width="800">

### 四级记忆系统

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ Short-term  │ → │   Archive   │ → │  Knowledge  │    │ UserRetention│
│  短期会话   │    │  历史归档   │    │ 长期知识    │    │   Buffer    │
│  按 Session │    │  按 User    │    │ SOUL.md     │    │  防止过度压缩│
│  分组       │    │  分组       │    │ USER.md     │    │             │
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
       │
       ▼  Governance: ToolChainRepair + Microcompact + TokenBudget
```

- **Short-term**：当前会话近期对话历史，超限后自动清理
- **Archive**：压缩后的历史归档，由 Consolidator 处理
- **Knowledge**：长期知识文件（SOUL.md / USER.md / MEMORY.md）
- **UserRetentionBuffer**：额外保留缓冲，防止治理压缩过度丢失关键上下文
- **Dream Engine**：离线定期将 Archive 整合为 Knowledge

### 技能系统

技能从 Markdown 文件自动发现并注入系统提示词：

```
skills/
├── main/                    # 主 Agent 技能（自动发现）
│   ├── weather/SKILL.md
│   └── github/SKILL.md
└── subagents/               # Subagent 技能（按 agent name 自动发现）
    ├── office-expert/
    └── query-12306/
```

### Slash 指令

| 指令 | 说明 |
|------|------|
| `/approve` | 批准待审批的工具调用 |
| `/deny` | 拒绝待审批的工具调用 |
| `/continue` | 继续对话，不将指令本身加入上下文 |
| `/weather 上海明天天气` | 技能指令，自动注入对应 SKILL.md |

### 治理系统

自动修复和优化上下文，无需人工干预：

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
```

## 添加新 Subagent

1. 在 `config/bot_config.yml` 的 `agents:` 中添加配置：

```yaml
agents:
  - name: "my-new-agent"
    role: subagent
    system_prompt: |
      你是一个...的 Agent。
      完成后必须通过 send_to_agent_async 将结果回复给主 Agent（target_agent="main"）
    tools:
      file_tools:
        enabled: true
      shell_tools:
        enabled: false
      mcp_tools:
        enabled: false
    skills:
      roots:
        - "skills/subagents/pdf"
```

2. （可选）创建专属技能目录 `skills/subagents/my-new-agent/`，放入 `SKILL.md`

3. 重启服务，新 subagent 自动注册到 `AgentPool`

## Agent 能力矩阵

| Agent | 文件 | Shell | MCP | 通信工具 | Skills |
|-------|:----:|:-----:|:---:|----------|--------|
| **main** | ✅ | ✅ | ✅（全部） | `send_to_agent`, `send_to_agent_async`, `list_communication_targets` | `skills/main/*` |
| **office-expert** | ✅ | ✅ | — | `send_to_agent_async`(→main), `list_communication_targets` | docx/pdf/pptx/xlsx |
| **query-12306** | ✅ | ✅ | ✅（12306-mcp, fetch） | `send_to_agent_async`(→main), `list_communication_targets` | — |
| **helper-sync** | ✅ | ✅ | — | —（spawn 同步返回） | `skills/subagents/*` |

## 适配其他 IM 平台

`BotService` 是通用基类，不绑定 QQ。只需提供对应的 `InputAdapter`、`OutputAdapter` 和 `Emitter` 工厂即可：

```python
from framework import AgentPipeline
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
    async def send(self, message, session_id):
        # 发送 Discord 消息
        ...

# 像 QQ 示例一样接入 BotService
```

## 配置参考

### QQ Bot

从 [QQ 开放平台](https://q.qq.com/) 获取 App ID 和 Secret。

```yaml
qq:
  app_id: "${QQ_APP_ID}"
  secret: "${QQ_SECRET}"
  sandbox: false
  allow_from:
    - "*"                        # "*" 表示允许所有人
```

### LLM

支持任何 OpenAI 兼容的 API：

```yaml
llm:
  api_key: "your-api-key"
  base_url: "https://api.openai.com/v1"
  model: "openai/gpt-4o"
  temperature: 0.7
  max_tokens: 80000
```

### 记忆

```yaml
memory:
  main:
    short_term:
      max_messages: 50
      max_tokens: 100000
      keep_ratio_for_messages: 0.4
    long_term:
      enabled: true
    dream_engine:
      enabled: true
      interval: 300
      threshold: 5
    governance:
      enabled: true
      tool_chain_repair: true
      microcompact:
        enabled: true
        keep_recent: 10
      token_budget:
        enabled: true
        budget_ratio: 0.5
        safety_buffer: 1024
```

### MCP

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

### Subagent 工具

每个 subagent 通过 `tools` 字段独立配置工具集：

```yaml
tools:
  file_tools:
    enabled: true
  shell_tools:
    enabled: true
    timeout: 60
    enable_safety_guard: false
  mcp_tools:
    enabled: true
    server_filter:
      - "12306-mcp"
```

## 插件系统

插件动态扩展工具、记忆提供者和技能来源，无需修改核心代码：

```yaml
plugins:
  enabled: true
  configurations:
    tool_call_cleanup:
      enabled: true                   # 清理冗余的工具调用记录
```

## 日志

日志文件位于 `logs/bot.log`，包含：
- 消息收发记录
- 工具调用记录
- LLM 调用记录
- Agent 间通信记录
- 错误日志

## 相关文档

- [ModexAgent 框架文档](../../README.zh-CN.md)
- [ModexAgent Framework (English)](../../README.md)
- [AGENTS.md](../../AGENTS.md)
- [docs/bot-guide.md](../../docs/bot-guide.md)
