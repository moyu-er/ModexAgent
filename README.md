<p align="center">
  <img src="assets/logo-wordmark-dark.svg" alt="ModexAgent" width="380">
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-%E2%89%A53.12-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

<p align="center">
  <strong>A modular, composable, production-ready Python Agent framework</strong>
  <br>
  Graph-driven ReAct · Interruptible Approval · Cross-Platform Terminal · Multi-Agent Star Topology · WebUI
</p>

<p align="center">
  <img src="assets/modexagent-intro-en.gif" alt="ModexAgent Intro" width="720">
</p>

ModexAgent is a Python framework for building AI agent applications. It decomposes model inference, tool invocation, memory management, I/O adapters, and multi-agent collaboration into independently evolvable modules. Start with a minimal ReAct agent and gradually expand into a full application with long-term memory, multi-agent coordination, runtime governance, and a browser-based WebUI.

The framework core replaces traditional loops with a **graph-driven execution engine**, supporting mid-execution suspension, approval, and resumption. The runtime uses **Pool mode** — multi-agent persistent pools with `MessageBroker` + `AgentMessageBus` routing. I/O adapters are fully decoupled from agent logic. A **React + WebSocket WebUI** is included in `examples/bot_project/` for real-time multi-conversation chat. The plugin architecture draws inspiration from OpenClaw, with deep customizations for type safety, cross-platform terminal interaction, and inter-agent communication.

> [!NOTE]
> The project is under active development. Core interfaces are stabilizing, and `examples/bot_project/` provides a comprehensive example covering most framework capabilities, including the WebUI frontend.

## Highlights

| Interruptible Approval | Cross-Platform Terminal | Multi-Agent Collaboration |
|:---:|:---:|:---:|
| ![Approval](assets/approval.jpg) | ![Terminal](assets/self_deployment.png) | ![Multi-Agent](assets/office_subagent.jpg) |
| Sensitive tool calls suspend for human approval with tiered cascade policies | Interactive shell with WinPTY/pexpect/tmux; SSH, multi-tab, visible & headless modes | Star-topology subagents via sync wake, async inbox, and isolated spawn |

## Key Features

- **Graph-driven ReAct Engine** — Execution modeled as `Graph[R] + Node[R] + Edge`. Supports `GraphInterrupt` suspension and state-persistent resumption. Naturally suited for approval and breakpoint-resume scenarios.
- **Interruptible Approval System** — When an agent invokes a sensitive tool, execution automatically suspends, persists state via `TurnSnapshot`, and resumes precisely after user confirmation. Tiered policies (NORMAL / HARDLINE / PENDING) and cascade cancellation.
- **Cross-platform Interactive Terminal** — Built-in terminal toolchain with unified interfaces for Windows (WinPTY/ConPTY), Linux, and macOS (pexpect/tmux); visible and headless PTY modes, covered by 248+ unit tests.
- **Star-topology Multi-agent Collaboration** — Main agent as communication hub. Subagents collaborate via `send_to_agent` (sync), `send_to_agent_async` (async inbox), and `spawn_subagent` (isolated). `CommunicationTracker` prevents silent message loss.
- **Pool Runtime** — Multi-agent persistent pools with `MessageBroker` + `AgentMessageBus` routing. I/O adapters are fully decoupled from agent logic.
- **Multi-tier Memory + Self-Learning** — Session, Archive, Knowledge, UserRetentionBuffer, Pruned, and Experience layers with configurable scopes (SessionScope / UserScope / GlobalScope). Dream Engine consolidates archives into knowledge; ExperienceReviewAgent turns conversations into reusable EXPERIENCE.md reference knowledge.
- **Hook + Interceptor Extension System** — Lifecycle hooks (InboxFlush, SubagentAutoSend, ProgressReport) and AOP interceptor chains (ControlDrain, ToolResultLimit) compose orthogonally without core intrusion.
- **Type Safety** — All interfaces use ABCs (zero Protocols), enums replacing raw strings, mypy strict-level checking.
- **Native MCP Integration** — Dynamically load MCP servers (SSE/stdio). `MCPToolAdapter` maps MCP capabilities to framework Tool objects.
- **Browser WebUI** — React + Vite frontend with real-time streaming, multi-conversation sidebar, workspace browser, and pool selector (see `examples/bot_project/`).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│        External Platforms (QQ / CLI / HTTP / WebSocket → WebUI)         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
           ┌─────────────┐                 ┌─────────────┐
           │InputAdapter │                 │OutputAdapter│
           └──────┬──────┘                 └──────▲──────┘
                  │                                  │
                  ▼                                  │
           ┌─────────────────────────────────────────┐
           │           AgentPipeline                 │
           │  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
           │  │Context  │→ │ ReAct   │→ │  Tool   │ │
           │  │Manager  │  │ Agent   │  │Manager  │ │
           │  │(Memory) │  │(Graph)  │  │(Tools)  │ │
           │  └─────────┘  └────┬────┘  └─────────┘ │
           │                    │                   │
           │  Hooks / Interceptors / Control / Approval│
           └────────────────────┼─────────────────────┘
                                │
                                ▼
                       ┌─────────────┐
                       │ MessageBroker│
                       └──────┬──────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌─────────┐    ┌─────────┐    ┌─────────┐
        │AgentPool│    │Subagent │    │ Inbox   │
        │(Persistent)│  │Manager  │    │Server   │
        └─────────┘    └─────────┘    └─────────┘
