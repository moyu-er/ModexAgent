<p align="center">
  <img src="../../assets/logo-wordmark-dark.svg" alt="ModexAgent" width="300">
</p>

<p align="center">
  <strong>ModexAgent Bot Example — Full-Stack Agent Application with WebUI</strong>
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

This is a **production-grade example** of the ModexAgent framework. It demonstrates how to build a multi-channel AI assistant with LLM dialogue, tool invocation, MCP integration, multi-tier memory, multi-agent collaboration, self-learning experience system, and a browser-based WebUI.

Uses **Pool mode** — multi-agent persistent pools with `MessageBroker` + `AgentMessageBus` routing. Input/Output adapters (QQ, WebSocket) are fully decoupled from agent logic.

> [!TIP]
> QQ Bot is just one possible adapter. The same architecture works for Discord, Feishu, DingTalk, Telegram, CLI, or any platform with an `InputAdapter`/`OutputAdapter` implementation. **No QQ credentials are needed to use the WebUI.**

## Capabilities

| Capability | Description |
|------------|-------------|
| **QQ Messaging** | C2C private chat + group chat, with automatic attachment download |
| **WebUI** | Browser-based chat with real-time streaming, multi-conversation sidebar, workspace browser, pool selector |
| **LLM Dialogue** | Streaming and non-streaming output, supporting 100+ models via OpenAI-compatible APIs |
| **ReAct Execution** | Thought → Action → Observation via the graph-driven engine |
| **Tool Invocation** | Built-in file/shell tools + MCP dynamic tools + custom tools |
| **Multi-tier Memory** | Session / Archive / Knowledge / UserRetentionBuffer / Pruned / Experience — with configurable scopes (UserScope / GlobalScope / SessionScope) |
| **Self-Learning** | ExperienceReviewAgent turns conversations into reusable EXPERIENCE.md knowledge; Dream Engine consolidates archives into long-term memory |
| **Context Governance** | ToolChainRepair + Microcompact + TokenBudget auto-optimization |
| **Tool Approval** | Interruptible execution with tiered policies (NORMAL / HARDLINE / PENDING) |
| **Multi-Agent Collaboration** | Main agent + persistent subagents, star-topology communication |
| **Skill System** | Dynamic system prompt construction from Markdown skill files |
| **Plugin System** | Dynamically extend tools, memory providers, and skill sources |
| **Slash Commands** | `/approve`, `/deny`, `/continue`, and skill-triggering commands |
| **Pool Runtime** | Multi-agent persistent pools with `MessageBroker` + `AgentMessageBus` routing |
| **Self-Deployment** | Agent connects via SSH to remote servers, pulls code, and restarts itself |

## Architecture

### WebUI Path (Browser → Agent)

```
Browser (React)
    │  WebSocket + REST
    ▼
┌──────────────────────────────────────────────────────┐
│              WebUIServer (aiohttp)                    │
│  /api/sessions, /api/pools, /api/workspace, /ws      │
└────────┬─────────────────────────────────────────────┘
         │  seed UserInputEnvelope
         ▼
┌──────────────────────────────────────────────────────┐
│              Input Pipeline (7-stage)                 │
│  S4 SetChannel → S5 ResolvePool → S6 SkillParse      │
│  → S7 PersistUserMessage → S8 Enqueue                │
└────────┬─────────────────────────────────────────────┘
         │  resolved session + InputMessage
         ▼
┌──────────────────────────────────────────────────────┐
│              PoolRouter                               │
│         session → pool dispatch                       │
└────────┬─────────────────────────────────────────────┘
         │
    ┌────┴─────┬─────────┐
    ▼          ▼         ▼
┌────────┐ ┌────────┐ ┌────────┐
│ main   │ │ coding │ │  ...   │  ← AgentPool instances
│  pool  │ │  pool  │ │  pool  │
└────┬───┘ └────┬───┘ └────┬───┘
     │          │          │
     ▼          ▼          ▼
  WebBotEmitter → WebSocket → Browser (streaming deltas)
```

### Pool Mode (IM + WebUI)

For multi-agent persistent collaboration. I/O is fully decoupled from agent logic via the Broker.

