from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.config import SandboxConfig


class TestSandboxConfigDeadFieldsRemoved:
    """deny_dirs / max_file_size_mb were dead config (zero readers) — removed."""

    def test_deny_dirs_not_a_field(self) -> None:
        assert "deny_dirs" not in SandboxConfig.model_fields

    def test_max_file_size_mb_not_a_field(self) -> None:
        assert "max_file_size_mb" not in SandboxConfig.model_fields

    def test_deny_dirs_rejected_as_extra(self) -> None:
        with pytest.raises(ValidationError):
            SandboxConfig(deny_dirs=["/home"])

    def test_max_file_size_mb_rejected_as_extra(self) -> None:
        with pytest.raises(ValidationError):
            SandboxConfig(max_file_size_mb=50)


class TestSandboxConfigWorkspaceDirNormalization:
    """workspace_dir is normalized to an absolute path via validator — no
    object.__setattr__ (rule 17: fix frozen-model mutation at the root)."""

    def test_relative_workspace_dir_becomes_absolute(self, tmp_path: Path) -> None:
        config = SandboxConfig(workspace_dir=str(tmp_path / "ws"))
        assert Path(config.workspace_dir).is_absolute()

    def test_default_workspace_dir_is_absolute(self) -> None:
        config = SandboxConfig()
        assert Path(config.workspace_dir).is_absolute()

    def test_workspace_dir_stored_absolute(self) -> None:
        """The stored value itself is absolute, not just a read-time view."""
        config = SandboxConfig(workspace_dir="relative_ws")
        assert Path(config.workspace_dir).is_absolute()


class TestSandboxConfigFrozen:
    def test_assignment_rejected(self) -> None:
        config = SandboxConfig()
        with pytest.raises(ValidationError):
            config.enable_network = True
