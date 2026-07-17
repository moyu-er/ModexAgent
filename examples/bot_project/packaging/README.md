# ModexBot Installer Packaging

Produces a **self-contained Windows installer** (`ModexBot-Setup-x.x.x.exe`)
that bundles a complete Python runtime, all dependencies, source code, a
pre-built React frontend, and an optional Electron desktop shell — all
pre-packaged at build time. **No network, no Python, no Node.js required on
the user's machine.**

## How It Works

```
build.bat (7 steps)
  │
  ├─ Step 1/7: prepare_icon.py  → logo.ico                   (from webui/public/logo.jpg)
  ├─ Step 2/7: (skipped — uv.exe no longer bundled)
  ├─ Step 3/7: build_archive.py → staging/app/               (git archive + frontend dist + prune tests/docs/assets)
  ├─ Step 4/7: prepare_python.py → staging/python/           (python-build-standalone + site-packages, strip dev deps + __pycache__)
  ├─ Step 5/7: electron/pack.js → staging/electron/          (Electron shell + icon embedding)
  ├─ Step 6/7: ISCC modexbot.iss → ModexBot-Setup-x.x.x.exe  (lzma2/max, non-solid)
  └─ Step 7/7: Done
```

**On the user's machine** (during installation) — **NO NETWORK**:

```
Inno Setup extracts files
  └─ python.exe postinstall.py    ← .pth files + config init + import verify
```

**After install**: double-click the desktop "ModexBot" icon →
`ModexBot.exe` (Electron) → bot starts → WebUI loads in a desktop window.
Fallback: Start Menu → "ModexBot (Browser)" → `launcher.pyw` → system browser.

## What Gets Packaged (and what doesn't)

The `.gitignore` files are the **single source of truth** for what's included
via `git archive HEAD`.

| Included (git-tracked) | Excluded (gitignored) |
|---|---|
| All `.py` source (`src/modex_agent/`, `bot/`, `modexbot/`) | `.env` (secrets) |
| `config/mcp/registry.example.json` (no-auth template) | `config/model.yml`, `config/im.yml` (secrets) |
| `config/pools/default/`, `config/pools/coder/` | `config/pools/main/` (personal) |
| SQLite migration files (`persistence/migrations/*.sql`) | `logs/`, `data/`, `.modex/` |
| `bot/web/dist/` (pre-built, copied separately) | `node_modules/`, `webui/dist/` |
| `pyproject.toml`, `README.md`, etc. | `.venv/`, `__pycache__/` |

> Build-time pruning also strips `tests/`, `assets/`, `docs/`, `.github/`, `rules/`, `scripts/` from the staged source, and dev-only site-packages (`mypy`, `pytest`, `pip`, …) + all `__pycache__` from the bundled Python — none are needed at runtime.

## Icon

The installer icon, shortcut icons, Electron exe icon, and window title-bar
icon all derive from a **single source**: `examples/bot_project/webui/public/logo.jpg`.

`prepare_icon.py` converts it to a multi-size `logo.ico` (16/32/48/64/128/256)
via Pillow at build time. The `.ico` is a build artifact (gitignored) consumed
by:

| Consumer | How |
|----------|-----|
| Installer exe icon | `modexbot.iss` → `SetupIconFile=logo.ico` |
| Desktop / Start Menu shortcuts | `modexbot.iss` → `IconFilename: {app}\logo.ico` |
| Electron `ModexBot.exe` icon | `pack.js` → `rcedit --set-icon logo.ico` |
| Electron window title-bar | `main.js` → `BrowserWindow({ icon: logo.ico })` |

## Prerequisites (Build Machine)