```
QQ User / Group Chat                Browser (WebUI)
    │                                      │
    ▼                                      ▼
┌─────────────────┐              ┌──────────────────┐
│ QQInputAdapter  │              │ WebSocketInput   │
└────────┬────────┘              │    Adapter       │
         │                       └────────┬─────────┘
         │                                │
         ▼                                ▼
┌──────────────────────────────────────────────────────┐
│              Input Pipeline (7-stage convergence)     │
│  IM: S4→S2→S3→S5→S6→S7→S8                          │
│  WebUI: S4→S5→S6→S7→S8                              │
└────────┬─────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────┐
│              PoolRouter                              │
│         session → pool dispatch                      │
└────────┬─────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────┐
│ MessageBroker                                        │
│  ┌─────────┐  ┌─────────┐  ┌─────────────────────┐  │
│  │AgentPool│  │Subagent │  │  BrokerOutput       │  │
│  │(Persistent)│ │Manager  │  │     Adapter         │  │
│  └─────────┘  └─────────┘  └─────────────────────┘  │
│                                                      │
│  ┌───────────────────────────────────────────────┐  │
│  │ AgentMessageBus (InboxProducer/Consumer)      │  │
│  └───────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
         │
         ├──────────────────────────────┐
         ▼                              ▼
┌──────────────────┐          ┌──────────────────┐
│ QQOutputAdapter  │          │ WebBotEmitter    │
│ (QQ replies)     │          │ (WebSocket deltas)│
└──────────────────┘          └──────────────────┘
```

## Quick Start

### Prerequisites

Only **two** runtimes are needed — everything else (including Python 3.12) is managed automatically:

