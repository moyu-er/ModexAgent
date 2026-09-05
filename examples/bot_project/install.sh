#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ROOT_VENV="$(cd "$SCRIPT_DIR/../.." && pwd)/.venv"
VENV_PYTHON="$ROOT_VENV/bin/python"
VENV_MARKER="$ROOT_VENV/.modexbot-pyproject-mtime"

echo ""
echo " ============================================="
echo "  ModexBot - Environment Setup"
echo " ============================================="
echo ""

# ── Detect OS ───────────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "macos" ;;
        *)       echo "unknown" ;;
    esac
}
OS=$(detect_os)

# ── Helper: prompt y/n ──────────────────────────────────────────────────
prompt_yn() {
    local prompt="$1"
    local default="${2:-Y}"
    local choice=""
    read -r -p "  $prompt " choice || true
    choice="${choice:-$default}"
    case "$choice" in
        [Yy]*) return 0 ;;
        *)     return 1 ;;
    esac
}

# ── Helper: file hash (cross-platform, for cache invalidation) ──────────
file_hash() {
    cksum "$1" 2>/dev/null | cut -d' ' -f1
}

# ── Helper: reload PATH for the current process after installers have written
# to shell profiles or known directories. Only mutates PATH; never touches other
# environment variables. Always returns 0.
reload_env() {
    if [ -f "$HOME/.local/bin/env" ]; then
        . "$HOME/.local/bin/env" 2>/dev/null || true
    fi

    if [ -z "${NVM_DIR:-}" ]; then
        export NVM_DIR="$HOME/.nvm"
    fi
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        . "$NVM_DIR/nvm.sh" 2>/dev/null || true
    fi

    local _bin
    for _bin in "/opt/homebrew/bin" "/usr/local/bin" "/home/linuxbrew/.linuxbrew/bin"; do
        [ -d "$_bin" ] || continue
        case ":$PATH:" in
            *":$_bin:") ;;
            *) export PATH="$_bin:$PATH" ;;
        esac
    done

    if [ -s "$NVM_DIR/nvm.sh" ]; then
        local _node_bin
        _node_bin=$(
            . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
            nvm which current 2>/dev/null | xargs dirname 2>/dev/null
        ) || true
        if [ -z "$_node_bin" ] || [ ! -d "$_node_bin" ]; then
            _node_bin=$(
                . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
                nvm which lts/* 2>/dev/null | xargs dirname 2>/dev/null
            ) || true
        fi
        if [ -n "$_node_bin" ] && [ -d "$_node_bin" ]; then
            case ":$PATH:" in
                *":$_node_bin:") ;;
                *) export PATH="$_node_bin:$PATH" ;;
            esac
        fi
    fi

    for _bin in "/usr/local/bin/node" "/usr/bin/node"; do
        [ -x "$_bin" ] || continue
        _bin=$(dirname "$_bin")
        case ":$PATH:" in
            *":$_bin:") ;;
            *) export PATH="$_bin:$PATH" ;;
        esac
    done

    return 0
}

path_contains() {
    local entry="$1"
    [[ ":${PATH}:" == *":${entry}:"* ]]
}

# ── Helper: discover the uv executable. uv's install location varies
# (standalone installer -> ~/.local/bin, Homebrew -> its bin), so never assume a
# fixed path. Probe PATH first, then known dirs, and pin the absolute path into
# UV_EXE. All later uv calls use "$UV_EXE" instead of bare uv. Returns 0 if found.
discover_uv() {
    UV_EXE=""
    if command -v uv &>/dev/null; then
        UV_EXE="$(command -v uv)"
        return 0
    fi
    local cand
    for cand in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
        if [ -x "$cand" ]; then
            UV_EXE="$cand"
            return 0
        fi
    done
    return 1
}

# ── Helper: ensure npm prefix is user-writable ───────────────────────────
# Node's official macOS installer sets prefix to /usr/local, which is owned
# by root. npm installs then fail with EACCES/EPERM. Fix by moving prefix
# into the user's home directory.
ensure_npm_prefix_usable() {
    if ! command -v npm &>/dev/null; then
        return 0
    fi

    local npm_prefix
    npm_prefix=$(npm config get prefix 2>/dev/null) || true
    if [ -z "$npm_prefix" ]; then
        return 0
    fi

    if [ -d "$npm_prefix" ] && [ -w "$npm_prefix" ]; then
        return 0
    fi

    echo ""
    echo "  [WARNING] npm prefix '$npm_prefix' is not writable by your user."
    echo "  This usually happens when Node.js is installed with the official"
    echo "  installer or with sudo. Local 'npm install' will fail with permission"
    echo "  errors until this is fixed."
    echo ""

    if ! prompt_yn "Move npm prefix to ~/.npm-global (recommended, no sudo)? [Y/n]:"; then
        echo ""
        echo "  [WARNING] Continuing with a root-owned npm prefix may cause"
        echo "  'EACCES: permission denied' errors during frontend builds."
        echo ""
        return 0
    fi

    local user_prefix="$HOME/.npm-global"
    mkdir -p "$user_prefix/bin"
    npm config set prefix "$user_prefix"

    # Make the new global bin directory available now and in future shells.
    case ":$PATH:" in
        *":$user_prefix/bin:") ;;
        *) export PATH="$user_prefix/bin:$PATH" ;;
    esac

    local profile
    profile=$(detect_shell_profile)
    append_path_once "$profile" "$user_prefix/bin" || true

    echo ""
    echo "  npm prefix moved to $user_prefix"
    echo "  Global packages will install there instead of $npm_prefix"
}

# ==========================================================================
# 1. Node.js
# ==========================================================================
HAS_NODE=0

if command -v node &>/dev/null; then
    HAS_NODE=1
    echo "  Node.js: $(node --version)"
    echo ""
fi

if [ "$HAS_NODE" -eq 1 ]; then
    :  # skip to uv
else
    echo "  [WARNING] Node.js not found."
    echo "  Node.js is required to build the WebUI frontend."
    echo ""

    installed=0
    case "$OS" in
        macos)
            if command -v brew &>/dev/null; then
                if prompt_yn "Install Node.js via Homebrew? [Y/n]:"; then
                    echo "  Installing Node.js (Homebrew)..."
                    brew install node && reload_env
                    if command -v node &>/dev/null; then
                        HAS_NODE=1
                        installed=1
                        echo "  Node.js $(node --version) installed."
                    fi
                fi
            fi
            ;;
        linux)
            if prompt_yn "Install Node.js via nvm (recommended, no sudo)? [Y/n]:"; then
                echo "  Installing nvm and Node.js LTS..."
                curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
                export NVM_DIR="$HOME/.nvm"
                reload_env
                if command -v nvm &>/dev/null; then
                    nvm install --lts && HAS_NODE=1 && installed=1
                    echo "  Node.js $(node --version) installed."
                else
                    echo "  [WARNING] nvm installed but not available in this shell."
                    echo "  Restart your terminal and re-run install.sh."
                fi
            fi
            ;;
    esac

    if [ "$installed" -eq 0 ]; then
        echo "  Install manually from: https://nodejs.org"
        echo "  (or: brew install node  |  apt install nodejs  |  nvm install --lts)"
        if ! prompt_yn "Continue without frontend build? [y/N]:" "N"; then
            echo "  Setup aborted. Install Node.js and re-run install.sh."
            exit 1
        fi
        echo "  OK -- will skip frontend build. WebUI will NOT be available."
        echo "  After installing Node.js, re-run install.sh or use: modexbot install"
    fi
    echo ""
fi

# Fix common npm permission issues before any npm command runs.
ensure_npm_prefix_usable

# ==========================================================================
# 2. uv
# ==========================================================================
# Only install uv when none is available; reuse an existing one. After any
# install we re-discover the real path because the location is not guaranteed.
discover_uv || true
if [ -z "${UV_EXE:-}" ]; then
    echo "  [INFO] uv package manager not found (required for Python dependency management)."
    echo ""
    if ! prompt_yn "Install uv automatically (official standalone installer)? [Y/n]:"; then
        echo ""
        echo "  Cannot proceed without uv."
        echo "  Install manually: https://docs.astral.sh/uv/"
        echo "  Then re-run install.sh."
        exit 1
    fi
    echo ""
    echo "  Installing uv..."
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        echo ""
        echo "  [ERROR] uv installer failed."
        echo "  Install manually: https://docs.astral.sh/uv/"
        echo "  Then re-run install.sh."
        exit 1
    fi
    reload_env
    if ! discover_uv; then
        echo ""
        echo "  [ERROR] uv installed but could not be located."
        echo "  Try restarting your terminal and re-running install.sh."
        exit 1
    fi
    echo "  uv installed."
    echo ""
fi
echo "  Using uv: $UV_EXE ($("$UV_EXE" --version 2>/dev/null))"

# ==========================================================================
# 3. Virtual environment
# ==========================================================================
if [ -x "$VENV_PYTHON" ]; then
    echo "  Virtual environment found, checking health..."
    # Verify the venv is actually isolated: sys.prefix must point inside it.
    # Do NOT import third-party packages here; only validate Python itself.
    if "$VENV_PYTHON" -c "import sys; sys.exit(0 if sys.prefix == '$ROOT_VENV' else 1)" >/dev/null 2>&1; then
        echo "  Virtual environment is healthy."
    else
        echo "  Existing venv is unhealthy (not isolated from system Python), recreating..."
        rm -rf "$ROOT_VENV"
    fi
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Creating virtual environment (uv-managed Python 3.12)..."
    # Force uv to use ONLY its own managed Python — never the system interpreter —
    # so the environment is reproducible and independent of any system Python.
    export UV_PYTHON_PREFERENCE=only-managed

    # Name the failing step so the user knows what broke. Both failure modes
    # share the same remedy (the GitHub interpreter download may need a mirror).
    venv_create_fail() {
        echo ""
        echo "[ERROR] Failed while $1."
        echo "  The uv-managed interpreter is downloaded from GitHub"
        echo "  (python-build-standalone) and may be blocked or time out on some"
        echo "  networks. Set a mirror and retry:"
        echo "    export UV_PYTHON_INSTALL_MIRROR=<mirror-base-url>"
        echo "    ./install.sh"
        echo "  See https://docs.astral.sh/uv/ for the mirror URL format."
        exit 1
    }

    "$UV_EXE" python install 3.12 || venv_create_fail "downloading uv-managed Python 3.12"
    "$UV_EXE" venv --python 3.12 "$ROOT_VENV" || venv_create_fail "creating the virtual environment"
fi

# ==========================================================================
# 4. Python dependencies
# ==========================================================================
# Copy mode avoids cross-filesystem hardlink failures (uv cache vs venv on
# different mounts) that can leave packages half-extracted. Belt-and-suspenders
# with [tool.uv] link-mode in the root pyproject.
export UV_LINK_MODE=copy

# uv pip install runs with cwd = bot_project, whose pyproject.toml has no
# [tool.uv] table, so the root project's mirror index would not be discovered.
# Set it explicitly so package downloads use the mirror deterministically.
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

# Stop any running bot first. A running process may hold its imported files open;
# reinstalling a package the bot imports while it runs can corrupt the install.
BOT_PID_FILE="$SCRIPT_DIR/.modex/bot.pid"
if [ -f "$BOT_PID_FILE" ]; then
    echo "Stopping running bot before dependency reinstall..."
    "$VENV_PYTHON" -m modexbot stop >/dev/null 2>&1 || true
fi

pip_install() {
    # $1 = extra uv flags (e.g. --reinstall) for the self-healing recovery path.
    local extra="${1:-}"
    # When --reinstall is requested, explicitly uninstall the editable packages
    # first. uv's --reinstall alone can leave stale physical copies in
    # site-packages when the previous install was interrupted or files were
    # locked — the stale bot/ directory then shadows the .pth source path and
    # the CLI imports outdated code (the modexctl ModuleNotFoundError bug).
    if [ "$extra" = "--reinstall" ]; then
        "$UV_EXE" pip uninstall --python "$VENV_PYTHON" modex-bot-project ModexAgent >/dev/null 2>&1 || true
    fi
    "$UV_EXE" pip install $extra --python "$VENV_PYTHON" -e "../../.[all,dev]"
    "$UV_EXE" pip install $extra --python "$VENV_PYTHON" -e ".[webui,dev]"
    # Editable install guard: a stale physical bot/ directory in any
    # site-packages entry shadows the .pth source path. getsitepackages() can
    # return multiple paths, so check each one.
    "$VENV_PYTHON" -c "
import site, os, shutil
for sp in site.getsitepackages():
    p = os.path.join(sp, 'bot')
    if os.path.isdir(p):
        print(f'  [INFO] Removing stale {p}/ (shadows editable source)...')
        shutil.rmtree(p, ignore_errors=True)
" 2>/dev/null || true
}

# Fingerprint BOTH pyproject files: most framework deps live in the root project
# (../../), so a change there must also trigger a reinstall — the bot pyproject
# alone would miss it.
CUR_HASH="$(file_hash pyproject.toml)#$(file_hash ../../pyproject.toml)"

NEEDS_PIP=0
# A framework-only sync can remove the bot package while the marker stays valid.
if [ ! -x "$ROOT_VENV/bin/modexbot" ] || [ ! -x "$ROOT_VENV/bin/modexctl" ]; then
    NEEDS_PIP=1
fi
if ! "$VENV_PYTHON" -c "import importlib.metadata as m; m.distribution('modex-bot-project')" >/dev/null 2>&1; then
    NEEDS_PIP=1
fi
if [ ! -f "$VENV_MARKER" ]; then
    NEEDS_PIP=1
else
    STORED_HASH=$(cat "$VENV_MARKER" 2>/dev/null || echo "")
    [ "$CUR_HASH" != "$STORED_HASH" ] && NEEDS_PIP=1
fi

if [ "$NEEDS_PIP" -eq 1 ]; then
    echo "Installing Python dependencies..."
    pip_install
    printf '%s' "$CUR_HASH" > "$VENV_MARKER"
fi

# Integrity smoke check — runs even when the marker says "already installed", so
# a previously-corrupted install (interrupted / files held open) is detected and
# self-heals instead of being silently skipped. aiohttp._cookie_helpers is a
# canary: its absence is exactly the production crash signature of a
# half-extracted aiohttp.
if ! "$VENV_PYTHON" -c "import aiohttp, aiohttp._cookie_helpers, aiohttp.web" >/dev/null 2>&1; then
    echo "Critical import check failed — environment is corrupted, forcing clean reinstall..."
    pip_install --reinstall
    "$VENV_PYTHON" -c "import aiohttp, aiohttp._cookie_helpers, aiohttp.web" >/dev/null 2>&1 || {
        echo "[ERROR] aiohttp still fails to import after reinstall."
        echo "  Ensure the bot is stopped, then delete $ROOT_VENV and re-run install.sh."
        exit 1
    }
    printf '%s' "$CUR_HASH" > "$VENV_MARKER"
fi

# ==========================================================================
# 5. Environment file
# ==========================================================================
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo ""
    echo "[INFO] Created .env from .env.example."
    cp .env.example .env
    echo "  File: $SCRIPT_DIR/.env"
    echo "  Edit it only if you use integrations that read it (see the file's comments)."
    echo ""
fi

# ==========================================================================
# 5b. Global model config
# ==========================================================================
# No example template is shipped — model.yml is bootstrapped entirely by the
# `modexbot config` wizard (run by `modexbot install` below). The wizard
# creates config/model.yml from scratch with the provider/key you enter.
if [ ! -f "config/model.yml" ]; then
    echo ""
    echo "  >>> ACTION REQUIRED: Set your model via 'modexbot config' <<<"
    echo "  The wizard creates config/model.yml from scratch."
    echo "  Minimum required: model, api_key, url"
    echo ""
fi

# ==========================================================================
# 6. modexbot install (frontend build only — no model gate)
# ==========================================================================
if [ "$HAS_NODE" -eq 1 ]; then
    echo ""
    echo "Running modexbot install (frontend build)..."
    "$VENV_PYTHON" -m modexbot install || {
        echo ""
        echo "[WARNING] modexbot install encountered errors."
        echo "  You can retry after fixing the issues above:"
        echo "    $VENV_PYTHON -m modexbot install"
    }
else
    echo ""
    echo "[INFO] Node.js not available — skipping frontend build (WebUI will NOT be available)."
    echo "  The bot still starts; configure a model via WebUI Settings or 'modexbot config'."
    echo "  After installing Node.js, rebuild the frontend with:"
    echo "    $VENV_PYTHON -m modexbot install -f"
fi

# ==========================================================================
# 7. Register modexbot CLI globally
# ==========================================================================
echo ""
echo "  [INFO] The 'modexbot' CLI is installed in $ROOT_VENV/bin/modexbot"
echo "  Adding this directory to your PATH lets you run 'modexbot'"
echo "  from any terminal without activating the venv."
echo ""
VENV_BIN="$ROOT_VENV/bin"

detect_shell_profile() {
    case "$(basename "${SHELL:-}")" in
        zsh)  [ -f "$HOME/.zshrc" ]  && echo "$HOME/.zshrc"  && return;;
        bash) [ -f "$HOME/.bashrc" ] && echo "$HOME/.bashrc" && return;;
        fish) echo "$HOME/.config/fish/config.fish" && return;;
    esac
    # fallback: pick first existing POSIX profile
    for f in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
        [ -f "$f" ] && echo "$f" && return
    done
    echo "$HOME/.profile"
}

# Append *bindir* to *profile* only if the exact entry is not already present.
# Returns 0 when added, 1 when already present. Never modifies or deletes
# existing lines.
append_path_once() {
    local profile="$1"
    local bindir="$2"
    local line

    mkdir -p "$(dirname "$profile")" 2>/dev/null || true

    case "$(basename "$profile")" in
        config.fish)
            line="set -gx PATH \"$bindir\" \$PATH"
            ;;
        *)
            line="export PATH=\"$bindir:\$PATH\""
            ;;
    esac

    if [ -f "$profile" ] && grep -qxF "$line" "$profile" 2>/dev/null; then
        return 1
    fi

    echo "$line" >> "$profile"
    return 0
}

SHELL_PROFILE=$(detect_shell_profile)

if prompt_yn "Add $VENV_BIN to your PATH ($SHELL_PROFILE)? [Y/n]:"; then
    if append_path_once "$SHELL_PROFILE" "$VENV_BIN"; then
        echo "  Added to $SHELL_PROFILE"
    else
        echo "  Already in $SHELL_PROFILE - skipping."
    fi
    # Make it available in the current session immediately, without duplicating.
    case ":$PATH:" in
        *":$VENV_BIN:"*) ;;
        *) export PATH="$VENV_BIN:$PATH" ;;
    esac
    echo ""
    echo "  Shell profile updated — new terminals will pick up the change automatically."
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    echo "  │ To use 'modexbot' in your CURRENT terminal RIGHT NOW:       │"
    echo "  │                                                             │"
    if [ "$(basename "${SHELL:-}")" = "fish" ]; then
        echo "  │   source $SHELL_PROFILE                                    │"
    else
        echo "  │   source $SHELL_PROFILE                                    │"
        echo "  │   (or: exec \$SHELL -l   to restart your shell)             │"
    fi
    echo "  └─────────────────────────────────────────────────────────────┘"
fi

# ==========================================================================
# Done
# ==========================================================================
echo ""
echo " ============================================="
echo "  Setup complete!"
echo " ============================================="
echo ""
echo " What's been set up:"
echo "   - uv package manager"
echo "   - Python virtual environment ($ROOT_VENV)"
echo "   - Framework + bot dependencies"
if [ "$HAS_NODE" -eq 1 ]; then
    echo "   - WebUI frontend (bot/web/dist)"
else
    echo "   - WebUI frontend: SKIPPED (Node.js not available)"
fi
echo ""
echo " Next steps:"
echo ""
echo "   1. Configure your model (sets model / api_key / url):"
echo ""
echo "         modexbot config"
echo ""
echo "   2. Start the bot:"
echo ""
echo "         modexbot start"
echo ""
echo " (start/restart will prompt for config if the model is unset.)"
echo ""
echo " (If 'modexbot' is not found, open a NEW terminal or run:"
echo "   source $SHELL_PROFILE)"
echo ""
echo " The bot will be available at: http://localhost:21800/webui/"
if [ "$HAS_NODE" -eq 0 ]; then
    echo ""
    echo " (WebUI will not work until Node.js is installed and frontend is built)"
    echo " After installing Node.js, run: modexbot install -f"
fi
echo ""
echo " Other commands:"
echo "   modexbot stop         - Stop the bot"
echo "   modexbot logs -f      - View live logs"
echo "   modexbot install -f   - Rebuild frontend"
echo "   modexbot config       - Interactive config wizard"
echo ""
