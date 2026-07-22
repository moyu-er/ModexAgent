"""Post-installation script — runs inside the installer on the user's machine.

NO NETWORK. NO DOWNLOAD. Just file operations:
  1. Create .pth files so the bundled Python finds project source on disk
  2. Copy .example config templates -> real config files (if missing)
  3. Create writable runtime directories (logs, data, .modex)
  4. Verify imports work

The bundled Python (python-build-standalone + all third-party deps) was
prepared at build time.  This script just wires it to the source layout
on the user's machine.

Usage (called by installer)::

    python.exe postinstall.py --app-dir "<InstallDir>"
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def _copy_if_missing(src: Path, dst: Path) -> bool:
    if dst.exists():
        return False
    shutil.copy2(src, dst)
    return True


# ── Step 1: .pth files ──────────────────────────────────────────────────────


def create_pth_files(app_dir: Path) -> None:
    """Create .pth files in the bundled Python's site-packages.

    These make ``import modex_agent``, ``import modexctl``, ``import bot``,
    and ``import modexbot`` resolve to the source directories on disk —
    giving users an editable-code experience without a venv or pip install.
    """
    python_dir = app_dir / "python"
    site_packages = python_dir / "Lib" / "site-packages"
    repo_root = app_dir / "app"

    pth_entries = {
        "zz_modex_agent_src.pth": repo_root / "src",
        "zz_modexbot_bot.pth": repo_root / "examples" / "bot_project",
    }

    print("\n=== Creating .pth files (editable source links) ===")
    for filename, target_dir in pth_entries.items():
        pth_path = site_packages / filename
        pth_path.write_text(str(target_dir) + "\n", encoding="utf-8")
        print(f"  {filename} -> {target_dir}")


# ── Step 2: Config init ─────────────────────────────────────────────────────


def init_config(app_dir: Path) -> None:
    """Copy .example files → real config files (only if missing)."""
    bot_project = app_dir / "app" / "examples" / "bot_project"
    config_dir = bot_project / "config"

    copies: list[tuple[str, str]] = [
        (".env.example", ".env"),
    ]
    if config_dir.exists():
        for example in config_dir.glob("*.example.yml"):
            real = example.name.replace(".example.yml", ".yml")
            copies.append((f"config/{example.name}", f"config/{real}"))

    mcp_example = config_dir / "mcp" / "registry.example.json"
    if mcp_example.exists():
        copies.append(("config/mcp/registry.example.json", "config/mcp/registry.json"))

    print("\n=== Initialising configuration ===")
    for src_rel, dst_rel in copies:
        src = bot_project / src_rel
        dst = bot_project / dst_rel
        if _copy_if_missing(src, dst):
            print(f"  Created: {dst_rel}")
        else:
            print(f"  Exists:  {dst_rel} (skipped)")


# ── Step 3: Runtime dirs ────────────────────────────────────────────────────


def create_runtime_dirs(app_dir: Path) -> None:
    bot_project = app_dir / "app" / "examples" / "bot_project"
    for dirname in ("logs", "data", ".modex"):
        path = bot_project / dirname
        path.mkdir(parents=True, exist_ok=True)
        print(f"  Directory ready: {dirname}/")


def create_cli_shims(app_dir: Path) -> None:
    python_exe = app_dir / "python" / "python.exe"
    scripts_dir = app_dir / "python" / "Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # The port comes from config/bot_config.yml (webui.port) — no env override
    # needed. MODEXBOT_PORT env still works as an optional escape hatch.
    shims = {
        "modexbot.bat": f'@echo off\r\n"{python_exe}" -m modexbot %*',
        "modexctl.bat": f'@echo off\r\n"{python_exe}" -c "from modexctl.main import main; main()" %*',
    }

    print("\n=== Creating CLI shims ===")
    for name, content in shims.items():
        shim_path = scripts_dir / name
        shim_path.write_text(content, encoding="ascii")
        print(f"  {shim_path}")


# ── Step 4: Verify ──────────────────────────────────────────────────────────


def verify_imports(app_dir: Path) -> None:
    """Smoke-test: project code + key deps are importable."""
    python_exe = app_dir / "python" / "python.exe"
    bot_project = app_dir / "app" / "examples" / "bot_project"

    env = dict(os.environ)
    env["PATH"] = str(app_dir / "python") + os.pathsep + env.get("PATH", "")

    checks = [
        "import modex_agent; print('  modex_agent OK')",
        "import modexctl; print('  modexctl OK')",
        "import modexbot; print('  modexbot OK')",
        "import bot; print('  bot OK')",
        "import aiosqlite; print('  aiosqlite OK')",
    ]

    print("\n=== Verifying imports ===")
    for stmt in checks:
        result = subprocess.run(
            [str(python_exe), "-c", stmt],
            capture_output=True, text=True, env=env,
            cwd=str(bot_project),
        )
        if result.returncode == 0:
            print(result.stdout.strip())
        else:
            print(f"  FAILED: {stmt}")
            print(f"    {result.stderr.strip()[:200]}")


# ── Done ────────────────────────────────────────────────────────────────────


def print_next_steps(app_dir: Path) -> None:
    python_exe = app_dir / "python" / "python.exe"
    bot_project = app_dir / "app" / "examples" / "bot_project"

    # Read the port from config so the printed URL always matches reality.
    port_result = subprocess.run(
        [str(python_exe), "-c",
         "from bot.config.webui_config import load_webui_port; print(load_webui_port())"],
        capture_output=True, text=True, cwd=str(bot_project),
    )
    port = port_result.stdout.strip() or "21800"

    print("\n" + "=" * 60)
    print("  ModexBot installation complete!")
    print("=" * 60)
    print()
    print("  Next steps:")
    print()
    print("    1. Configure your model (API key, provider, model):")
    print()
    print(f'       "{python_exe}" -m modexbot config')
    print()
    print("    2. Start the bot:")
    print()
    print("       Double-click the 'ModexBot' desktop icon,")
    print("       or run:")
    print(f'       "{python_exe}" -m modexbot start')
    print()
    print("    3. Open the WebUI in your browser:")
    print()
    print(f"       http://localhost:{port}/webui/")
    print()
    print("=" * 60)


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="ModexBot post-installation.")
    parser.add_argument("--app-dir", type=Path, required=True)
    args = parser.parse_args()
    app_dir = args.app_dir.resolve()

    print(f"  App directory: {app_dir}")

    create_pth_files(app_dir)
    create_runtime_dirs(app_dir)
    create_cli_shims(app_dir)
    init_config(app_dir)
    verify_imports(app_dir)
    print_next_steps(app_dir)


if __name__ == "__main__":
    main()
