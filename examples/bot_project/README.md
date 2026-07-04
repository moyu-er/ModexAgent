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
| **Tool Approval** | The agent asks before writing/editing outside your project; approve via WebUI or `/approve`. Off by default; opt-in per agent |
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
│              Input Pipeline (WebUI, 8 stages)        │
│  SetChannel → ResolveWorkspace → ResolvePool →       │
│  Approval → Skill → Unsupported → Persist → Enqueue  │
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
│         Input Pipeline (claim / pass-through)        │
│  IM:    SetChannel→ResolveWs→EnvCtrl→SessCtrl→       │
│         ResolvePool→Approval→Skill→Unsupported→      │
│         Persist→Enqueue                              │
│  WebUI: SetChannel→ResolveWs→ResolvePool→Approval→   │
│         Skill→Unsupported→Persist→Enqueue            │
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
| Virtual environment | Creates virtual environment at repo root (`../../.venv`) with `uv venv --python 3.12` (Python downloaded automatically by uv) |
| Python dependencies | Installs the full framework (`..\..\.[all,dev]`) and bot CLI (`.[webui,dev]`) |
| Environment file | Copies `.env.example` → `.env` if `.env` doesn't exist |
| `modexbot install` | Runs config wizard (checks `config/model.yml`) + builds WebUI frontend via `npm run build` |
| **PATH registration** | Prompts to add the venv `Scripts`/`bin` directory to your **system-wide PATH**, so `modexbot` works from any terminal — no activation needed |

> [!NOTE]
> Both scripts are **idempotent** — re-running skips already-complete steps. They cache the `pyproject.toml` hash so Python dependencies are only reinstalled when project requirements change. Missing prerequisites trigger interactive y/n prompts. **You can run the scripts from any directory** — they locate the project via their own file path.

After the script completes:

```bash
# Works from ANY directory, ANY shell — no activation required
modexbot start
```

Then open `http://localhost:21800/webui/` in your browser.

Common commands (all shell-agnostic after PATH setup): `modexbot stop` \| `modexbot logs -f` \| `modexbot install -f` \| `modexbot config`

> [!TIP]
> If you skipped the PATH step, you can still run commands via the venv Python directly:
> - Windows: `..\..\.venv\Scripts\python.exe -m modexbot start`
> - Linux/macOS: `../../.venv/bin/python -m modexbot start`

> [!NOTE]
> **Self-healing installs**: the install scripts now **stop the running bot before reinstalling dependencies** and run a post-install integrity check (`import aiohttp._cookie_helpers`); if it fails they trigger a clean reinstall. This avoids the Windows failure where reinstalling a package the bot imports while it is running corrupts the install (typical symptom: `No module named 'aiohttp._cookie_helpers'`, crash on startup).
> Manual recovery if ever needed: **stop the bot** (`modexbot stop`), delete the root `.venv`, and re-run `install.bat` / `install.sh`.
> On cross-filesystem setups (uv cache on C:, venv on another drive) the root `pyproject.toml` sets `[tool.uv] link-mode = "copy"`, forcing copy over hardlink so extraction can't be left half-done.

---

### Option B: Manual Setup

#### 1. Install Dependencies

Install `uv` and `Node.js` if you don't have them already, then:

```bash
cd /path/to/ModexAgent

# Create virtual environment at repo root (uv downloads Python 3.12 automatically)
uv venv --python 3.12

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# Install framework
uv pip install -e ".[all,dev]"

# Install bot project (registers the 'modexbot' CLI)
cd examples\bot_project
uv pip install -e ".[webui,dev]"
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

# MCP server credentials
MCP_BEARER_TOKEN=your_modelscope_bearer_token
MINIMAX_MCP_API_KEY=your_minimax_api_key
```

> [!NOTE]
> Model settings (model / api_key / base URL / capabilities) do **not** live in
> `.env`. They live in `config/model.yml` — see the next step.

#### 3. Configure the Model

The model is configured in `config/model.yml` (the single source of truth —
copy it from `config/model.example.yml`). Edit it interactively with
`modexbot config`, or by hand:

```yaml
model:
  url: https://api.minimaxi.com/v1
  api_key: your_llm_api_key      # literal value, gitignored — not an ${ENV} ref
  model: openai/MiniMax-M2.5
  capabilities: [text, image]
  temperature: 0.7
  max_output_tokens: 50000
```

All pools inherit this global config. `config/bot_config.yml` and
`config/pools/*.yml` no longer carry an `llm:` block.

#### 4. Run

**One-click start (recommended):**

```bash
# Build WebUI frontend + config wizard, then start the bot
modexbot install
modexbot start
```

The `install` command checks your `config/model.yml` (offering to run the config wizard if needed) and builds the WebUI frontend (`npm run build`). It skips the build if the frontend is already up-to-date — use `-f` to force rebuild. The `start` command launches the bot as a detached background process.

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

All user messages — from IM (QQ) and WebUI — flow through a shared pipeline before reaching the agent. Stages **claim or pass through**: a stage that recognises an input handles it (control commands terminate; skills/approval claim-and-continue), and a stage that doesn't recognise it leaves the envelope untouched. A single terminal `UnsupportedCommand` stage rejects any slash command no stage claimed, with one generic notice — so command recognition and rejection live in one place, not scattered across stages. The IM pipeline runs 10 stages, WebUI 8 (no environment/session control — the browser has GUI equivalents), in this order:

