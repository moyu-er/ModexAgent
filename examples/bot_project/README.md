<p align="center"><img src="../../assets/logo-wordmark-dark.svg" alt="ModexAgent" width="300"></p>

<p align="center"><strong>ModexBot: the official customizable reference application for ModexAgent</strong></p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>

<p align="center"><img src="../../assets/webui-multiagent.png" alt="ModexBot WebUI showing multi-agent collaboration" width="900"></p>
<p align="center"><sub>Current source UI example; packaged release versions may differ.</sub></p>

ModexBot is not a fixed demo. It is the framework's official, runnable application for building your own multi-agent assistant: use the shipped WebUI and channels directly, or customize the prompts, pools, skills, tools, plugins, adapters, and graph workflows.

## What It Includes

- A React WebUI with streaming chat, workspaces, conversation trees, tool traces, configurable human approval for native agents, model selection, and graph execution.
- Persistent agent pools with root agents, subagents, cross-pool collaboration, memory, and reusable experience.
- QQ and Telegram channels on the same adapter boundaries; the WebUI needs no IM credentials.
- Built-in tools, MCP integration, project plugins, skills, and declarative graph workflows.

## Start

Choose one of the two supported installation sources; the canonical instructions and troubleshooting live in the user guide.

| Source | Best for | Instructions |
| --- | --- | --- |
| Windows release installer | Fastest packaged setup | [Windows release installer](../../docs/user-guide/getting-started.md#windows-release-installer) |
| Source checkout | Development and current source behavior | [Install from source](../../docs/user-guide/getting-started.md#install-from-source) |

### Source Quick Start

From this directory (`examples/bot_project`), run the platform bootstrap:

```bash
bash install.sh
```

```powershell
.\install.bat
```

Configure a model provider, model, endpoint, and API key:

```bash
modexbot config
```

After model configuration, `config/model.yml` stores the API key as a literal secret. Keep it local, never commit or share it, and rotate any exposed key.

> [!IMPORTANT]
> Before starting, bind `webui.host` in `config/bot_config.yml` to `127.0.0.1`. The current source default is `0.0.0.0`, and the WebUI has no authentication layer. Never expose port `21800` to the public internet.

```yaml
webui:
  port: 21800
  host: "127.0.0.1"
```

Then start the application and open <http://localhost:21800/webui/>:

```bash
modexbot start
modexbot status
```

## Customize the Application

Start with WebUI **Settings** for routine changes. Use the files below when developing or versioning an application variant.

| Path | Purpose |
| --- | --- |
| `plugins/` | Project Python plugins and component registrations |
| `config/` | Runtime, model, IM, and MCP configuration |
| `config/scopes/` | Workspace, pool, agent, tool, and capability declarations |
| `agents/` | Markdown system prompts |
| `skills/`, `local_skills/` | Per-agent assignments and the project skill library |
| `bot/` | Application services, pipelines, workspace wiring, and business logic |
| `bot/adapters/` | QQ, Telegram, WebSocket, and custom channel adapters |
| `webui/` | React, TypeScript, Vite, and Tailwind frontend |
| `config/graphs/` | Declarative graph workflow specifications |

## Develop the WebUI

Install the frontend dependencies from this directory:

```bash
cd webui
npm ci
```

Keep the bot backend running, then start the Vite development server:

```bash
npm run dev
```

In another terminal, or after stopping the development server, run the build and tests:

```bash
npm run build
npm test -- --run
```

`npm ci` installs the locked dependencies. The `dev`, `build`, and `test` scripts match `webui/package.json`. See [`webui/AGENTS.md`](webui/AGENTS.md) for the frontend map and [`AGENTS.md`](AGENTS.md) for the application architecture.

## Full Guides

The complete documentation is maintained once under `docs/user-guide/`; this README intentionally does not duplicate the large configuration reference.

[Getting started](../../docs/user-guide/getting-started.md) | [Configuration](../../docs/user-guide/configuration.md) | [Workflows](../../docs/user-guide/workflows.md) | [Extensions](../../docs/user-guide/extensions.md)
