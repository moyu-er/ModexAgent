<p align="center">
  <img src="../../assets/logo-wordmark-dark.svg" alt="ModexAgent" width="300">
</p>

<p align="center">
  <strong>ModexAgent QQ Bot Example — Full-Stack Agent Application</strong>
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

This is a **production-grade example** of the ModexAgent framework. It demonstrates how to build a QQ Bot with LLM dialogue, tool invocation, MCP integration, four-layer memory, multi-agent collaboration, and plugin extensions.

Through **Pipeline** and **Pool** dual runtime modes, this example covers the full spectrum from single-agent long-running services to multi-agent persistent collaboration.

> [!TIP]> The QQ Bot platform is just one possible adapter. The same architecture works for Discord, Feishu, DingTalk, Telegram, CLI, or any platform with an `InputAdapter`/`OutputAdapter` implementation.

## Capabilities

| Capability | Description |
|------------|-------------|
| **QQ Messaging** | C2C private chat + group chat, with automatic attachment download |
| **LLM Dialogue** | Streaming and non-streaming output, supporting 100+ models via OpenAI-compatible APIs |
| **ReAct Execution** | Thought → Action → Observation via the graph-driven engine |
| **Tool Invocation** | Built-in file/shell tools + MCP dynamic tools + custom tools |
| **Four-Layer Memory** | Short-term / Archive / Knowledge / UserRetentionBuffer |
| **Dream Engine** | Offline memory consolidation, periodically compressing archives into long-term knowledge |
| **Context Governance** | ToolChainRepair + Microcompact + TokenBudget auto-optimization |
| **Tool Approval** | Interruptible execution with tiered policies (NORMAL / HARDLINE / PENDING) |
| **Multi-Agent Collaboration** | Main agent + persistent subagents, star-topology communication |
| **Skill System** | Dynamic system prompt construction from Markdown skill files |
| **Plugin System** | Dynamically extend tools, memory providers, and skill sources |
| **Slash Commands** | `/approve`, `/deny`, `/continue`, and skill-triggering commands |
| **Dual Runtime Modes** | Pipeline (single-agent) / Pool (multi-agent persistent pool) |
| **Self-Deployment** | Agent connects via SSH to remote servers, pulls code, and restarts itself |

## Architecture

### Pipeline Mode

For single-agent long-running services. Shortest path, lowest latency.

```
QQ User / Group Chat
    │
    ▼
┌─────────────────┐
│ QQInputAdapter  │  ← Receive messages + download attachments
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│ AgentPipeline                                   │
│  ┌─────────────┐  ┌─────────┐  ┌─────────────┐ │
│  │ContextManager│→│ReActAgent│→│ ToolManager │ │
│  │ (Memory/CTX) │  │(Graph)  │  │(Tool Exec)  │ │
│  └─────────────┘  └────┬────┘  └─────────────┘ │
│                        │                       │
│                 ┌──────┴──────┐                │
│                 │QQBotEmitter │                │
│                 │(Buffer/Send)│                │
│                 └──────┬──────┘                │
│  Hooks / Interceptors / Control / Approval     │
└────────────────────────┼───────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────┐
│ SessionPrefixStripAdapter → QQOutputAdapter     │
│ Send replies to QQ (C2C / group + file upload)  │
└─────────────────────────────────────────────────┘
```

### Pool Mode (Default)

For multi-agent persistent collaboration. I/O is fully decoupled from agent logic via the Broker.

```
QQ User / Group Chat
    │
    ▼
┌─────────────────┐
│ QQInputAdapter  │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│ BrokerBridgeService                             │
│ Native adapters ↔ MessageBroker bridge          │
└────────┬────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│ MessageBroker                                     │
│  ┌─────────┐  ┌─────────┐  ┌─────────────────┐  │
│  │AgentPool│  │Subagent │  │ BrokerOutput    │  │
│  │(Persistent)│  │Manager  │  │   Adapter       │  │
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

### Mode Comparison

| Dimension | Pipeline | Pool |
|-----------|----------|------|
| **Use case** | Single-bot long-running | Multi-agent persistent collaboration |
| **Core components** | `AgentPipeline` | `AgentPool` + `BrokerBridgeService` + `AgentMessageBus` |
| **Subagent dispatch** | `SubagentManager(local)` direct `asyncio.create_task` | `SubagentManager(queued)` via `AgentMessageBus` |
| **Message routing** | Internal pipeline handling | Native adapter → Broker → Agent → Broker → Output adapter |
| **State isolation** | Single pipeline state | Per-agent independent state (persistent/ephemeral/shared) |
| **Switch** | `python bot_service.py --mode pipeline` | `python bot_service.py --mode pool` (default) |

## Quick Start

### 1. Install Dependencies

From the repository root, create a virtual environment and install all extras:

```bash
cd /path/to/ModexAgent

