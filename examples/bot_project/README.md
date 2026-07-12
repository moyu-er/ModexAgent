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

Uses **Pool mode** — multi-agent persistent pools with `MessageBroker` + `AgentMessageBus` routing. Input/Output adapters (QQ, Telegram, WebSocket) are fully decoupled from agent logic.

> [!TIP]
> The fastest way to try it is the **WebUI** — no IM credentials required. IM support is plugin-style: QQ and Telegram ship out of the box, and adding another platform (Discord, Feishu, DingTalk, …) is a single `register_<name>.py` module. **No IM credentials are needed to use the WebUI.**

## Capabilities

| Capability | Description |
|------------|-------------|
| **IM Messaging** | QQ (C2C private chat + group chat, with automatic attachment download) and Telegram (long-polling, text + single media) |
| **WebUI** | Browser-based chat with real-time streaming, multi-conversation sidebar, workspace browser, pool selector |
| **In-Browser Config** | Edit pools, models, MCP servers, skills, and system prompts from the Settings UI — no YAML hand-editing |
| **Multi-Model Switching** | Multiple providers / models in `model.yml`; switch per turn from the chat composer |
| **TodoPanel** | The agent tracks its own task list live; the panel surfaces progress without prompting |
| **Attachments** | Upload files in the WebUI (or QQ auto-download); the agent senses them and can view/download symmetrically, with type/magic/size gating |
| **Rich Rendering** | Markdown, syntax-highlighted code, mermaid diagrams, reasoning blocks, streaming deltas |
| **Session Tree** | Threaded conversation tree with parent/child branches per session |
| **Themes** | Light / dark UI toggle |
| **LLM Dialogue** | Streaming and non-streaming output, supporting 100+ models via OpenAI-compatible APIs |
| **ReAct Execution** | Thought → Action → Observation via the graph-driven engine, with loop detection that exits a runaway loop as a controlled stop |
| **Tool Invocation** | Built-in file/shell tools + MCP dynamic tools + custom tools |
| **Multi-tier Memory** | Session / Archive / Knowledge / UserRetentionBuffer / Pruned / Experience — with configurable scopes (UserScope / GlobalScope / SessionScope) |
| **Self-Learning** | ExperienceReviewAgent turns conversations into reusable EXPERIENCE.md knowledge; Dream Engine consolidates archives into long-term memory |
| **Context Governance** | ToolChainRepair + Microcompact + TokenBudget auto-optimization |
| **Tool Approval** | The agent asks before writing/editing outside your project; approve via WebUI or `/approve`. Off by default; opt-in per agent |
| **Multi-Agent Collaboration** | Per-pool star (main agent + subagents via `send_to_agent`) + cross-pool peer messaging between main agents |
| **Skill System** | Dynamic system prompt construction from Markdown skill files (`local_skills/` or bundled by packages) |
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
│ main   │ │ coder  │ │  ...   │  ← AgentPool instances
│  pool  │ │  pool  │ │  pool  │
└────┬───┘ └────┬───┘ └────┬───┘
     │          │          │
     ▼          ▼          ▼
  WebBotEmitter → WebSocket → Browser (streaming deltas)
```

### Pool Mode (IM + WebUI)

For multi-agent persistent collaboration. I/O is fully decoupled from agent logic via the Broker.

```
QQ User / Group      Telegram Chat        Browser (WebUI)
    │                      │                     │
    ▼                      ▼                     ▼
┌─────────────────┐ ┌─────────────────┐ ┌──────────────────┐
│ QQInputAdapter  │ │ TelegramInput   │ │ WebSocketInput   │
└────────┬────────┘ │    Adapter      │ │    Adapter       │
         │          └────────┬────────┘ └────────┬─────────┘
         │                   │                   │
         ▼                   ▼                   ▼
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
         ├──────────────────┬─────────────────────┐
         ▼                  ▼                     ▼
┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
│ QQOutputAdapter  │ │TelegramOutput    │ │ WebBotEmitter    │
│ (QQ replies)     │ │Adapter           │ │ (WebSocket deltas)│
└──────────────────┘ └──────────────────┘ └──────────────────┘
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
# Timezone for timestamps
TIMEZONE=Asia/Shanghai

# MCP server credentials
MCP_BEARER_TOKEN=your_modelscope_bearer_token
MINIMAX_MCP_API_KEY=your_minimax_api_key
```

> [!NOTE]
> **IM credentials do not live in `.env`.** QQ and Telegram credentials live in
> `config/im.yml` (one section per platform) — see *Configuration Reference → IM
> Adapters*. Model settings (model / api_key / base URL / capabilities) also do
> **not** live in `.env`; they live in `config/model.yml` — see the next step.

#### 3. Configure the Model

The model is configured in `config/model.yml` (the single source of truth —
copy it from `config/model.example.yml`). Run the interactive wizard with
`modexbot model`, or edit it by hand. It holds multiple providers, each with
their own models; `default_provider` + `default_model` is what a pool uses
unless you switch per turn in the WebUI:

```yaml
default_provider: "DeepSeek"
default_model: "deepseek-v4-flash"
max_context_tokens: 200000
providers:
  - key: deepseek
    name: "DeepSeek"
    url: https://api.deepseek.com
    api_key: your_api_key            # literal value, gitignored — not an ${ENV} ref
    models:
      - name: "deepseek-v4-flash"
        model: openai/deepseek-v4-flash
        capabilities: [text]
        temperature: 0.7
        max_output_tokens: 50000
