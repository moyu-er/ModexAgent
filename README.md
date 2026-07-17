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
  Graph-driven ReAct · Interruptible Approval · Cross-Platform Terminal · Multi-Agent (Per-Pool Star + Cross-Pool Peer) · WebUI
</p>

<p align="center">
  <img src="assets/modexagent-intro.gif" alt="ModexAgent Intro" width="720">
</p>

ModexAgent is a Python framework for building AI agent applications. It decomposes model inference, tool invocation, memory management, I/O adapters, and multi-agent collaboration into independently evolvable modules. Start with a minimal ReAct agent and gradually expand into a full application with long-term memory, multi-agent coordination, runtime governance, and a browser-based WebUI.

The framework core replaces traditional loops with a **graph-driven execution engine**, supporting mid-execution suspension, approval, and resumption. The runtime uses **Pool mode** — multi-agent persistent pools with `MessageBroker` + `AgentMessageBus` routing. I/O adapters are fully decoupled from agent logic. A **React + WebSocket WebUI** is included in `examples/bot_project/` for real-time multi-conversation chat. The plugin architecture draws inspiration from OpenClaw, with deep customizations for type safety, cross-platform terminal interaction, and inter-agent communication.

> [!NOTE]
> The project is under active development. Core interfaces are stabilizing, and `examples/bot_project/` provides a comprehensive example covering most framework capabilities, including the WebUI frontend.

## Highlights

| Browser WebUI | Interruptible Approval | Multi-Agent Collaboration |
|:---:|:---:|:---:|
| ![WebUI](assets/webui-settings-pools.png) | ![Approval](assets/webui-approval.png) | ![Multi-Agent](assets/webui-multiagent.png) |
| Real-time streaming chat with a built-in TodoPanel, per-turn model selector, in-browser config editor, attachments, and mermaid | Sensitive tool calls suspend for human approval with tiered cascade policies | Star-topology subagents via sync wake, async inbox, and isolated spawn; cross-pool peer messaging between main agents |

## Key Features

- **Graph-driven ReAct Engine** — Execution modeled as `Graph[R] + Node[R] + Edge`. Supports `GraphInterrupt` suspension and state-persistent resumption. Naturally suited for approval and breakpoint-resume scenarios. Loop detection exits a runaway ReAct loop as a controlled stop instead of burning tokens (ADR-0016).
- **Interruptible Approval** — The agent asks before making risky changes. When it tries to write or edit files outside your project folder, it pauses and asks for your go-ahead — approve with one click in the WebUI or reply `/approve` in chat, and it continues exactly where it stopped. Off by default; turn it on per agent.
- **Cross-platform Interactive Terminal** — Built-in terminal toolchain with unified interfaces for Windows (WinPTY/ConPTY), Linux, and macOS (pexpect/tmux); visible and headless PTY modes, covered by 248+ unit tests.
- **Multi-agent Collaboration** — Each pool is a strict star: a main agent as hub, subagents talk only to their parent via the single `send_to_agent` tool (the framework routes through the broker, the async inbox, or an isolated subagent session as needed). Across pools, main agents communicate as peers — a main agent can `send_to_agent` another pool's main agent, which receives on its own bus and replies in kind. Subagent↔subagent and subagent→non-parent sends are rejected by the topology gate.
- **Pool Runtime** — Multi-agent persistent pools with `MessageBroker` + `AgentMessageBus` routing. I/O adapters are fully decoupled from agent logic.
- **Multi-tier Memory + Self-Learning** — Session, Archive, Knowledge, UserRetentionBuffer, Pruned, and Experience layers with configurable scopes (SessionScope / UserScope / GlobalScope). Dream Engine consolidates archives into knowledge; ExperienceReviewAgent turns conversations into reusable EXPERIENCE.md reference knowledge.
- **Hook + Interceptor Extension System** — Lifecycle hooks (InboxFlush, SubagentAutoSend) and AOP interceptor chains (ControlDrain, ToolResultLimit) compose orthogonally without core intrusion.
- **Type Safety** — All interfaces use ABCs (zero Protocols), enums replacing raw strings, mypy strict-level checking.
- **Native MCP Integration** — Dynamically load MCP servers (SSE/stdio). `MCPToolAdapter` maps MCP capabilities to framework Tool objects.
- **Browser WebUI** — React + Vite frontend with real-time streaming, a built-in **TodoPanel** for task tracking, a **per-turn model selector** (multi-provider / multi-model switching), an **in-browser config editor** (pools, models, MCP servers, skills, system prompts — no YAML hand-editing), session tree, attachment upload/download, mermaid diagrams, and light/dark themes (see `examples/bot_project/`).

