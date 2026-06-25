from __future__ import annotations

import os
from pathlib import Path

import pytest

from modex_agent.sandbox.guard import CommandSeverity, GuardMatch, GuardResult
from modex_agent.sandbox.guard_path import PathBoundaryConfig, PathBoundaryGuard


class TestPathBoundaryGuardPOSIX:
    """POSIX absolute path boundary checks."""

    def test_posix_absolute_outside_blocked(self, tmp_path: Path) -> None:
        """cat /etc/passwd with workspace elsewhere -> blocked."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check("cat /etc/passwd")
        assert not result.allowed
        assert result.reason is not None
        assert "path_boundary" in result.reason
        assert any(m.category == "path_boundary" for m in result.matches)

    def test_posix_absolute_inside_allowed(self, tmp_path: Path) -> None:
        """cat <workspace>/file.txt -> allowed."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check(f"cat {tmp_path}/file.txt")
        assert result.allowed

    def test_relative_ignored(self, tmp_path: Path) -> None:
        """Relative paths are not checked."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check("cat file.txt")
        assert result.allowed

    def test_device_path_allowed(self, tmp_path: Path) -> None:
        """Benign device paths are allowed."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check("cat /dev/null")
        assert result.allowed


class TestPathBoundaryGuardWindows:
    """Windows absolute path boundary checks."""

    def test_windows_absolute_outside_blocked(self, tmp_path: Path) -> None:
        """type C:\Windows\System32 with workspace elsewhere -> blocked."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check(r"type C:\Windows\System32")
        assert not result.allowed
        assert any(m.category == "path_boundary" for m in result.matches)


class TestPathBoundaryGuardHome:
    """Home-relative path boundary checks."""

    def test_home_path_expanded(self, tmp_path: Path) -> None:
        """cat ~/.ssh/id_rsa -> blocked (home is outside workspace)."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check("cat ~/.ssh/id_rsa")
        assert not result.allowed
        assert any(m.category == "path_boundary" for m in result.matches)


class TestPathBoundaryGuardEnvVar:
    """Environment variable expansion in paths."""

    def test_env_var_expanded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """cat /tmp/$FAKE_HOME/.bashrc -> blocked when expanded path is outside workspace."""
        # The regex extracts the raw path literal /tmp/$FAKE_HOME/.bashrc.
        # After expandvars it becomes /tmp/outside/.bashrc, which is outside workspace.
        monkeypatch.setenv("FAKE_HOME", "outside")
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check("cat /tmp/$FAKE_HOME/.bashrc")
        assert not result.allowed
        assert any(m.category == "path_boundary" for m in result.matches)


class TestPathBoundaryGuardAllowPaths:
    """Extra allowed paths outside workspace_root."""

    def test_allow_paths_extension(self, tmp_path: Path) -> None:
        """cat /tmp/file.txt with allow_paths=('/tmp',) -> allowed."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(
                workspace_root=str(tmp_path),
                allow_paths=("/tmp",),
            )
        )
        result = guard.check("cat /tmp/file.txt")
        assert result.allowed


class TestPathBoundaryGuardConfig:
    """Configuration edge cases."""

    def test_disabled_guard(self, tmp_path: Path) -> None:
        """enabled=False -> all commands allowed."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(
                workspace_root=str(tmp_path),
                enabled=False,
            )
        )
        result = guard.check("cat /etc/passwd")
        assert result.allowed

    def test_no_workspace_no_check(self) -> None:
        """workspace_root=None -> no boundary checking performed."""
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=None)
        )
        result = guard.check("cat /etc/passwd")
        assert result.allowed


class TestPathBoundaryGuardResultStructure:
    """GuardResult and GuardMatch structure for path boundary denials."""

    def test_denied_result_has_matches(self, tmp_path: Path) -> None:
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check("cat /etc/passwd")
        assert result.allowed is False
        assert len(result.matches) > 0
        assert result.reason is not None
        for match in result.matches:
            assert isinstance(match, GuardMatch)
            assert match.pattern == "<path-boundary>"
            assert match.severity == CommandSeverity.CRITICAL
            assert match.category == "path_boundary"
            assert "outside workspace" in match.description

    def test_allowed_result(self, tmp_path: Path) -> None:
        guard = PathBoundaryGuard(
            PathBoundaryConfig(workspace_root=str(tmp_path))
        )
        result = guard.check("cat file.txt")
        assert result.allowed is True
        assert result.matches == ()
        assert result.reason is None
