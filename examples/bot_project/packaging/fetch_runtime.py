"""Download uv.exe into the staging directory.

uv is the only runtime we bundle — it downloads Python 3.12 automatically
when creating the venv during installation.  This keeps the installer small
(~15 MB for uv vs ~80 MB for a bundled CPython) and lets uv pick the
correct python-build-standalone release for the host platform.

If the build machine already has uv on PATH, that copy is used instead of
downloading.

Usage::

    python fetch_runtime.py --staging-dir staging
    # → staging/runtime/uv.exe
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# --- Constants ---------------------------------------------------------------

_UV_DOWNLOAD_URL = (
    "https://github.com/astral-sh/uv/releases/latest/download/"
    "uv-x86_64-pc-windows-msvc.zip"
)


# --- Public API --------------------------------------------------------------


def fetch_uv(staging_dir: Path) -> Path:
    """Ensure ``staging_dir/runtime/uv.exe`` exists.

    Uses a system-installed uv if available; otherwise downloads the latest
    standalone release from GitHub.
    """
    runtime_dir = staging_dir / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target = runtime_dir / "uv.exe"

    # 1. Prefer uv already on PATH (faster, no download)
    system_uv = shutil.which("uv")
    if system_uv:
        print(f"  [fetch_runtime] Using system uv: {system_uv}")
        shutil.copy2(system_uv, target)
        return target

    # 2. Download from GitHub releases
    if target.exists():
        print(f"  [fetch_runtime] uv.exe already present: {target}")
        return target

    print(f"  [fetch_runtime] Downloading uv from {_UV_DOWNLOAD_URL} ...")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        urllib.request.urlretrieve(_UV_DOWNLOAD_URL, tmp.name)
        zip_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            # The zip contains uv.exe at the root
            uv_entry = next(
                (n for n in zf.namelist() if n.endswith("uv.exe")),
                None,
            )
            if uv_entry is None:
                print("  [fetch_runtime] ERROR: uv.exe not found in archive")
                sys.exit(1)
            with zf.open(uv_entry) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    finally:
        zip_path.unlink(missing_ok=True)

    # Verify
    result = subprocess.run([str(target), "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [fetch_runtime] ERROR: uv verification failed: {result.stderr}")
        sys.exit(1)

    print(f"  [fetch_runtime] uv ready: {target} ({result.stdout.strip()})")
    return target


# --- CLI ---------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Download uv.exe for installer staging.")
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=Path("staging"),
        help="Staging directory root (default: staging)",
    )
    args = parser.parse_args()
    fetch_uv(args.staging_dir)


if __name__ == "__main__":
    main()