```

All pools share this single model config. `config/bot_config.yml` and
`config/pools/*.yml` do **not** carry an `llm:` block.

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

The built-in React frontend (Geist-inspired, warm dark palette) is the fastest way to use the bot — no IM credentials required. Open `http://localhost:21800/webui/` after `modexbot start`.

- **Real-time streaming** — agent output renders incrementally with typing animation
- **Multi-conversation sidebar + session tree** — switch between conversations; each is fully isolated, with parent/child branches per session
- **Workspace browser** — browse and switch project directories via the UI
- **Pool selector** — choose which agent pool handles each new conversation
- **History replay** — past conversations load from the transcript store

#### In-browser configuration

Edit everything from the Settings UI — no YAML hand-editing, no restart dance (changes that need a restart ask for one):

| Tab | What you edit |
|-----|---------------|
| **Pools** | Create/rename pools, add subagents, tool presets, approval, system prompts |
| **Models** | Providers and models (`default_provider` / `default_model` + per-provider model list) |
| **MCP** | Add/rename/remove MCP servers, manage keys |
| **Skills** | Browse skills with their origin (`local_skills/` vs bundled) |

<img src="../../assets/webui-settings-pools.png" alt="Settings — Pools" width="860">

<img src="../../assets/webui-settings-model.png" alt="Settings — Models" width="860">

<img src="../../assets/webui-settings-mcp.png" alt="Settings — MCP" width="860">

<img src="../../assets/webui-settings-skills.png" alt="Settings — Skills" width="860">

#### Per-turn model switching

Pick provider + model in the chat composer's selector before each message. Models are defined once in `model.yml` and shared across pools.

#### TodoPanel

When the agent breaks its work into steps, a side panel tracks the live task list — so you can see progress and catch when it drifts, without prompting it.

#### Rich rendering

Markdown, syntax-highlighted code, **mermaid diagrams**, and reasoning blocks all render inline.

#### Themes

Light / dark toggle in the sidebar.

### Input Pipeline (Converged Message Processing)

All user messages — from IM (QQ, Telegram) and WebUI — flow through a shared pipeline before reaching the agent. Stages **claim or pass through**: a stage that recognises an input handles it (control commands terminate; skills/approval claim-and-continue), and a stage that doesn't recognise it leaves the envelope untouched. A single terminal `UnsupportedCommand` stage rejects any slash command no stage claimed, with one generic notice — so command recognition and rejection live in one place, not scattered across stages. The IM pipeline runs 10 stages, WebUI 8 (no environment/session control — the browser has GUI equivalents), in this order:

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

See `config/pools/default/pool.yml` and `config/pools/coder/pool.yml` for live examples. In chat, reply `/approve` or `/deny`; in the WebUI, click the button on the approval card. (Approval never applies to subagents.)

<img src="../../assets/webui-approval.png" alt="Tool approval" width="860">

### Multi-Agent Collaboration

The main agent delegates tasks to specialist subagents and gathers their replies. It picks the right subagent for each job, and the whole conversation — including the subagents' work — shows up in one place. Subagents don't talk to each other directly; everything flows through the main agent, so it's easy to follow.

<img src="../../assets/webui-multiagent.png" alt="Multi-agent collaboration" width="860">

### Self-Deployment

The agent connects to a remote server via SSH, runs `git pull`, and restarts its own service — demonstrating the depth of the interactive terminal:

<img src="../../assets/self_deployment.png" alt="Self-deployment via terminal" width="860">

### Attachments

Files flow in symmetrically and the agent is aware of them (ADR-0013):

- **WebUI upload** — attach files in the composer; the agent sees them and can pick a tool to read/view them, and you can download them back.
- **QQ auto-download** — image/file attachments in IM are fetched automatically.
- **Safety gating** — type + magic-number + size checks (default images ≤ 20 MB, other files ≤ 10 MB, configurable front- and back-end), with a per-session storage budget (oldest evicted) and an outbound cap.

### Skill System

Skills are auto-discovered from Markdown files (with optional YAML frontmatter `description`) and injected into system prompts. Each skill carries an **origin** — `local` (your `local_skills/` directory, editable) or `bundled` (shipped by a package/plugin):

```
skills/                     # per-pool skills (auto-discovered)
├── main/                   # Main agent skills
│   ├── weather/SKILL.md
│   └── github/SKILL.md
└── subagents/              # Subagent skills (auto-discovered by agent name)
    ├── office-expert/
    └── query-12306/

local_skills/               # project-wide local skills (origin: local)
└── huashu-design/SKILL.md
```

The WebUI **Skills** tab shows every skill and its origin.

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

Main-agent example (`config/pools/default/pool.yml`):

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

## Pools & Workspaces

### Pools

A **pool** is a self-contained agent deployment: one **main agent** plus zero or more **subagents** that collaborate in a star topology (subagents talk only to the main agent, never to each other). Pools are isolated from each other — each carries its own agents, system prompts, tools, memory, and sessions. Main agents of different pools can talk to each other as peers via `send_to_agent` (cross-pool messaging), so a task in one pool can ask a specialist in another pool for help.

On disk, a pool is a directory under `config/pools/` — **the directory name is the pool identity**:

```
config/pools/
├── default/                # pool name = directory name
│   ├── pool.yml            # main agent config (max_steps, tools, approval, …)
│   └── templates/          # subagent templates — one .yml each
│       └── office-expert.yml
└── coder/
    ├── pool.yml
    └── templates/          # this pool's subagents
```

- The **main agent name** defaults to the directory name (override with `main_agent_name` in `pool.yml`).
- **Subagents** are `templates/*.yml` and register automatically — the main agent hands work to them via `send_to_agent`.
- Choose which pool handles a conversation from the WebUI pool selector (or `/pool_name` in IM).

The bundled `default` and `coder` pools are examples — use them as-is, inspect them, or replace them with your own.

### Workspaces

A **workspace** is the live working directory a pool operates in — file tools, terminal, and per-pool resources are anchored to it. Multiple workspaces can be live at once with per-pool isolation; switch the active workspace from the WebUI workspace browser (or `/cd <path>` in IM). Pools and workspaces are orthogonal: any pool can run in any workspace.

## Customizing Pools & Agents

The fastest path is the **WebUI → Settings → Pools** tab: create/rename pools, add subagents, pick a tool preset, toggle approval, and edit system prompts — then apply and restart when prompted. Everything you edit there is persisted to the same `config/pools/<name>/pool.yml` + `templates/*.yml` you could edit by hand.

### Tool presets

A subagent's tool set is summarized by a **preset** (what it's allowed to do):

| Preset | Read | Write | Edit | List | Search | Find | Bash | Terminal |
|--------|:----:|:-----:|:----:|:----:|:------:|:----:|:----:|:--------:|
| `full` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅* |
| `read_write` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — |
| `read_only` | ✅ | — | — | ✅ | ✅ | ✅ | ✅ | — |
| `minimal` | ✅ | ✅ | — | ✅ | ✅ | — | — | — |

`*` Terminal tools require `use_terminal: true`. Subagents always use `SubprocessTool` for bash (stateless).

### By hand (YAML)

A subagent template at `config/pools/<pool>/templates/my-agent.yml`:

```yaml
name: "my-agent"
max_steps: 60
tool_preset: read_write        # read_only / read_write / full / minimal
extra_tools: []                # optional extra tool names on top of the preset
system_prompt: |
  You are a specialized agent for …
  Reply to the main agent via send_to_agent (target_agent="main").
skills:
  roots:
    - "skills/subagents/my-agent"
```

Restart the service (or save in the WebUI) and the agent registers automatically — the main agent can then delegate to it.

## Adapting to Other IM Platforms

`BotService` is a generic base class, not bound to any platform. QQ and Telegram are the two bundled adapters, and both follow the exact same plug-and-play pattern — so adding a new platform (Discord, Feishu, DingTalk, …) is the same process that produced them:

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

3. (Optional) Declare a typed config section in `bot/config/domains/im.py` via `register_kind`, and add a matching section to `config/im.yml`.

4. Restart the service. `WebUIService` automatically discovers and imports every `bot/adapters/register_*.py` module, so **no changes to `WebUIService` are required**.

The `ChannelRouterOutputAdapter` guarantees that slash-command replies from one platform never leak to another — each emitter is channel-filtered, and WebUI acts as a universal observer that records every conversation regardless of origin.

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

### IM Adapters

IM credentials live in `config/im.yml` (gitignored — it holds secrets). Copy `config/im.example.yml` to get started. Each platform is one top-level section; an adapter reads only its own section and is skipped entirely when `enabled: false`.

```yaml
# QQ — get App ID and Secret from https://q.qq.com/
qq:
  enabled: true
  app_id: "your_qq_app_id"
  secret: "your_qq_secret"
  allow_from:
    - "*"                        # "*" allows everyone, or list user/group ids

# Telegram — get a token from @BotFather
telegram:
  enabled: true
  token: "your_telegram_bot_token"
  proxy: null                    # optional, e.g. "http://127.0.0.1:7890"
  allow_from:
    - "*"
```

Both `qq` and `telegram` are registered as typed config kinds in `bot/config/domains/im.py`; secrets are masked on read. To add another platform, see *Adapting to Other IM Platforms*.

### LLM (Models)

Models live in `config/model.yml` — the single source of truth (see *Quick Start → Configure the Model*). Any OpenAI-compatible provider works; configure multiple providers and switch per turn in the WebUI. Edit with `modexbot model` or the WebUI **Models** tab.

### Memory

```yaml
memory:
  session:
    max_messages: 150
    max_context_tokens: 100000
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
