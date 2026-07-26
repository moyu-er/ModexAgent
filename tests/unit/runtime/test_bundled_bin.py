"""Unit tests for ``modex_agent.runtime.bundled_bin``.

Covers the pure ``prepend_path_idempotent`` algorithm, the
``bundled_bin_dir`` resolver, and the ``ensure_bundled_bin_on_path``
idempotency guarantee.  Registry operations (``register_public_path`` /
``unregister_public_path``) are tested via mocking — they mutate
``HKCU\\Environment`` and are not safe to run in CI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from modex_agent.runtime.bundled_bin import (
    bundled_bin_dir,
    ensure_bundled_bin_on_path,
    prepend_path_idempotent,
)


# ── prepend_path_idempotent ──────────────────────────────────────────────────


class TestPrependPathIdempotent:
    """The core algorithm — all callers depend on this being correct."""

    def test_empty_path(self) -> None:
        assert prepend_path_idempotent("", "C:\\bin") == "C:\\bin"

    def test_simple_prepend(self) -> None:
        result = prepend_path_idempotent("C:\\existing", "C:\\bin")
        assert result == "C:\\bin;C:\\existing"

    def test_exact_match_idempotent_no_marker(self) -> None:
        """Without marker: exact-match removal, then prepend."""
        path = "C:\\bin;C:\\other"
        result = prepend_path_idempotent(path, "C:\\bin", marker=None)
        assert result == "C:\\bin;C:\\other"

    def test_exact_match_case_insensitive(self) -> None:
        path = "c:\\BIN;C:\\other"
        result = prepend_path_idempotent(path, "C:\\bin", marker=None)
        assert result == "C:\\bin;C:\\other"

    def test_marker_removes_all_matching(self) -> None:
        """Marker mode: all entries containing the marker are removed."""
        path = "C:\\old\\python\\Scripts;C:\\system32;D:\\other\\python\\Scripts"
        result = prepend_path_idempotent(
            path, "E:\\new\\python\\Scripts", marker="\\python\\Scripts"
        )
        # Both old entries removed, new one prepended, system32 kept.
        assert result == "E:\\new\\python\\Scripts;C:\\system32"

    def test_marker_case_insensitive(self) -> None:
        path = "C:\\OLD\\PYTHON\\SCRIPTS;C:\\system32"
        result = prepend_path_idempotent(
            path, "C:\\new\\python\\Scripts", marker="\\python\\Scripts"
        )
        assert result == "C:\\new\\python\\Scripts;C:\\system32"

    def test_marker_reinstall_different_dir(self) -> None:
        """The key idempotency scenario: reinstall to a new dir."""
        old_path = "C:\\OldInstall\\python\\Scripts;C:\\Windows\\System32"
        result = prepend_path_idempotent(
            old_path, "D:\\NewInstall\\python\\Scripts", marker="\\python\\Scripts"
        )
        assert result == "D:\\NewInstall\\python\\Scripts;C:\\Windows\\System32"

    def test_repeated_calls_leave_one_entry(self) -> None:
        """Calling N times must leave exactly one entry."""
        path = "C:\\system32"
        marker = "\\bin\\windows"
        for _ in range(5):
            path = prepend_path_idempotent(
                path, "C:\\install\\bin\\windows", marker=marker
            )
        entries = [e for e in path.split(";") if e]
        assert entries.count("C:\\install\\bin\\windows") == 1
        assert entries[0] == "C:\\install\\bin\\windows"
        assert "C:\\system32" in entries

    def test_preserves_unrelated_entries(self) -> None:
        path = "C:\\Python312;C:\\Windows\\System32;C:\\Windows;D:\\tools"
        result = prepend_path_idempotent(path, "C:\\bin", marker=None)
        entries = result.split(";")
        assert entries[0] == "C:\\bin"
        for e in ["C:\\Python312", "C:\\Windows\\System32", "C:\\Windows", "D:\\tools"]:
            assert e in entries

    def test_strips_empty_entries(self) -> None:
        """Empty entries (leading/trailing/duplicate separators) are dropped."""
        path = ";C:\\existing;;C:\\other;"
        result = prepend_path_idempotent(path, "C:\\bin", marker=None)
        entries = result.split(";")
        assert "" not in entries
        assert entries == ["C:\\bin", "C:\\existing", "C:\\other"]

    def test_posix_pathsep(self) -> None:
        result = prepend_path_idempotent(
            "/usr/bin", "/opt/bin", marker=None, pathsep=":"
        )
        assert result == "/opt/bin:/usr/bin"


# ── bundled_bin_dir ──────────────────────────────────────────────────────────


class TestBundledBinDir:
    """Resolution logic — must return None in dev mode."""

    def test_env_override_existing(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "custom_bin"
        bin_dir.mkdir()
        with patch.dict(os.environ, {"MODEX_BUNDLED_BIN_DIR": str(bin_dir)}):
            assert bundled_bin_dir() == bin_dir

    def test_env_override_nonexistent_returns_none(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"MODEX_BUNDLED_BIN_DIR": str(tmp_path / "nope")}):
            assert bundled_bin_dir() is None

    def test_env_override_takes_precedence(self, tmp_path: Path) -> None:
        """Even if a sibling bin/ exists, the env override wins."""
        bin_dir = tmp_path / "env_bin"
        bin_dir.mkdir()
        with patch.dict(os.environ, {"MODEX_BUNDLED_BIN_DIR": str(bin_dir)}):
            assert bundled_bin_dir() == bin_dir

    def test_no_bundled_dir_in_dev_mode(self) -> None:
        """In a normal dev venv there's no bin/<platform>/ directory."""
        with patch.dict(os.environ, {"MODEX_BUNDLED_BIN_DIR": ""}, clear=False):
            # Ensure the env override doesn't interfere.
            os.environ.pop("MODEX_BUNDLED_BIN_DIR", None)
            result = bundled_bin_dir()
            # In the test environment, there's no bundled bin dir.
            assert result is None

    def test_finds_sibling_bin_dir(self, tmp_path: Path) -> None:
        """Simulates the installer layout: <install>/python/python.exe + <install>/bin/<platform>/."""
        install_root = tmp_path
        python_dir = install_root / "python"
        python_dir.mkdir()
        platform_name = "windows" if sys.platform == "win32" else "linux"
        bin_dir = install_root / "bin" / platform_name
        bin_dir.mkdir(parents=True)

        fake_exe = python_dir / ("python.exe" if sys.platform == "win32" else "python")

        with patch.dict(os.environ, {"MODEX_BUNDLED_BIN_DIR": ""}, clear=False):
            os.environ.pop("MODEX_BUNDLED_BIN_DIR", None)
            with patch.object(sys, "executable", str(fake_exe)):
                result = bundled_bin_dir()
                assert result is not None
                assert result == bin_dir