# Create virtual environment
uv venv --python 3.12

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# Install full dependencies (includes terminal, gateway, skills, MCP, etc.)
uv pip install -e ".[all,dev]"
```

> [!IMPORTANT]
> The `terminal` extra is required for the interactive shell tool (`shell` tool in the bot). On Windows it installs `pywinpty`; on Linux/macOS it installs `pexpect` and `libtmux`.

### 2. Configure Environment Variables

```bash
cd examples/bot_project
cp .env.example .env
# Edit .env with real values
```

Key fields in `.env`:

```env
# QQ Bot credentials (from https://q.qq.com/)
QQ_APP_ID=your_qq_app_id
QQ_SECRET=your_qq_bot_secret

# LLM provider (any OpenAI-compatible API)
LLM_API_KEY=your_llm_api_key
LLM_BASE_URL=https://api.minimaxi.com/v1
LLM_MODEL=openai/MiniMax-M2.5

# MCP server credentials
MCP_BEARER_TOKEN=your_modelscope_bearer_token
MINIMAX_MCP_API_KEY=your_minimax_api_key
```

### 3. Configure Bot Settings

Edit `config/bot_config.yml` (supports `${ENV_VAR}` interpolation):

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

`config/mcp.json` should be configured for your MCP servers.

### 4. Run

**One-click start (recommended):**

```bash
# Any directory — runs in background, survives terminal close
python examples/bot_project/scripts/botctl.py restart
```

The script stops any existing bot, then starts a new one as a detached process.
It returns immediately — the bot keeps running in the background.

To stop:

```bash
python examples/bot_project/scripts/botctl.py stop
```

Use `--help` to see all options:

```bash
python examples/bot_project/scripts/botctl.py --help
```

**Manual start (for debugging):**

```bash
# Pool mode (default, multi-agent collaboration)
python bot_service.py

# Pipeline mode (single agent)
python bot_service.py --mode pipeline
```

## Core Feature Details

### Tool Approval

When an agent invokes a sensitive tool, the ReAct graph engine automatically suspends, renders an approval prompt, and waits for user confirmation. Rejection supports cascade cancellation or error-resume:

<img src="../../assets/approval.jpg" alt="Tool approval" width="800">

### Multi-Agent Collaboration

The main agent distributes tasks to subagents via `send_to_agent` (sync) or `send_to_agent_async` (async inbox). `list_communication_targets` dynamically injects visible subagent names to help the LLM decide who to contact:

<img src="../../assets/office_subagent.jpg" alt="Multi-agent collaboration" width="800">

### Self-Deployment

The agent connects to a remote server via SSH, runs `git pull`, and restarts its own service — demonstrating the depth of the interactive terminal:

<img src="../../assets/self_deployment.png" alt="Self-deployment via terminal" width="800">

### Four-Layer Memory System

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ Short-term  │ → │   Archive   │ → │  Knowledge  │    │ UserRetention│
│  Session    │    │   History   │    │  Long-term  │    │   Buffer    │
│  Per-session│    │  Per-user   │    │ SOUL.md     │    │  Prevents   │
│             │    │             │    │ USER.md     │    │  over-compaction│
└─────────────┘    └─────────────┘    └─────────────┘    └─────────────┘
       │
       ▼  Governance: ToolChainRepair + Microcompact + TokenBudget
```

- **Short-term**: Recent conversation history for the current session, auto-cleanup on overflow
- **Archive**: Compressed historical records processed by the Consolidator
- **Knowledge**: Long-term knowledge files (SOUL.md / USER.md / MEMORY.md)
- **UserRetentionBuffer**: Extra retention buffer preventing over-aggressive governance compaction
- **Dream Engine**: Offline periodic consolidation of archives into knowledge

