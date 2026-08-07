"""Unit tests for :mod:`modex_agent.agents.external.cli_resolver`.

Covers the four-level resolution strategy and the ``ModexctlResolutionError``
contract. The resolver is the single source of truth for the spawn ``PATH``
prepend directory — replacing the previous inline ``shutil.which`` + silent
``Path(".")`` fallback that caused cross-pool messaging flakes.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest

from modex_agent.agents.external.cli_resolver import (
    ModexctlResolutionError,
    _has_modexctl,
    _sibling_bin_dir,
    resolve_modexctl_bin_dir,
)

# ---------------------------------------------------------------------------
# _has_modexctl — pure file-existence probe
# ---------------------------------------------------------------------------


class TestHasModexctl:
    def test_returns_false_for_nonexistent_directory(self, tmp_path: Path) -> None:
        assert _has_modexctl(tmp_path / "does-not-exist") is False

    def test_returns_false_for_empty_directory(self, tmp_path: Path) -> None:
        assert _has_modexctl(tmp_path) is False

    @pytest.mark.parametrize("filename", ["modexctl", "modexctl.exe", "modexctl.bat"])
    def test_returns_true_when_runnable_present(self, tmp_path: Path, filename: str) -> None:
        (tmp_path / filename).touch()
        assert _has_modexctl(tmp_path) is True


# ---------------------------------------------------------------------------
# _sibling_bin_dir — sys.executable-derived candidate
# ---------------------------------------------------------------------------


class TestSiblingBinDir:
    def test_returns_scripts_on_windows_layout(self, tmp_path: Path) -> None:
        """When python.exe sits in a venv-style dir, return its Scripts/ subdir."""
        # Build a fake venv layout: <tmp>/python.exe + <tmp>/Scripts/
        fake_python = tmp_path / "python.exe"
        fake_python.touch()
        (tmp_path / "Scripts").mkdir()

        with mock.patch.object(sys, "executable", str(fake_python)):
            with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                result = _sibling_bin_dir()

        assert result == tmp_path / "Scripts"

    def test_returns_executable_parent_when_no_scripts_subdir_on_windows(
        self, tmp_path: Path
    ) -> None:
        """python-build-standalone layout: scripts sit next to python.exe."""
        fake_python = tmp_path / "python.exe"
        fake_python.touch()
        # No Scripts/ subdir

        with mock.patch.object(sys, "executable", str(fake_python)):
            with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                result = _sibling_bin_dir()

        assert result == tmp_path

    def test_returns_executable_parent_on_posix(self, tmp_path: Path) -> None:
        fake_python = tmp_path / "python"
        fake_python.touch()

        with mock.patch.object(sys, "executable", str(fake_python)):
            with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "linux"):
                result = _sibling_bin_dir()

        assert result == tmp_path


# ---------------------------------------------------------------------------
# resolve_modexctl_bin_dir — the four-level strategy
# ---------------------------------------------------------------------------


class TestResolveModexctlBinDir:
    """Strategy priority (highest first):
    1. MODEXBOT_BIN_DIR env override
    2. sibling of sys.executable
    3. shutil.which parent
    4. raise ModexctlResolutionError
    """

    def test_strategy_1_env_override_wins_when_present(self, tmp_path: Path) -> None:
        """Env override is authoritative when it points at a directory with modexctl."""
        override_dir = tmp_path / "override"
        override_dir.mkdir()
        (override_dir / "modexctl.exe").touch()

        sibling_dir = tmp_path / "sibling"
        sibling_dir.mkdir()
        (sibling_dir / "modexctl.exe").touch()

        fake_python = tmp_path / "python.exe"
        fake_python.touch()

        with mock.patch.dict(os.environ, {"MODEXBOT_BIN_DIR": str(override_dir)}):
            with mock.patch.object(sys, "executable", str(fake_python)):
                with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                    result = resolve_modexctl_bin_dir()

        assert result == override_dir

    def test_strategy_1_stale_override_falls_through_to_strategy_2(self, tmp_path: Path) -> None:
        """Stale override (directory doesn't exist) silently falls through."""
        stale_override = tmp_path / "does-not-exist"
        sibling_dir = tmp_path / "Scripts"
        sibling_dir.mkdir()
        (sibling_dir / "modexctl.exe").touch()

        fake_python = tmp_path / "python.exe"
        fake_python.touch()

        with mock.patch.dict(os.environ, {"MODEXBOT_BIN_DIR": str(stale_override)}):
            with mock.patch.object(sys, "executable", str(fake_python)):
                with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                    result = resolve_modexctl_bin_dir()

        assert result == sibling_dir

    def test_strategy_1_override_without_modexctl_falls_through(self, tmp_path: Path) -> None:
        """Override dir exists but has no modexctl → fall through to strategy 2."""
        empty_override = tmp_path / "empty-override"
        empty_override.mkdir()

        sibling_dir = tmp_path / "Scripts"
        sibling_dir.mkdir()
        (sibling_dir / "modexctl.exe").touch()

        fake_python = tmp_path / "python.exe"
        fake_python.touch()

        with mock.patch.dict(os.environ, {"MODEXBOT_BIN_DIR": str(empty_override)}):
            with mock.patch.object(sys, "executable", str(fake_python)):
                with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                    result = resolve_modexctl_bin_dir()

        assert result == sibling_dir

    def test_strategy_2_sibling_of_sys_executable(self, tmp_path: Path) -> None:
        """No env override → sibling of sys.executable (the wheel layout)."""
        sibling_dir = tmp_path / "Scripts"
        sibling_dir.mkdir()
        (sibling_dir / "modexctl.exe").touch()

        fake_python = tmp_path / "python.exe"
        fake_python.touch()

        env_without_override = {k: v for k, v in os.environ.items() if k != "MODEXBOT_BIN_DIR"}
        with mock.patch.dict(os.environ, env_without_override, clear=True):
            with mock.patch.object(sys, "executable", str(fake_python)):
                with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                    result = resolve_modexctl_bin_dir()

        assert result == sibling_dir

    def test_strategy_3_shutil_which_fallback(self, tmp_path: Path) -> None:
        """No env override, no modexctl next to sys.executable → shutil.which."""
        which_dir = tmp_path / "which-dir"
        which_dir.mkdir()
        which_exe = which_dir / "modexctl.exe"

        fake_python = tmp_path / "python.exe"
        fake_python.touch()
        # Make sure sibling doesn't have modexctl:
        (tmp_path / "Scripts").mkdir()  # exists but empty

        env_without_override = {k: v for k, v in os.environ.items() if k != "MODEXBOT_BIN_DIR"}
        with mock.patch.dict(os.environ, env_without_override, clear=True):
            with mock.patch.object(sys, "executable", str(fake_python)):
                with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                    with mock.patch.object(shutil, "which", return_value=str(which_exe)):
                        result = resolve_modexctl_bin_dir()

        assert result == which_dir

    def test_strategy_4_raises_with_diagnostic_context_when_all_fail(self, tmp_path: Path) -> None:
        """All strategies fail → ModexctlResolutionError with diagnostics."""
        fake_python = tmp_path / "python.exe"
        fake_python.touch()
        # No Scripts/ subdir → sibling returns tmp_path itself, which has no modexctl
        # shutil.which returns None
        env_without_override = {k: v for k, v in os.environ.items() if k != "MODEXBOT_BIN_DIR"}

        with mock.patch.dict(os.environ, env_without_override, clear=True):
            with mock.patch.object(sys, "executable", str(fake_python)):
                with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                    with mock.patch.object(shutil, "which", return_value=None):
                        with pytest.raises(ModexctlResolutionError) as exc_info:
                            resolve_modexctl_bin_dir()

        err = exc_info.value
        assert err.sys_executable == str(fake_python)
        assert err.which_result is None
        # Error message carries the diagnostic context (not just a generic message)
        assert "sys.executable" in str(err)
        assert "shutil.which" in str(err)
        assert "MODEXBOT_BIN_DIR" in str(err)

    def test_strategy_3_returns_which_parent_even_when_file_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        """shutil.which returns a non-None string → strategy 3 returns its
        parent directory unconditionally (no _has_modexctl probe on the
        result — shutil.which itself is the existence check). This documents
        the contract that strategy 3 trusts which's verdict.
        """
        fake_python = tmp_path / "python.exe"
        fake_python.touch()
        which_parent = tmp_path / "ghost-dir"
        which_parent.mkdir()  # parent exists, but no modexctl file inside
        stale_which_path = str(which_parent / "modexctl.exe")

        env_without_override = {k: v for k, v in os.environ.items() if k != "MODEXBOT_BIN_DIR"}

        with mock.patch.dict(os.environ, env_without_override, clear=True):
            with mock.patch.object(sys, "executable", str(fake_python)):
                with mock.patch("modex_agent.agents.external.cli_resolver.sys.platform", "win32"):
                    with mock.patch.object(shutil, "which", return_value=stale_which_path):
                        result = resolve_modexctl_bin_dir()

        # Strategy 3 trusts shutil.which's verdict — returns parent verbatim
        assert result == which_parent
