<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# modexbot

CLI entry point for the ModexAgent bot. Provides start/stop/restart/install commands with 3-layer process discovery.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker |
| `__main__.py` | `python -m modexbot` entry point |
| `cli.py` | CLI implementation — start, stop, restart, install, logs, status commands with process discovery |
| `main.py` | CLI → service bootstrap, creates and runs `BotService` |

## Commands

| Command | Purpose |
|---------|---------|
| `modexbot start` | Start the bot in a detached subprocess |
| `modexbot stop` | Stop the running bot process |
| `modexbot restart` | Stop then start the bot |
| `modexbot install` | Rebuild the WebUI frontend (`npm run build` in `webui/`). Skips if `dist/` is up-to-date; use `--force` to override |
| `modexbot logs` | Show recent bot log output |

## For AI Agents

### Working In This Directory
- `cli.py` uses 3-layer process discovery: (1) PID file check, (2) `psutil` process scan, (3) port probe.
- `main.py` is the actual runtime entry — it constructs `BotService`, calls `initialize()`, and starts the QQ/WebUI services.
- `_build_webui()` in `cli.py` compares `bot/web/dist/index.html` mtime against `webui/src/` and config files to decide whether to rebuild.
- The CLI recently had changes (check git status for current modifications).

### Common Patterns
- Process lifecycle: PID file at `.modex/bot.pid`, logs at `logs/`.
- Start creates a detached subprocess; stop sends SIGTERM then waits for graceful shutdown.
- `install` does NOT reinstall npm packages unless `node_modules/` is missing; it only rebuilds the production bundle.

## Dependencies

### Internal
- `bot/service/core.py` — `BotService` is the main runtime
- `bot/service/qq_service.py` — QQ platform service
- `bot/service/web_ui_service.py` — WebUI service

<!-- MANUAL -->