## Architecture Overview

The browser WebUI talks to the agent over **WebSocket** (streaming chat, live status) and **REST** (config editing, session/pool/workspace management); IM adapters plug in symmetrically through the same broker.

```
┌─────────────────────────────────────────────────────────────────────────┐
│        External Platforms (QQ / Telegram / CLI / HTTP / WebSocket → WebUI)         │
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

### Windows — Installer (Recommended)

Download the installer from [Releases](https://github.com/moyu-er/ModexAgent/releases/latest) and double-click to install. The installer bundles everything — Python runtime, all dependencies, the WebUI frontend — so **no prerequisites and no internet connection during installation**.

1. **Download** — Go to [Releases](https://github.com/moyu-er/ModexAgent/releases/latest), download `ModexBot-Setup-x.x.x.exe`
2. **Install** — Double-click the downloaded `.exe`, follow the wizard (no admin rights needed)
3. **Launch** — Double-click the "ModexBot" desktop shortcut, or find it in the Start Menu
4. **Configure** — On first launch, open **Settings** in the WebUI and enter your model API key (supports DeepSeek, OpenAI, and more)

The WebUI opens automatically. Start chatting!

<details>
<summary>Command-line usage</summary>

After installation, `modexbot` is available from any terminal:

| Command | Action |
|---------|--------|
| `modexbot start` | Start the bot |
| `modexbot stop` | Stop the bot |
| `modexbot restart` | Restart |
| `modexbot logs -f` | Follow live logs |
| `modexbot config` | Config wizard |
| `modexbot model` | Model settings |

Uninstall via **Add/Remove Programs** — your config files (API keys, etc.) are preserved.

</details>

### macOS / Linux / Developers — From Source

No installer for macOS/Linux yet. Run from source instead:

```bash
git clone https://github.com/moyu-er/ModexAgent.git
cd ModexAgent/examples/bot_project

# Windows
install.bat

# Linux / macOS
chmod +x install.sh && ./install.sh
```

The script auto-installs `uv` + Node.js if missing, sets up the Python environment, builds the WebUI, and registers `modexbot` on your PATH. Then:

```bash
modexbot start   # open http://localhost:21800/webui/
```

For detailed steps and troubleshooting, see **[docs/bot-local-setup.md](docs/bot-local-setup.md)**.

## Project Structure

```
ModexAgent/
├── src/modex_agent/   # The framework package — ABCs, runtime, memory, multi-agent, tools, WebUI wiring
├── examples/
│   └── bot_project/   # Reference application: WebUI + IM adapters (QQ, Telegram) — Pool mode
├── tests/             # Unit, architecture, conformance, and integration tests
└── docs/              # ADRs, design docs, agent docs
```

`src/modex_agent/` is the reusable framework; `examples/bot_project/` is the canonical end-to-end application built on top of it. Per-module `AGENTS.md` files document each package in detail.

## Documentation

| Document | Description |
| --- | --- |
| [ADR index](docs/adr/) | Architecture Decision Records (ADR-0001 ~ 0024) |
| [Docs overview](docs/AGENTS.md) | Index of `docs/` — ADRs, design docs, agent docs |
| [CONTEXT.md](CONTEXT.md) | Domain glossary — Pool, Workspace, ReAct Agent, Graph, GraphInterrupt, Assembly, etc. |
| [Bot local setup](docs/bot-local-setup.md) | Step-by-step bot setup from source (prerequisites, venv, config wizard, troubleshooting) |
| [Bot example](examples/bot_project/README.md) | bot_project walkthrough (multi-channel IM + WebUI, multi-agent setup, configuration) |
| [External coding agents](docs/design/external-coding-agent-integration/spec.md) | Integrate Pi / OpenCode / future coding agent CLIs as pool main agents (ADR-0022) |
| Per-module `AGENTS.md` | Every package under `src/modex_agent/` ships an `AGENTS.md` describing its responsibility and key files |

## Development

```bash
pytest tests/unit/ -v
pytest tests/integration/ -v -m integration

ruff check src/modex_agent tests
ruff format src/modex_agent
mypy src/modex_agent
```