| Stage | IM | WebUI | Purpose |
|-------|:--:|:-----:|---------|
| SetChannel | ✅ | ✅ | Tag conversation with originating channel (runs first, so notices route to the right adapter) |
| ResolveWorkspace | ✅ | ✅ | Resolve and anchor the live workspace root |
| EnvironmentControl | ✅ | — | `/cd`, `/pool`, `/exit`, `/pwd` |
| SessionControl | ✅ | — | `/stop` turn cancellation |
| ResolvePool | ✅ | ✅ | Resolve pool + agent, persist session→pool |
| Approval | ✅ | ✅ | Claim `/approve` · `/deny` into a structured approval decision |
| SkillParse | ✅ | ✅ | Validate `/skillName`, convert to XML; pass through if unknown |
| UnsupportedCommand | ✅ | ✅ | Terminal stage: reject any unclaimed `/command` with one generic notice |
| PersistUserMessage | ✅ | ✅ | Write to transcript store (single persistence path) |
| Enqueue | ✅ | ✅ | Build InputMessage, enqueue to agent |

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

The agent asks for your permission before making potentially risky changes. It watches file writes/edits: changes **inside your project folder** go ahead automatically, but writes **outside the project** (or to sensitive locations) pause and surface an approval prompt — a card in the WebUI, or a message in chat. Approve to let it proceed, deny to stop; the agent picks up exactly where it paused.

Approval is **off by default**. Enable it for a main agent in the pool config:

```yaml
approval:
  enabled: true
  tools:
    write_file: { allowed_paths: ["./*"] }   # auto-allow inside the project, ask elsewhere
    edit_file:  { allowed_paths: ["./*"] }
```

See `config/pools/main.yml` and `coding.yml` for live examples. In chat, reply `/approve` or `/deny`; in the WebUI, click the button on the approval card. (Approval never applies to subagents.)

<img src="../../assets/approval.jpg" alt="Tool approval" width="800">

### Multi-Agent Collaboration

The main agent delegates tasks to specialist subagents and gathers their replies. It picks the right subagent for each job, and the whole conversation — including the subagents' work — shows up in one place. Subagents don't talk to each other directly; everything flows through the main agent, so it's easy to follow.

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

Commands are resolved inside the input pipeline before reaching the agent — `EnvironmentControl`/`SessionControl` claim IM control commands, `Approval` claims `/approve` · `/deny`, `SkillParse` claims `/skillName`, and the terminal `UnsupportedCommand` stage rejects anything unclaimed:

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

Governance runs on the model-visible message copy before each LLM call. It is configured under `memory.governance` in a pool config or subagent template.

Main-agent example (`config/pools/main.yml`):

```yaml
memory:
  session:
    max_messages: 150
    max_context_tokens: 100000
  governance:
    tool_chain_repair: true      # Required: repair orphan/incomplete tool-call groups
    lossy_compaction:
      tool_result_head_chars: 1200
      assistant_head_chars: 1200
      agent_head_chars: 2000
      user_head_chars: 4000
      compact_range_count: 50    # Optional: default 50, min 20
```

Subagent templates (`config/pools/*/templates/*.yml`) should keep governance lightweight:

```yaml
memory:
  session:
    max_messages: 100
  governance:
    tool_chain_repair: true
```

## Adding a New Subagent

The bundled **`coding` pool** (`config/pools/coding.yml` + `config/pools/coding/templates/`) is the reference multi-agent setup. To add your own subagent, mirror that structure:

1. Describe the agent in the pool config (or a subagent template) — its name, what it does, and what it's allowed to do:

```yaml
agents:
  - name: "my-new-agent"
    role: subagent
    max_steps: 60
    system_prompt: |
      You are a specialized agent for ...
      Reply to the main agent via send_to_agent (target_agent="main").
    # What this agent can do is chosen by a tool preset — see coding.yml:
    #   read_only / read_write / full / minimal
    extra_tools: []          # optional extra tool names on top of the preset
    skills:
      roots:
        - "skills/subagents/my-new-agent"
```

2. (Optional) Drop a `SKILL.md` into `skills/<pool>/my-new-agent/` to give it a dedicated skill.

3. Restart the service. The new subagent registers automatically and the main agent can hand work to it.

## Agent Capability Matrix

The **`coding` pool** (`config/pools/coding.yml` + its `templates/`) is the bundled multi-agent example: a `coding` main agent plus a team of subagents — scout, context-builder, planner, worker, reviewer, oracle, delegate — each allowed to do a different subset of work. Every subagent can be reached from the main agent; subagents report back to it.

Each subagent's tool set is summarized by a **preset** (what it's allowed to do):

| Preset | Read | Write | Edit | List | Search | Find | Bash | Terminal |
|--------|:----:|:-----:|:----:|:----:|:------:|:----:|:----:|:--------:|
| `full` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅* |
| `read_write` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| `read_only` | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | — |
| `minimal` | ✅ | ✅ | — | ✅ | ✅ | — | — | — |

`*` Terminal tools require `use_terminal: true`. Subagents always use `SubprocessTool` for bash (stateless). See the coding pool config for the full agent roster and presets.

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
from modex_agent import AgentPipeline
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter


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
  max_output_tokens: 80000
```

### Memory

```yaml
memory:
  main:
    short_term:
      max_messages: 50
      max_context_tokens: 100000
      keep_ratio_for_messages: 0.4
    long_term:
      enabled: true
    dream_engine:
      enabled: true
      interval: 300
      threshold: 5
    governance:
      tool_chain_repair: true
      lossy_compaction:
        tool_result_head_chars: 1200
        assistant_head_chars: 1200
        agent_head_chars: 2000
        user_head_chars: 4000
        compact_range_count: 50
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