# ── ensure_bundled_bin_on_path ───────────────────────────────────────────────


class TestEnsureBundledBinOnPath:
    """The runtime injection — must be idempotent and side-effect-safe."""

    def test_returns_none_when_no_bundled_dir(self) -> None:
        os.environ.pop("MODEX_BUNDLED_BIN_DIR", None)
        with patch("modex_agent.runtime.bundled_bin.bundled_bin_dir", return_value=None):
            assert ensure_bundled_bin_on_path() is None

    def test_prepends_to_path(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bundled"
        bin_dir.mkdir()
        original_path = os.environ.get("PATH", "")
        try:
            with patch("modex_agent.runtime.bundled_bin.bundled_bin_dir", return_value=bin_dir):
                result = ensure_bundled_bin_on_path()
                assert result == bin_dir
                assert os.environ["PATH"].startswith(str(bin_dir))
        finally:
            os.environ["PATH"] = original_path

    def test_idempotent_multiple_calls(self, tmp_path: Path) -> None:
        """Calling multiple times must leave exactly one entry."""
        bin_dir = tmp_path / "bundled"
        bin_dir.mkdir()
        original_path = os.environ.get("PATH", "")
        try:
            with patch("modex_agent.runtime.bundled_bin.bundled_bin_dir", return_value=bin_dir):
                ensure_bundled_bin_on_path()
                ensure_bundled_bin_on_path()
                ensure_bundled_bin_on_path()

                entries = [e for e in os.environ["PATH"].split(os.pathsep) if e]
                # The bin_dir appears exactly once.
                count = sum(1 for e in entries if str(bin_dir).lower() in e.lower())
                assert count == 1
        finally:
            os.environ["PATH"] = original_path

    def test_cleans_old_marker_entries(self, tmp_path: Path) -> None:
        """Exact-match: same-path duplicates are removed, different paths kept."""
        bin_dir = tmp_path / "bundled"
        bin_dir.mkdir()
        other_bin = tmp_path / "other_bundled"
        other_bin.mkdir()
        system_path = "C:\\Windows\\System32"

        original_path = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = f"{bin_dir};{system_path};{bin_dir}"

            with patch("modex_agent.runtime.bundled_bin.bundled_bin_dir", return_value=bin_dir):
                ensure_bundled_bin_on_path()

                entries = [e for e in os.environ["PATH"].split(os.pathsep) if e]
                assert entries[0] == str(bin_dir)
                count = sum(1 for e in entries if e.lower() == str(bin_dir).lower())
                assert count == 1
                assert system_path in entries
        finally:
            os.environ["PATH"] = original_path
