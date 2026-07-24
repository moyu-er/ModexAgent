"""Stage ``rg.exe`` from a local cache into ``staging/bin/windows/``.

The cache lives at ``packaging/windows/bin/windows/rg.exe`` (gitignored —
binary artifact, not source).  On first run the script downloads the
pinned ripgrep release into the cache; subsequent builds copy from the
cache without any network access.

ripgrep is the **only** bundled CLI — it powers both ``GlobTool``
(``rg --files``) and ``SearchFilesTool`` (``rg`` content search).  The
``fd`` fallback in ``GlobTool`` is never reached when ``rg`` is bundled,
and ``SearchFilesTool`` falls back to ``git grep`` → Python ``re``
(neither uses ``fd``).  See ``glob_tool.py`` / ``search_tool.py``.

Usage::

    python prepare_bundled_bin.py --staging-dir staging
    # → staging/bin/windows/rg.exe  (copied from cache or downloaded)
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

_SCRIPT_DIR = Path(__file__).resolve().parent

_RG_VERSION = "15.1.0"
_RG_URL = (
    f"https://github.com/BurntSushi/ripgrep/releases/download/"
    f"{_RG_VERSION}/ripgrep-{_RG_VERSION}-x86_64-pc-windows-msvc.zip"
)


def _cache_dir() -> Path:
    return _SCRIPT_DIR / "bin" / "windows"


def _ensure_cached(name: str, url: str) -> Path:
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / name

    if target.exists():
        print(f"  [prepare_bundled_bin] Cache hit: {target}")
        return target

    system_bin = shutil.which(name)
    if system_bin:
        print(f"  [prepare_bundled_bin] Copying system {name} -> cache: {system_bin}")
        shutil.copy2(system_bin, target)
        return target

    print(f"  [prepare_bundled_bin] Downloading {url} ...")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        urllib.request.urlretrieve(url, tmp.name)
        zip_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            entry = next((n for n in zf.namelist() if n.endswith(name)), None)
            if entry is None:
                print(f"  [prepare_bundled_bin] ERROR: {name} not found in archive")
                sys.exit(1)
            with zf.open(entry) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    finally:
        zip_path.unlink(missing_ok=True)

    result = subprocess.run(
        [str(target), "--version"], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(
            f"  [prepare_bundled_bin] ERROR: verification failed: "
            f"{result.stderr.strip()[:200]}"
        )
        sys.exit(1)

    print(f"  [prepare_bundled_bin] Cached: {target} ({result.stdout.strip()})")
    return target


def prepare_bundled_bin(staging_dir: Path) -> Path:
    staging_bin = staging_dir / "bin" / "windows"
    staging_bin.mkdir(parents=True, exist_ok=True)

    cached = _ensure_cached("rg.exe", _RG_URL)
    dst = staging_bin / "rg.exe"
    shutil.copy2(cached, dst)
    print(f"  [prepare_bundled_bin] Staged: {dst}")
    return staging_bin


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage rg.exe from local cache into installer staging."
    )
    parser.add_argument("--staging-dir", type=Path, default=Path("staging"))
    args = parser.parse_args()
    prepare_bundled_bin(args.staging_dir)


if __name__ == "__main__":
    main()
