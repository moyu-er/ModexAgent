"""Prepare a self-contained Python runtime with all dependencies pre-installed.

Strategy: copy site-packages from the project's existing .venv (where all
deps are already installed and working) into a fresh python-build-standalone.
Then strip the project's own packages — third-party deps remain.

At install time, .pth files make the source on disk importable, giving users
an editable-code experience with zero network, zero compilation.

Usage::

    python prepare_python.py --staging-dir staging
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent


def _find_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        cwd=str(_SCRIPT_DIR),
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return _SCRIPT_DIR.parents[2]


_REPO_ROOT = _find_repo_root()
_EXISTING_VENV = _REPO_ROOT / ".venv"

_STRIP_EXACT = [
    "modex_agent",
    "modexctl",
    "modexbot",
    "bot",
]

_STRIP_PATTERNS = [
    "ModexAgent-*.dist-info",
    "modex_bot_project-*.dist-info",
    "_editable_impl_modexagent.pth",
    "_editable_impl_modex_bot_project.pth",
    "__editable___modexagent_*.py",
    "__editable___modex_bot_project_*.py",
]


def _find_uv() -> str:
    uv = shutil.which("uv")
    if uv:
        return uv
    candidates = [
        str(Path.home() / ".local" / "bin" / "uv.exe"),
        str(Path.home() / ".local" / "bin" / "uv"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    print("  [prepare_python] ERROR: uv not found", file=sys.stderr)
    sys.exit(1)


def _get_standalone_python() -> Path:
    uv_exe = _find_uv()

    print("  [prepare_python] Ensuring Python 3.12 is installed via uv...")
    subprocess.run([uv_exe, "python", "install", "3.12"], check=True)

    result = subprocess.run(
        [uv_exe, "python", "dir"],
        capture_output=True, text=True, check=True,
    )
    uv_python_root = Path(result.stdout.strip())

    candidates = list(uv_python_root.glob("cpython-3.12.*"))
    if not candidates:
        print("  [prepare_python] ERROR: cpython-3.12 not found")
        sys.exit(1)

    python_dir = candidates[0]

    # python-build-standalone ships a pyvenv.cfg that makes it behave like a
    # venv — sys.path points to the original cache dir, not the copy.  Delete
    # it so the Python acts as a standalone interpreter with its own Lib/.
    pyvenv_cfg = python_dir / "pyvenv.cfg"
    if pyvenv_cfg.exists():
        pyvenv_cfg.unlink()
        print("  [prepare_python] Removed pyvenv.cfg (standalone mode)")

    return python_dir


def _copy_to_staging(src_python: Path, staging_dir: Path) -> Path:
    dst_python = staging_dir / "python"
    print(f"  [prepare_python] Copying Python: {src_python} -> {dst_python}")
    if dst_python.exists():
        shutil.rmtree(dst_python)
    shutil.copytree(src_python, dst_python)
    return dst_python


def _copy_site_packages(venv_dir: Path, python_dir: Path) -> None:
    src_sp = venv_dir / "Lib" / "site-packages"
    dst_sp = python_dir / "Lib" / "site-packages"

    if not src_sp.exists():
        print(f"  [prepare_python] ERROR: {src_sp} not found")
        sys.exit(1)

    print(f"  [prepare_python] Copying site-packages: {src_sp} -> {dst_sp}")

    for item in src_sp.iterdir():
        dst_item = dst_sp / item.name
        if dst_item.exists():
            if dst_item.is_dir():
                shutil.rmtree(dst_item)
            else:
                dst_item.unlink()
        if item.is_dir():
            shutil.copytree(item, dst_item)
        else:
            shutil.copy2(item, dst_item)

    sp_size = sum(f.stat().st_size for f in dst_sp.rglob("*") if f.is_file())
    print(f"  [prepare_python] site-packages copied: {sp_size / 1e6:.0f} MB")


def _strip_project_code(python_dir: Path) -> None:
    site_packages = python_dir / "Lib" / "site-packages"
    if not site_packages.exists():
        return

    print("  [prepare_python] Stripping project code from site-packages...")

    targets: list[Path] = []
    for name in _STRIP_EXACT:
        p = site_packages / name
        if p.exists():
            targets.append(p)
    for pattern in _STRIP_PATTERNS:
        targets.extend(site_packages.glob(pattern))

    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        print(f"    Removed: {target.name}")

    scripts_dir = python_dir / "Scripts"
    if scripts_dir.exists():
        for exe in list(scripts_dir.glob("modexbot*")) + list(scripts_dir.glob("modexctl*")):
            exe.unlink()
            print(f"    Removed script: {exe.name}")


def _verify(python_dir: Path) -> None:
    python_exe = python_dir / "python.exe"

    third_party = [
        "import pydantic; import aiohttp; import httpx; import yaml; import typer; import rich",
        "import litellm; import openai; import tiktoken; import questionary",
        "import PIL; import docx; import openpyxl; import pptx",
        "import aiosqlite",
    ]

    print("  [prepare_python] Verifying third-party imports...")
    ok = True
    for stmt in third_party:
        result = subprocess.run(
            [str(python_exe), "-c", stmt],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    FAILED: {stmt}")
            print(f"      {result.stderr.strip()[:200]}")
            ok = False
        else:
            print(f"    OK: {stmt.split(';')[0].replace('import ', '')}")

    result = subprocess.run(
        [str(python_exe), "-c", "import modex_agent"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("    WARNING: modex_agent still importable (strip may have failed)")
    else:
        print("    OK: project code stripped (modex_agent not importable without .pth)")

    if not ok:
        print("  [prepare_python] WARNING: some third-party imports failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare bundled Python runtime.")
    parser.add_argument("--staging-dir", type=Path, default=Path("staging"))
    parser.add_argument("--venv-dir", type=Path, default=_EXISTING_VENV,
                        help="Path to existing .venv with all deps installed")
    args = parser.parse_args()

    if not args.venv_dir.exists():
        print(f"  [prepare_python] ERROR: {args.venv_dir} not found")
        print("    Run the project install first (install.bat or uv pip install -e .)")
        sys.exit(1)

    src_python = _get_standalone_python()
    python_dir = _copy_to_staging(src_python, args.staging_dir)
    _copy_site_packages(args.venv_dir, python_dir)
    _strip_project_code(python_dir)
    _verify(python_dir)

    total = sum(f.stat().st_size for f in python_dir.rglob("*") if f.is_file())
    print(f"\n  [prepare_python] Done. Python runtime: {python_dir}")
    print(f"  [prepare_python] Total size: {total / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
