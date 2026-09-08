[English](getting-started.md) | [简体中文](getting-started.zh-CN.md) | [README](../../README.md)

# Getting Started with ModexBot

ModexBot is the ready-to-run WebUI application built on ModexAgent. This guide takes you from installation to your first local conversation, then introduces the commands you will use day to day.

> [!NOTE]
> Windows release assets use the name `ModexBot-Setup-*.exe`. **v1.0.0-dev** is a published development snapshot, not a claim that every feature is stable. Check [Releases](https://github.com/moyu-er/ModexAgent/releases/latest) for the newest available asset; the current source tree may contain changes made after any release.

## Choose an installation path

| Your system or goal | Recommended path |
| --- | --- |
| Windows, quickest setup | Install the published `.exe`; it bundles Python and the WebUI |
| macOS or Linux | Clone the repository and run `bash install.sh` |
| Windows development | Clone the repository and run `.\install.bat` |
| You want the newest source behavior | Use a source installation on any supported platform |

## Windows release installer

The installer bundles a Python runtime, Python dependencies, and a pre-built frontend, so you generally do not need to install Python or Node.js separately. Its Tauri desktop shell requires the Microsoft Edge WebView2 Runtime, which is normally already present on Windows 10 version 1803 and later. If it is missing, install the [WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/#download-section) or use the browser shortcut.

1. Open the [latest Releases page](https://github.com/moyu-er/ModexAgent/releases/latest).
2. Download the Windows asset named `ModexBot-Setup-*.exe`.
3. Double-click the file and follow the installer. It installs per user, so administrator rights are not normally required.
4. Launch **ModexBot** from the desktop or Start menu.
5. Open **Settings → Models** and add your model provider, model name, API URL, and API key.

When WebView2 is available, the desktop launcher opens the WebUI for you. The Start menu also provides browser, configuration, log, and stop shortcuts.

Because the release is a packaged snapshot, screens and configuration fields may differ from the current repository documentation. Use the source installation below when you specifically need current source behavior.

## Install from source

### Prerequisites

| Requirement | What it is used for |
| --- | --- |
| [Git](https://git-scm.com/) | Cloning the repository |
| [uv](https://docs.astral.sh/uv/) | Installing and managing Python and project packages |
| Python 3.12 | The required runtime; the setup scripts ask `uv` to download and manage it |
| [Node.js](https://nodejs.org/) with npm | Building the browser WebUI |

A separate system Python is not required when `uv` can install Python 3.12. Node.js is optional only for backend-only use; you need it for the WebUI described in this guide.

Clone the repository first:

```bash
git clone https://github.com/moyu-er/ModexAgent.git
cd ModexAgent
```

### macOS or Linux

```bash
cd examples/bot_project
bash install.sh
```

The script can offer to install missing `uv` and Node.js, creates the repository-root `.venv` with Python 3.12, installs the framework and bot, builds the WebUI, and offers to add the CLI to `PATH`.

### Windows from source

Run these commands in PowerShell from the cloned repository:

```powershell
cd examples\bot_project
.\install.bat
```

The batch script performs the same setup with Windows paths. It can use WinGet for missing tools and offers to add `.venv\Scripts` to your user `PATH`.

Both scripts are safe to rerun. If `modexbot` is not found immediately after setup, open a new terminal so the updated `PATH` is loaded.

## Configure a model

Run either command; both currently open the same interactive multi-provider model wizard:

```bash
modexbot config
# Equivalent model wizard:
modexbot model
```

You can also start without a model and configure one later under **Settings → Models** in the WebUI. After changing the active provider or model from the CLI, restart the bot so runtime routing uses the new configuration.

The wizard writes `examples/bot_project/config/model.yml` in a source checkout. This file contains API keys as literal secret values.

> [!IMPORTANT]
> Treat `model.yml` as a secret. Do not commit it, paste it into issues, attach it to support requests, or include it in screenshots. Use restricted API keys and rotate a key if it is exposed.

## Keep the WebUI local

The current source configuration listens on `0.0.0.0` by default. That accepts connections through every network interface, and the WebUI currently has no authentication layer. Do not expose port `21800` to the public internet.

For normal local use, edit `examples/bot_project/config/bot_config.yml` before starting and bind the server to loopback:

```yaml
webui:
  port: 21800
  host: "127.0.0.1"
```

Apply the same setting through the installed configuration folder when using a release that exposes this field. Firewall rules remain a useful second layer. The CLI `--port` option changes the port, not the configured host.

## Start and open the WebUI

```bash
modexbot start
modexbot status
```

`start` launches a detached background process. Open:

<http://localhost:21800/webui/>

Send a message in the chat. If a model is not configured yet, open **Settings → Models**, save a provider and model, and restart when prompted.

## Everyday CLI commands

| Command | Purpose |
| --- | --- |
| `modexbot config` | Open the interactive global model configuration wizard |
| `modexbot model` | Open the same multi-provider model wizard |
| `modexbot install` | Build the WebUI; skips when the built files are current |
| `modexbot install -f` | Force a fresh WebUI build |
| `modexbot start` | Start the bot in the background |
| `modexbot status` | Show process status |
| `modexbot logs` | Show the latest 50 log lines |
| `modexbot logs -f` | Follow new log output; press Ctrl+C to stop following |
| `modexbot restart` | Stop the existing bot and start a fresh process |
| `modexbot stop` | Stop the running bot |

Use `modexbot <command> --help` for command-specific options. `start` and `restart` do not rebuild the WebUI; run `modexbot install` after frontend source changes.

## Optional MCP servers

MCP is not required for your first conversation. `modexbot install` does not activate `config/mcp/registry.example.json`, and the source bootstrap scripts leave this optional registry setup to you. If you want MCP tools, follow [Configuration → MCP Servers](configuration.md#mcp-servers) to create and review `registry.json`; do not blindly enable servers you do not trust.

## Recover from a Node.js or WebUI build failure

If setup continued without Node.js, or the frontend build failed:

1. Install a current Node.js LTS release.
2. Open a new terminal so `node` and `npm` are on `PATH`.
3. Confirm `node --version` and `npm --version` work.
4. From `examples/bot_project`, run `modexbot install -f`.
5. If the bot is already running, run `modexbot restart`.

On macOS or Linux, an npm `EACCES` or `EPERM` error usually means the npm prefix is not writable. Use a user-owned npm prefix as suggested by the setup script; do not solve it by repeatedly running the build with `sudo`.

For other startup problems, run:

```bash
modexbot status
modexbot logs -f
```

If port `21800` is already occupied, stop the old ModexBot process or choose another port consistently with `--port`.

<a id="manual-source-installation"></a>

<details>
<summary>Manual source installation</summary>

Use this route when you do not want the bootstrap script. Run the following from the repository root:

```bash
uv python install 3.12
uv venv --python 3.12
```

Activate the environment on macOS or Linux:

```bash
source .venv/bin/activate
```

Or activate it in Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the local graph engine and root framework together, then install the bot:

```bash
uv pip install -e src/modex_graph -e '.[all]'
uv pip install -e 'examples/bot_project[webui]'
cd examples/bot_project
modexbot install
modexbot config
```

Keep `-e src/modex_graph` explicit. `modex-graph` is a local sibling package, so installing only the root editable project with a pip-style resolver can incorrectly look for that dependency on PyPI.

Before running `modexbot start`, change `webui.host` to `127.0.0.1` as shown in [Keep the WebUI local](#keep-the-webui-local). Then start the bot normally.

</details>

Next: [Configuration](configuration.md) | [Workflows](workflows.md) | [Extensions](extensions.md)
