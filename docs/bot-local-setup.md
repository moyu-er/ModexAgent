# Bot Local Setup (From Source)

Step-by-step guide for running the ModexAgent bot (`examples/bot_project/`) from source. This is the path for macOS/Linux users (no installer yet) and developers who want to hack on the code. Windows users who just want to run the bot should use the [installer](https://github.com/moyu-er/ModexAgent/releases) instead — see the [root README](../README.md) Quick Start.

## Prerequisites

Only **two** runtimes are needed — everything else (including Python 3.12) is managed automatically:

| Runtime | Purpose | Install |
|---------|---------|---------|
| [**uv**](https://docs.astral.sh/uv/) | Python package & version manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (POSIX) or `winget install astral-sh.uv` (Windows) |
| [**Node.js**](https://nodejs.org/) | WebUI frontend build (optional for backend-only) | [nodejs.org](https://nodejs.org/) or auto-installed by the bootstrap script |

> No system Python, pip, or npm required. `uv` downloads and manages its own Python 3.12. `node` includes `npm`.

---

## Option A: Bootstrap Script (Recommended)

Run the platform-specific bootstrap script from the `examples/bot_project/` directory:

| Platform | Script | How to run |
|----------|--------|------------|
| **Windows** | `install.bat` | Double-click, or run in **any terminal** (cmd, PowerShell, Windows Terminal) |
| **Linux / macOS** | `install.sh` | `chmod +x install.sh && ./install.sh` (works in **any shell**: bash, zsh, fish, etc.) |

Both scripts perform the same automated steps:

| Step | What it does |
|------|-------------|
| Prerequisite checks | Detects `uv` and `Node.js` — offers to install missing ones with y/n prompts (winget on Windows, brew/nvm on macOS/Linux) |
| Virtual environment | Creates virtual environment at repo root (`../../.venv`) with `uv venv --python 3.12` (Python downloaded automatically by uv) |
| Python dependencies | Installs the full framework (`..\..\.[all,dev]`) and bot CLI (`.[webui,dev]`) |
| Environment file | Copies `.env.example` → `.env` if `.env` doesn't exist |
| `modexbot install` | Runs config wizard (checks `config/model.yml`) + builds WebUI frontend via `npm run build` |
| **PATH registration** | Prompts to add the venv `Scripts`/`bin` directory to your **system-wide PATH**, so `modexbot` works from any terminal — no activation needed |

> [!NOTE]
> Both scripts are **idempotent** — re-running skips already-complete steps. They cache the `pyproject.toml` hash so Python dependencies are only reinstalled when project requirements change. Missing prerequisites trigger interactive y/n prompts. **You can run the scripts from any directory** — they locate the project via their own file path.

After the script completes:

```bash
modexbot start
```

Then open `http://localhost:21800/webui/` in your browser.

Common commands: `modexbot stop` | `modexbot restart` | `modexbot logs -f` | `modexbot install -f` | `modexbot config` | `modexbot model`

> [!TIP]
> If you skipped the PATH step, you can still run commands via the venv Python directly:
> - Windows: `..\..\.venv\Scripts\python.exe -m modexbot start`
> - Linux/macOS: `../../.venv/bin/python -m modexbot start`

---

## Option B: Manual Step-by-Step

### 1. Install Dependencies

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
cd examples/bot_project
uv pip install -e ".[webui,dev]"
```

> [!IMPORTANT]
> The `terminal` extra is required for the interactive shell tool. On Windows it installs `pywinpty`; on Linux/macOS it installs `pexpect` and `libtmux`. Both are included in the `[all]` extra.

### 2. Configure Environment Variables

```bash
cd examples/bot_project
cp .env.example .env
# Edit .env with real values
```

Key fields in `.env`:

```env
# Timezone for timestamps
TIMEZONE=Asia/Shanghai
```

> [!NOTE]
> **IM credentials do not live in `.env`.** QQ and Telegram credentials live in `config/im.yml` (one section per platform). Model settings (model / api_key / base URL / capabilities) also do **not** live in `.env`; they live in `config/model.yml` — see the next step.

### 3. Configure the Model

The model is configured in `config/model.yml` (the single source of truth). Run the interactive wizard with `modexbot model` (or `modexbot config`), which creates `config/model.yml` from scratch with the provider/key you enter — no template file is shipped. It holds multiple providers, each with their own models; `default_provider` + `default_model` is what a pool uses unless you switch per turn in the WebUI:

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

All pools share this single model config. `config/bot_config.yml` and `config/pools/*.yml` do **not** carry an `llm:` block.

### 4. Build and Run

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

For debugging, you can run the service in the foreground:

```bash
python -m modexbot _run    # in-process (same as 'modexbot start' but foreground)
# or
python debug_main.py        # debug entry, writes PID so 'modexbot stop' still works
```

---

## Troubleshooting

### `No module named 'aiohttp._cookie_helpers'` on startup (Windows)

This happens when dependencies are reinstalled while the bot is still running. The install scripts now **stop the running bot before reinstalling** and run a post-install integrity check (`import aiohttp._cookie_helpers`). Manual recovery: stop the bot (`modexbot stop`), delete the root `.venv`, and re-run `install.bat` / `install.sh`.

### Cross-filesystem hardlink corruption

On setups where the uv cache and venv are on different drives (e.g. cache on C:, venv on D:), the root `pyproject.toml` sets `[tool.uv] link-mode = "copy"`, forcing copy over hardlink so extraction can't be left half-done.

### `modexbot` command not found

The bootstrap script registers the venv `Scripts`/`bin` directory on your PATH. If you skipped that step or it failed, either:
- Re-run `install.bat` / `install.sh` (it will detect the missing PATH entry and offer to add it)
- Or invoke via the venv Python directly: `.venv\Scripts\python.exe -m modexbot start` (Windows) / `.venv/bin/python -m modexbot start` (POSIX)

### WebUI frontend not built

Run `modexbot install -f` to force-rebuild the frontend. This requires Node.js to be installed.

---

## Building a Windows Installer

If you want to produce a self-contained Windows installer (the download path described in the root README), see [`examples/bot_project/packaging/README.md`](../examples/bot_project/packaging/README.md). The build requires Inno Setup 6/7, Python 3.10+, Node.js, git, uv, and an existing `.venv` at the repo root. Output: `ModexBot-Setup-<version>.exe`.

---

## Related Documentation

- [Root README](../README.md) — framework overview + installer download
- [Bot example README](../examples/bot_project/README.md) — capabilities, architecture, configuration reference
- [Packaging README](../examples/bot_project/packaging/README.md) — building the Windows installer