| Tool | Purpose | Install |
|------|---------|---------|
| **Python 3.10+** | Run build scripts (needs Pillow) | [python.org](https://python.org) |
| **Node.js + npm** | Build WebUI frontend + Electron packaging | [nodejs.org](https://nodejs.org) |
| **Inno Setup 6/7** | Compile installer | `winget install JRSoftware.InnoSetup` |
| **Git** | `git archive` (source export) | [git-scm.com](https://git-scm.com) |
| **uv** | Python runtime for `prepare_python.py` | [astral.sh](https://docs.astral.sh/uv/) |
| **Existing `.venv`** at repo root | Source of site-packages (`uv pip install -e ".[all,dev]"`) | — |
| **Electron zip** (offline mode) | `electron-v33.4.11-win32-x64.zip` placed in `electron/` | [npmmirror](https://npmmirror.com/mirrors/electron/33.4.11/electron-v33.4.11-win32-x64.zip) |

## Build

```cmd
cd examples\bot_project\packaging

:: Full build (with Electron desktop shell)
build.bat

:: Skip Electron (browser-only installer, ~115 MB)
build.bat --skip-electron

:: Skip frontend rebuild (use existing dist/)
build.bat --skip-fe

:: Skip both
build.bat --skip-fe --skip-electron
```

Output: `ModexBot-Setup-<version>.exe` (version from `pyproject.toml`).

## Install Layout (User's Machine)

```
%LOCALAPPDATA%\Programs\ModexBot\
├── logo.ico                     ← installer/shortcut icon
├── launcher.pyw                 ← browser fallback launcher
├── postinstall.py               ← install-time script
├── electron\                    ← Electron desktop shell (if packaged)
│   ├── ModexBot.exe             ← desktop window (icon embedded via rcedit)
│   └── resources\app\
│       ├── main.js
│       ├── package.json
│       └── logo.ico             ← BrowserWindow icon
├── python\                      ← bundled CPython 3.12 + all third-party deps
│   ├── python.exe
│   ├── pythonw.exe
│   ├── Lib\site-packages\       ← third-party only (project code stripped)
│   │   └── zz_modex_agent_src.pth   ← → <InstallDir>\app\src
│   │   └── zz_modexbot_bot.pth      ← → <InstallDir>\app\examples\bot_project
│   └── Scripts\
│       ├── modexbot.bat
│       └── modexctl.bat
└── app\                         ← git archive source (tests/assets/docs pruned at build)
    ├── pyproject.toml
    ├── src\modex_agent\
    └── examples\bot_project\
        ├── modexbot\
        ├── bot\
        │   └── web\dist\        ← pre-built React frontend
        ├── config\              ← writable
        └── .env, logs\, data\, .modex\
```

## Shortcuts Created

| Shortcut | Command | Icon |
|----------|---------|------|
| Desktop → ModexBot | `ModexBot.exe` (Electron) or `launcher.pyw` (browser) | `logo.ico` |
| Start Menu → ModexBot | same as above | `logo.ico` |
| Start Menu → ModexBot (Browser) | `launcher.pyw` | `logo.ico` |
| Start Menu → ModexBot Stop | `python.exe -m modexbot stop` | — |
| Start Menu → ModexBot Logs | `python.exe -m modexbot logs -f` | — |
| Start Menu → ModexBot Config | `python.exe -m modexbot config` | — |
| Start Menu → ModexBot Config Folder | `explorer.exe config\` | — |
| Start Menu → ModexBot Logs Folder | `explorer.exe logs\` | — |

## Post-Install Modifications

Because `.pth` files make the on-disk source importable (editable experience,
no venv/pip needed):

| Want to... | How |
|---|---|
| Modify code | Edit `.py` files in `app\`, restart with `modexbot restart` |
| Add a dependency | Install Python 3.12 yourself, then `pip install <package>` into `app\` (the bundled runtime ships neither `pip` nor `uv` — by design, to keep the install small) |
| Rebuild WebUI | Install Node.js → `modexbot install -f` |
| Change config | Edit `config\*.yml` or use `modexbot config` / WebUI Settings |

## Uninstall

Via Add/Remove Programs or `unins000.exe`. Cleans up:
- `electron\`, `logs\`, `data\`, `.modex\`, `__pycache__\`
- Removes `python\Scripts` from PATH

Config files with secrets (`.env`, `model.yml`, `im.yml`) are **preserved**.

## Files in This Directory

| File | Purpose |
|------|---------|
| `build.bat` | One-click build orchestrator (7 steps, `--skip-fe` / `--skip-electron`) |
| `prepare_icon.py` | Convert `logo.jpg` → `logo.ico` (Pillow, multi-size) |
| `fetch_runtime.py` | Download `uv.exe` into staging (unused — uv no longer bundled; kept as a standalone utility) |
| `build_archive.py` | `git archive HEAD` + frontend build + prune non-runtime dirs → `staging/app/` |
| `prepare_python.py` | Copy python-build-standalone + site-packages from `.venv`; strip project code, dev deps, `__pycache__` |
| `postinstall.py` | Install-time: `.pth` files, CLI shims, config init, import verification |
| `launcher.pyw` | Browser fallback: starts bot + opens system browser |
| `modexbot.iss` | Inno Setup script (`#ifexist` conditional for Electron/browser modes) |
| `electron/main.js` | Electron main process: bot lifecycle + BrowserWindow management |
| `electron/pack.js` | Electron packaging: local zip (offline) or `@electron/packager` (online) + icon embedding via rcedit |
| `electron/package.json` | Electron 33.4.11 + `@electron/packager` dev dependency |
