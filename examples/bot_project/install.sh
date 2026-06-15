#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ROOT_VENV="$SCRIPT_DIR/../../.venv"
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

# ==========================================================================
# 2. uv
# ==========================================================================
if command -v uv &>/dev/null; then
    :  # already installed
else
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
    if ! command -v uv &>/dev/null; then
        echo ""
        echo "  [ERROR] uv installed but not found on PATH after reload."
        echo "  Try restarting your terminal and re-running install.sh."
        exit 1
    fi
    echo "  uv $(uv --version) installed."
    echo ""
fi

# ==========================================================================
# 3. Virtual environment
# ==========================================================================
if [ -x "$VENV_PYTHON" ]; then
    echo "  Virtual environment already exists, skipping creation."
else
    echo "Creating virtual environment (Python 3.12)..."
    if ! uv venv --python 3.12 "$ROOT_VENV"; then
        echo ""
        echo "[ERROR] uv venv failed. uv will download Python 3.12 automatically."
        echo "  Check network connectivity and retry."
        exit 1
    fi
fi

# ==========================================================================
# 4. Python dependencies
# ==========================================================================
NEEDS_PIP=0
if [ ! -f "$VENV_MARKER" ]; then
    NEEDS_PIP=1
elif [ -f "pyproject.toml" ]; then
    CUR_HASH=$(file_hash "pyproject.toml")
    STORED_HASH=$(cat "$VENV_MARKER" 2>/dev/null || echo "")
    [ "$CUR_HASH" != "$STORED_HASH" ] && NEEDS_PIP=1
fi

if [ "$NEEDS_PIP" -eq 1 ]; then
    echo "Installing Python dependencies..."
    uv pip install --python "$VENV_PYTHON" -e "../../.[all,dev]"
    uv pip install --python "$VENV_PYTHON" -e ".[webui,dev]"
    if [ -f "pyproject.toml" ]; then
        file_hash "pyproject.toml" > "$VENV_MARKER"
    fi
fi

# ==========================================================================
# 5. Environment file
# ==========================================================================
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo ""
    echo "[INFO] Creating .env from .env.example..."
    cp .env.example .env
    echo ""
    echo "  >>> ACTION REQUIRED: Edit .env with your credentials <<<"
    echo "  File: $SCRIPT_DIR/.env"
    echo "  Minimum required: LLM_MODEL, LLM_API_KEY, LLM_BASE_URL"
    echo ""
fi

# ==========================================================================
# 6. modexbot install (config wizard + frontend build)
# ==========================================================================
if [ "$HAS_NODE" -eq 1 ]; then
    echo ""
    echo "Running modexbot install (config check + frontend build)..."
    "$VENV_PYTHON" -m modexbot install || {
        echo ""
        echo "[WARNING] modexbot install encountered errors."
        echo "  You can retry after fixing the issues above:"
        echo "    $VENV_PYTHON -m modexbot install"
    }
else
    echo ""
    echo "[INFO] Node.js not available - running config wizard only."
    echo "  Frontend build will be skipped (WebUI will NOT be available)."
    echo ""
    "$VENV_PYTHON" -m modexbot config
    echo ""
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
    echo "  PATH updated for this session — 'modexbot' is ready to use."
    echo "  (New terminals will pick up the change from $SHELL_PROFILE)"
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
echo " Next step:"
echo ""
echo "       modexbot start"
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
