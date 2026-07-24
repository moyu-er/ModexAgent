"""Test fixtures for glob tool — downloads ripgrep for deterministic backend testing.

Provides:
  - ``rg_binary`` (session): path to downloaded rg, or None if unavailable
  - ``rg_env`` (function): puts rg on PATH for tests that need the rg backend
  - ``no_rg_env`` (function): hides rg/fd from PATH for Python fallback tests

The rg binary is cached under ``<temp>/modex_test_rg/`` so repeated runs
don't re-download.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import pytest

RG_VERSION = "14.1.1"
_CACHE_DIR = Path(os.environ.get("TEMP", os.environ.get("TMP", "/tmp"))) / "modex_test_rg"


def _rg_target() -> str:
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "x86_64-pc-windows-msvc"
    if sys.platform == "darwin":
        return "aarch64-apple-darwin" if machine in ("arm64", "aarch64") else "x86_64-apple-darwin"
    if machine in ("aarch64", "arm64"):
        return "aarch64-unknown-linux-gnu"
    return "x86_64-unknown-linux-gnu"


def _rg_ext() -> str:
    return ".zip" if sys.platform == "win32" else ".tar.gz"


def _rg_binary_name() -> str:
    return "rg.exe" if sys.platform == "win32" else "rg"


def _download_rg() -> Path | None:
    target = _rg_target()
    ext = _rg_ext()
    bin_name = _rg_binary_name()
    version_dir = _CACHE_DIR / f"rg-{RG_VERSION}"
    rg_path = version_dir / bin_name

    if rg_path.exists():
        return rg_path

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    archive_name = f"ripgrep-{RG_VERSION}-{target}{ext}"
    url = f"https://github.com/BurntSushi/ripgrep/releases/download/{RG_VERSION}/{archive_name}"
    archive_path = _CACHE_DIR / archive_name

    try:
        urllib.request.urlretrieve(url, archive_path)
    except Exception:
        return None

    version_dir.mkdir(parents=True, exist_ok=True)

    if ext == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(version_dir)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(version_dir)

    for p in version_dir.rglob(bin_name):
        if p.is_file():
            if sys.platform != "win32":
                p.chmod(0o755)
            return p

    return None


@pytest.fixture(scope="session")
def rg_binary() -> Path | None:
    if shutil.which("rg") is not None:
        return Path(shutil.which("rg"))  # type: ignore[arg-type]
    return _download_rg()


@pytest.fixture
def rg_env(rg_binary: Path | None, monkeypatch: pytest.MonkeyPatch) -> None:
    if rg_binary is None:
        pytest.skip("ripgrep unavailable for testing")
    rg_dir = str(rg_binary.parent)
    current_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{rg_dir}{os.pathsep}{current_path}")


@pytest.fixture
def no_rg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    original_which = shutil.which

    def _mock_which(cmd: str, *args: object, **kwargs: object) -> str | None:
        if cmd in ("rg", "fd"):
            return None
        return original_which(cmd, *args, **kwargs)  # type: ignore[misc]

    monkeypatch.setattr(shutil, "which", _mock_which)
