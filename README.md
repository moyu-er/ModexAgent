<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-wordmark-dark.svg">
    <img src="assets/logo-wordmark.svg" alt="ModexAgent" width="380">
  </picture>
</p>

<p align="center">
  <strong>Your agents. Your workflow.</strong><br>
  A modular Python agent framework with a ready-to-run web workbench.
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB" alt="Python 3.12 or newer"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-2DD4A8" alt="MIT license"></a>
  <a href="https://github.com/moyu-er/ModexAgent/releases"><img src="https://img.shields.io/badge/Status-Active%20development-64748B" alt="Active development"></a>
</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="#make-it-yours">Customization</a> ·
  <a href="docs/user-guide/workflows.md">Workflows</a> ·
  <a href="#documentation">Documentation</a>
</p>

ModexAgent brings models, tools, memory, and multi-agent collaboration into a framework you can shape around your work. Build a coding assistant, assemble a research team, or connect agents into a review workflow without rebuilding the surrounding runtime.

Want to use it before extending it? **ModexBot**, included in this repository, puts those capabilities in a browser: conversations, workspaces, agent teams, tool traces, and configuration in one place. It is both a usable application and a starting point for your own.

<p align="center">
  <img src="assets/modexagent-intro.gif" alt="Animated introduction to ModexAgent and its capabilities" width="960">
  <br><sub>A quick tour of ModexAgent.</sub>
</p>

## From a Conversation to a Workflow

**Give agents the tools to do the work.** Read and edit files, search a project, run commands, and connect MCP servers. Choose models and toolsets for each role, then add domain knowledge through prompts and Skills.

**Let specialists work together.** Define a main agent and its collaborators, delegate focused tasks, and inspect child sessions as the work unfolds. Connect teams across pools, or use graph workflows for a more explicit sequence of coding, review, and revision.

**Keep context beyond one reply.** Persistent sessions support ongoing work. Optional archive and core memory preserve longer-term context; the Experience capability can turn reviewed interactions into reusable guidance.

**Stay close to the execution.** Follow streamed responses, inspect tool arguments and results, manage attachments, and pause an active turn. Configure human approval and sandbox policies for native agents when you need additional control.

**Work through the interface that fits.** Use the browser workbench, connect QQ or Telegram, or bring OpenCode into the same workspace and session experience. External agents retain their own model, context, tool, and permission behavior.

## Make It Yours

Customization is not an extra layer bolted onto ModexBot. The application itself uses plugins to supply behavior to the framework, and the same extension points are available to your project.

| Start with | Shape it with | Use it for |
| --- | --- | --- |
| **Configuration** | WebUI settings and YAML agent declarations | Models, roles, toolsets, collaborators, memory, and permissions |
| **Prompts and Skills** | Markdown instructions and `SKILL.md` packages | Domain expertise, reusable procedures, and task-specific guidance |
| **MCP** | Registered servers selected per native agent | Browser tools and existing integrations |
| **Python plugins** | Tools, hooks, providers, interceptors, and more | New behavior or replacements for bundled implementations |
| **Capabilities** | A package of tools, hooks, prompt sections, and shared resources | Features that can be enabled together per agent |

In ModexBot, project plugins live in `examples/bot_project/plugins/` and are discovered at startup. Register a tool or hook, select it in the agent declaration, and restart. Framework applications can configure their own plugin discovery, while application channels and graph nodes have dedicated extension interfaces.

**[Write your first plugin →](docs/user-guide/extensions.md)** · [Configure an agent](docs/user-guide/configuration.md)

## Get Started

### Run ModexBot

**Windows:** download a `ModexBot-Setup-*.exe` from [Releases](https://github.com/moyu-er/ModexAgent/releases/latest). The installer bundles Python and the WebUI. Published development builds are snapshots and may differ from the current source; see the [installation guide](docs/user-guide/getting-started.md) for prerequisites and first launch.

**From source:** clone the repository, then run the setup script for your platform. Source setup uses Python 3.12 through `uv`, plus Node.js/npm to build the WebUI; the scripts can help install missing tools.

```bash
git clone https://github.com/moyu-er/ModexAgent.git
cd ModexAgent/examples/bot_project
```

macOS / Linux:

```bash
bash install.sh
```

Windows PowerShell:

```powershell
.\install.bat
```

> [!IMPORTANT]
> Keep the workbench on a trusted machine and network. The current source defaults to `0.0.0.0` without WebUI authentication. Before starting, set `webui.host` to `127.0.0.1` in `config/bot_config.yml` for local use. See [local setup and security](docs/user-guide/getting-started.md#keep-the-webui-local).

Open a new terminal if setup changed your `PATH`, then configure a model and start:

```bash
modexbot config
modexbot start
```

Open **[localhost:21800/webui/](http://localhost:21800/webui/)**, choose a pool, and start a conversation. You can also configure models in **Settings → Models**. Model access requires your own provider configuration; optional MCP servers and OpenCode have their own prerequisites.

**[Full installation guide →](docs/user-guide/getting-started.md)** · [Try a workflow](docs/user-guide/workflows.md)

### Build on the Framework

Use ModexBot as a working integration example, not a required UI for your application. Reuse the Python runtime and extension interfaces, provide your own tools and adapters, and choose which capabilities to assemble.

The [extension guide](docs/user-guide/extensions.md) walks from configuration to a complete Python tool plugin. For an explicit local package installation without the bot, use the [manual setup](docs/user-guide/getting-started.md#manual-source-installation) and install the framework and local `modex_graph` package together; skip the bot installation steps.

## Documentation

The user guides follow the current source tree. English and Chinese editions cover the same topics.

| Guide | What you will find |
| --- | --- |
| [Getting started](docs/user-guide/getting-started.md) | Installation, model setup, CLI commands, local security, and troubleshooting |
| [Configuration](docs/user-guide/configuration.md) | Agent declarations, tools, memory, MCP, Skills, and permissions |
| [Workflows](docs/user-guide/workflows.md) | Workspaces, agent teams, graph runs, approvals, and OpenCode |
| [Extensions](docs/user-guide/extensions.md) | Your first plugin, component replacement, Capabilities, channels, and graph nodes |

## Under the Hood

| Part | Responsibility |
| --- | --- |
| [`modex_agent`](src/modex_agent/) | Agent runtime, session management, tools, memory, collaboration, and extension contracts |
| [`modex_graph`](src/modex_graph/) | Standalone graph execution engine, used by native ReAct and workflow orchestration |
| [`bot_project`](examples/bot_project/) | ModexBot application, WebUI, channel adapters, project plugins, and example configurations |

Declarations are validated and compiled before components are assembled. Native and external execution share application-facing routing and output interfaces, while keeping their execution responsibilities distinct.

For deeper engineering context, explore the [architecture decisions](docs/adr/), [domain glossary](CONTEXT.md), and [Capability author guide](docs/design/capability-bundles/AUTHOR-GUIDE.md).

> [!NOTE]
> ModexAgent is under active development. APIs and configuration can evolve; check the documentation for the version you are running.