```

## Quick Start

### Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Install

```bash
git clone git@github.com:moyu-er/ModexAgent.git
cd ModexAgent

# Create virtual environment
uv venv --python 3.12

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# Install full dependencies
uv pip install -e ".[all,dev]"
```

### Run the Example

```bash
cd examples/bot_project
cp .env.example .env
# Edit .env with your QQ_APP_ID, LLM_API_KEY, etc.

# One-click start (Pool mode + WebUI, runs in background)
python -m modexbot restart

# Or run in the foreground for debugging
python bot_service.py
```

Then open the WebUI at `http://localhost:21800/webui/`.

> [!TIP]
> `examples/bot_project/` is a fully functional QQ Bot + WebUI example. See [examples/bot_project/README.md](examples/bot_project/README.md) for details.

## Project Structure

```text
framework/
  core/              # Core abstractions: Agent, Context, Emitter, Provider, Tool
  agents/react/      # ReAct Agent graph engine implementation
  pipeline/          # End-to-end orchestration
  memory/            # Multi-tier memory system + Dream Engine + Governance
  tools/             # Tool registry, execution, terminal system, MCP adapters
  multi_agent/       # Multi-agent collaboration: Pool, MessageBus
  hook/              # Lifecycle hook extension points
  interceptor/       # AOP interceptor chains
  control/           # Runtime control, approval, event bus
  commands/          # Slash command system
  sandbox/           # Sandbox adapters (Subprocess / Docker / E2B)
  security/          # Security policies and approval classifiers
  providers/         # LLM providers (LiteLLM, OpenAI-compatible)
  ioc/               # Typed configuration (Pydantic v2) and factory layer
  runtime/           # Runtime state storage, snapshots, codecs

examples/
  bot_project/       # Full QQ Bot + WebUI example (Pool mode)
  sandbox/           # Sandbox-related examples

tests/               # Unit, integration, and end-to-end tests
docs/                # Framework documentation
```

## Optional Extras

| Extra     | Includes                                                                     | Use Case              |
| --------- | ---------------------------------------------------------------------------- | --------------------- |
| `llm`     | `litellm`, `openai`                                                          | LLM Provider          |
| `sandbox` | `docker`, `e2b-code-interpreter`                                             | Sandbox execution     |
| `gateway` | `qq-botpy`, `aiohttp`                                                        | QQ Bot adapter        |
| `skills`  | `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `pdfplumber`             | Document processing   |
| `terminal`| `pywinpty` (Win), `pexpect` + `libtmux` (Unix)                              | Interactive terminal  |
| `dev`     | `pytest`, `pytest-asyncio`, `ruff`, `mypy`                                   | Development           |
| `all`     | Everything above (except `dev`)                                              | One-shot full install |

```bash
# Core framework + LLM support only
uv pip install -e ".[llm]"

# Full development environment
uv pip install -e ".[all,dev]"
```

## Documentation

| Document | Description |
| --- | --- |
| [Architecture](docs/architecture.md) | Framework architecture and design decisions |
| [Core Modules](docs/core-modules.md) | Agent, Tool, Memory, Pipeline core concepts |
| [Memory System](docs/memory-system.md) | Multi-tier memory, Dream Engine, Governance |
| [Multi-Agent Guide](docs/multi-agent-guide.md) | Star topology, communication tools, subagent lifecycle |
| [Extension Guide](docs/extension-guide.md) | Hooks, interceptors, plugins, slash commands |
| [Bot Guide](docs/bot-guide.md) | bot_project example walkthrough |
| [Current Runtime](docs/current-runtime.md) | ReAct runtime design, control flow, approval flow |

## Development

```bash
pytest tests/unit/ -v
pytest tests/integration/ -v -m integration

ruff check framework tests
ruff format framework
mypy framework
```