### Skill System

Skills are auto-discovered from Markdown files and injected into system prompts:

```
skills/
├── main/                    # Main agent skills (auto-discovered)
│   ├── weather/SKILL.md
│   └── github/SKILL.md
└── subagents/               # Subagent skills (auto-discovered by agent name)
    ├── office-expert/
    └── query-12306/
```

### Slash Commands

| Command | Description |
|---------|-------------|
| `/approve` | Approve pending tool invocation |
| `/deny` | Deny pending tool invocation |
| `/continue` | Continue dialogue without injecting the command into context |
| `/weather Shanghai tomorrow` | Skill command — auto-injects the matching SKILL.md |

### Governance

Auto-repair and optimize context without human intervention:

```yaml
memory:
  main:
    governance:
      enabled: true
      tool_chain_repair: true         # Fix broken tool call chains
      microcompact:
        enabled: true
        keep_recent: 10               # Keep the last 10 messages
      token_budget:
        enabled: true
        budget_ratio: 0.5             # 50% of LLM max_tokens
```

## Adding a New Subagent

1. Add configuration in `config/bot_config.yml` under `agents:`:

```yaml
agents:
  - name: "my-new-agent"
    role: subagent
    system_prompt: |
      You are a specialized agent for ...
      You must reply to the main agent via send_to_agent_async (target_agent="main")
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

2. (Optional) Create a skill directory `skills/subagents/my-new-agent/` with `SKILL.md`

3. Restart the service. The new subagent auto-registers in `AgentPool`.

## Agent Capability Matrix

| Agent | File | Shell | MCP | Communication | Skills |
|-------|:----:|:-----:|:---:|---------------|--------|
| **main** | ✅ | ✅ | ✅ (all) | `send_to_agent`, `send_to_agent_async`, `list_communication_targets` | `skills/main/*` |
| **office-expert** | ✅ | ✅ | — | `send_to_agent_async`(→main), `list_communication_targets` | docx/pdf/pptx/xlsx |
| **query-12306** | ✅ | ✅ | ✅ (12306-mcp, fetch) | `send_to_agent_async`(→main), `list_communication_targets` | — |
| **helper-sync** | ✅ | ✅ | — | — (spawn sync return) | `skills/subagents/*` |

## Adapting to Other IM Platforms

`BotService` is a generic base class, not bound to QQ. You only need to provide the corresponding `InputAdapter`, `OutputAdapter`, and `Emitter` factory:

```python
from framework import AgentPipeline
from framework.pipeline.adapters import InputAdapter, OutputAdapter

class DiscordInputAdapter(InputAdapter):
    @property
    def name(self): return "discord"
    async def receive(self):
        # Receive Discord messages, yield InputMessage(...)
        ...

class DiscordOutputAdapter(OutputAdapter):
    @property
    def name(self): return "discord"
    async def send(self, message, session_id):
        # Send Discord messages
        ...

# Wire them into BotService just like the QQ example
```

## Configuration Reference

### QQ Bot

Get App ID and Secret from [QQ Open Platform](https://q.qq.com/).

```yaml
qq:
  app_id: "${QQ_APP_ID}"
  secret: "${QQ_SECRET}"
  sandbox: false
  allow_from:
    - "*"                        # "*" allows everyone
```

### LLM

Any OpenAI-compatible API:

```yaml
llm:
  api_key: "your-api-key"
  base_url: "https://api.openai.com/v1"
  model: "openai/gpt-4o"
  temperature: 0.7
  max_tokens: 80000
```

### Memory

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

### Subagent Tools

Each subagent independently configures its tool set via the `tools` field:

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

## Plugin System

Plugins dynamically extend tools, memory providers, and skill sources without modifying core code:

```yaml
plugins:
  enabled: true
  configurations:
    tool_call_cleanup:
      enabled: true                   # Clean up redundant tool call records
```

## Logs

Log files are at `logs/bot.log`, containing:
- Message send/receive records
- Tool invocation records
- LLM call records
- Agent communication records
- Error logs

## Related Documentation

- [ModexAgent Framework README](../../README.md)
- [ModexAgent Framework (中文)](../../README.zh-CN.md)
- [AGENTS.md](../../AGENTS.md)
- [docs/bot-guide.md](../../docs/bot-guide.md)