| Runtime | Purpose | How to get it |
|---------|---------|---------------|
| [**uv**](https://docs.astral.sh/uv/) | Python package & version manager | The setup scripts below offer one-click install. Manual: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [**Node.js**](https://nodejs.org/) | WebUI frontend build (optional for backend-only) | The setup scripts offer auto-install (winget/brew/nvm). Manual: [nodejs.org](https://nodejs.org/) |

> No system Python, pip, or npm required. `uv` downloads and manages its own Python 3.12. `node` includes `npm`.

### Option A: One-Click Setup (Recommended)

Run the platform-specific bootstrap script in this directory:

| Platform | Script | How to run |
|----------|--------|------------|
| **Windows** | `install.bat` | Double-click, or run in **any terminal** (cmd, PowerShell, Windows Terminal) |
| **Linux / macOS** | `install.sh` | `chmod +x install.sh && ./install.sh` (works in **any shell**: bash, zsh, fish, etc.) |

Both scripts perform the same automated steps:

| Step | What it does |
|------|-------------|
| Prerequisite checks | Detects `uv` and `Node.js` — offers to install missing ones with y/n prompts (winget on Windows, brew/nvm on macOS/Linux) |
| `uv` installer | Installs via the [official standalone installer](https://docs.astral.sh/uv/) if missing |
| Virtual environment | Creates `.venv` with `uv venv --python 3.12` (Python downloaded automatically by uv) |
| Python dependencies | Installs the full framework (`..\..\.[all,dev]`) and bot CLI (`.[webui,dev]`) |
| Frontend build | Runs `npm install` and `npm run build` to compile the WebUI (skipped if Node.js is unavailable) |
| Environment file | Copies `.env.example` → `.env` if `.env` doesn't exist |
| **PATH registration** | Prompts to add `.venv/bin` (or `.venv\Scripts`) to your **system-wide PATH**, so `modexbot` works from any terminal, any shell — no activation needed |

> [!NOTE]
> Both scripts are **idempotent** — re-running skips already-complete steps. They cache the `pyproject.toml` hash so Python dependencies are only reinstalled when project requirements change. Missing prerequisites trigger interactive y/n prompts.

After the script completes and you restart your terminal:

```bash
# Works from ANY directory, ANY shell — no activation required
modexbot restart
```

Then open `http://localhost:21800/webui/` in your browser.

Common commands (all shell-agnostic after PATH setup): `modexbot stop` \| `modexbot logs -f` \| `modexbot install -f` \| `modexbot config`

> [!TIP]
> If you skipped the PATH step, activate the venv first:
> - Windows: `.venv\Scripts\activate`
> - Linux/macOS: `source .venv/bin/activate`

---

### Option B: Manual Setup

#### 1. Install Dependencies

Install `uv` and `Node.js` if you don't have them already, then:

```bash
cd /path/to/ModexAgent

# Create virtual environment (uv downloads Python 3.12 automatically)
uv venv --python 3.12

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# Install full dependencies (includes terminal, gateway, skills, MCP, etc.)
uv pip install -e ".[all,dev]"
```

> [!IMPORTANT]
> The `terminal` extra is required for the interactive shell tool. On Windows it installs `pywinpty`; on Linux/macOS it installs `pexpect` and `libtmux`.

#### 2. Configure Environment Variables

```bash
cd examples/bot_project
cp .env.example .env
# Edit .env with real values
```

Key fields in `.env`:

```env
# QQ Bot credentials (from https://q.qq.com/) — OPTIONAL for WebUI-only use
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

> [!NOTE]
> If you only want to use the WebUI (no QQ Bot), you only need `LLM_API_KEY`. QQ credentials are optional.

#### 3. Configure Bot Settings

Edit `config/bot_config.yml` (supports `${ENV_VAR}` interpolation):

```yaml
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

#### 4. Run

**One-click start (recommended):**

```bash
# Install/rebuild WebUI frontend, then start the bot in background
modexbot install
modexbot restart
```

The `install` command builds the WebUI frontend (`npm run build`). It skips the build if the frontend is already up-to-date — use `-f` to force rebuild. The `restart` command stops any existing bot, starts a new one as a detached process, and returns immediately.

Then open `http://localhost:21800/webui/` in your browser.

To stop:

```bash
modexbot stop
```

**Manual start (for debugging):**

```bash
# Pool mode (multi-agent collaboration + WebUI)
python bot_service.py
```

## Core Feature Details

### WebUI

The built-in React frontend provides:

- **Real-time streaming** — agent output renders incrementally with typing animation
- **Multi-conversation sidebar** — switch between conversations; each is fully isolated
- **Workspace browser** — browse and switch project directories via the UI
- **Pool selector** — choose which agent pool handles each new conversation
- **History replay** — past conversations load from the transcript store

### Input Pipeline (Converged Message Processing)

All user messages — from IM (QQ) and WebUI — flow through a shared 7-stage pipeline before reaching the agent. This convergence guarantees consistent handling of control commands, skill parsing, pool resolution, persistence, and enqueuing across every channel:

| Stage | Name | IM | WebUI | Purpose |
|-------|------|:--:|:-----:|---------|
| S2 | EnvironmentControl | ✅ | — | `/cd`, `/pool`, `/exit`, `/pwd` |
| S3 | SessionControl | ✅ | — | `/stop` turn cancellation |
| S4 | SetChannel | ✅ | ✅ | Tag conversation with originating channel |
| S5 | ResolvePool | ✅ | ✅ | Resolve pool + agent, persist session→pool |
| S6 | SkillParse | ✅ | ✅ | Validate `/skillName`, convert to XML |
| S7 | PersistUserMessage | ✅ | ✅ | Write to transcript store (single persistence path) |
| S8 | Enqueue | ✅ | ✅ | Build InputMessage, enqueue to agent |

### Multi-tier Memory System

```
┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
│ Session   │  │  Archive  │  │ Knowledge │  │UserRetention│ │  Pruned   │  │Experience │
│ Per-sess. │→ │ Per-user  │→ │ SOUL.md   │  │  Buffer    │  │Per-sess.  │  │EXPERIENCE │
│ (auto-clean)│ │(compressed)│ │ USER.md   │  │ (prevents  │  │(catalog)  │  │.md files  │
└───────────┘  └───────────┘  └───────────┘  │over-compact)│ └───────────┘  └───────────┘
                                             └───────────┘
```

- **Session**: Recent conversation history for the current session, auto-cleanup on overflow
- **Archive**: Compressed historical records processed by the Consolidator, shared across sessions for the same user
- **Knowledge**: Long-term knowledge files (SOUL.md / USER.md / MEMORY.md)
- **UserRetentionBuffer**: Extra retention buffer preventing over-aggressive governance compaction
- **Pruned**: Catalog of pruned messages stored per-session for injection reference
- **Experience**: Self-learned reusable reference knowledge from past conversations (EXPERIENCE.md)
- **Dream Engine**: Offline periodic consolidation of archives into knowledge
- **ExperienceReviewAgent**: Reviews conversations and creates/updates EXPERIENCE.md files
- **Configurable Scopes**: SessionScope / UserScope / GlobalScope — archive and knowledge can be per-user or global

### Self-Learning (Experience System)

The bot learns from every conversation:

1. After a conversation ends, `ExperienceReviewAgent` analyzes the interaction
2. It extracts reusable patterns, solutions, and knowledge
3. Creates or updates `EXPERIENCE.md` files in the experience directory
4. On future conversations, relevant experiences are injected into the system prompt
5. Experiences are scope-aware — per-user with UserScope, shared globally with GlobalScope

### Tool Approval

When an agent invokes a sensitive tool, the ReAct graph engine automatically suspends, renders an approval prompt, and waits for user confirmation. Rejection supports cascade cancellation or error-resume:

<img src="../../assets/approval.jpg" alt="Tool approval" width="800">

### Multi-Agent Collaboration

The main agent distributes tasks to subagents via `send_to_agent` (async inbox-based). The tool description dynamically shows all available targets so the LLM can decide who to contact:

<img src="../../assets/office_subagent.jpg" alt="Multi-agent collaboration" width="800">

### Self-Deployment

The agent connects to a remote server via SSH, runs `git pull`, and restarts its own service — demonstrating the depth of the interactive terminal:

<img src="../../assets/self_deployment.png" alt="Self-deployment via terminal" width="800">

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

Commands are processed by the input pipeline (S2/S3 for control, S6 for skills) before reaching the agent:

| Command | Description |
|---------|-------------|
| `/approve` | Approve pending tool invocation |
| `/deny` | Deny pending tool invocation |
| `/continue` | Continue dialogue without injecting the command into context |
| `/cd <path>` | Change workspace directory (IM only) |
| `/pool_name` | Switch to a different agent pool (IM only) |
| `/stop` | Cancel the running turn (IM only) |
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
| **main** | ✅ | ✅ | ✅ (all) | `send_to_agent`, `send_to_agent_async` | `skills/main/*` |
| **office-expert** | ✅ | ✅ | — | `send_to_agent_async`(→main) | docx/pdf/pptx/xlsx |
| **query-12306** | ✅ | ✅ | ✅ (12306-mcp, fetch) | `send_to_agent_async`(→main) | — |
| **helper-sync** | ✅ | ✅ | — | — (spawn sync return) | `skills/subagents/*` |

## Adapting to Other IM Platforms

`BotService` is a generic base class, not bound to QQ. Adding a new platform (Discord, Feishu, DingTalk, Telegram, etc.) is plug-and-play:

1. Create `bot/adapters/<platform>.py` with three classes:
   - `<Platform>InputAdapter` — subclass of `InputAdapter`, receives messages and produces seed `UserInputEnvelope` for the input pipeline.
   - `<Platform>OutputAdapter` — subclass of `OutputAdapter`, sends replies back to the platform.
   - `<Platform>Emitter` — subclass of `StreamingAwareEmitter` or `ContentEmitter`, converts agent events into platform messages.
   - Override `configure_input_pipeline()` only if the pipeline is held externally (see `WebSocketInputAdapter`); otherwise inherit the ABC default.

2. Create `bot/adapters/register_<platform>.py` and decorate a build function with `@register`:

```python
from bot.adapters.channels import AdapterBuildContext, register

@register("discord", enabled=True)
def build_discord(ctx: AdapterBuildContext):
    from bot.adapters.discord import DiscordInputAdapter, DiscordOutputAdapter, DiscordEmitter

    cfg = ctx.raw_config.get("discord", {})
    if not cfg.get("enabled"):
        return None  # Skip if not configured

    discord_input = DiscordInputAdapter(...)
    discord_output = DiscordOutputAdapter(discord_input)

    def emitter_factory(session_id: str):
        return DiscordEmitter(
            output_adapter=discord_output,
            session_id=session_id,
            config=...,
        )

    return discord_input, discord_output, emitter_factory
```

3. Restart the service. `WebUIService` automatically discovers and imports every `bot/adapters/register_*.py` module, so **no changes to `WebUIService` are required**.

The same `ChannelRouterOutputAdapter` used for QQ and WebUI guarantees that slash-command replies from one platform never leak to another.

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
