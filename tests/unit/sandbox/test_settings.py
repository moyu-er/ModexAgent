"""Tests for sandbox settings — tier semantics and frozen pydantic config.

Ticket 02 (docs/design/sandbox-integration/tickets.md): ``SandboxSettings`` /
``GuardSettings`` / ``SandboxPolicy`` / ``SandboxBackend`` with the DEFAULT
dormant tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.settings import (
    GuardSettings,
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)


class TestSandboxBackend:
    """SandboxBackend tiers and string values (PRD §配置模型)."""

    def test_members(self) -> None:
        assert {b.value for b in SandboxBackend} == {
            "default",
            "auto",
            "local",
            "oci",
            "host",
        }

    def test_from_string_value(self) -> None:
        assert SandboxBackend("auto") is SandboxBackend.AUTO


class TestSandboxPolicy:
    """SandboxPolicy values match the PRD contract."""

    def test_members(self) -> None:
        assert {p.value for p in SandboxPolicy} == {
            "read-only",
            "workspace-write",
            "danger-full-access",
        }

    def test_from_string_value(self) -> None:
        assert SandboxPolicy("workspace-write") is SandboxPolicy.WORKSPACE_WRITE


class TestDefaults:
    """Default settings equal the dormant DEFAULT tier (PRD: 完全休眠)."""

    def test_default_backend_is_default(self) -> None:
        assert SandboxSettings().backend is SandboxBackend.DEFAULT

    def test_default_policy_is_danger_full_access(self) -> None:
        assert SandboxSettings().policy is SandboxPolicy.DANGER_FULL_ACCESS

    def test_default_network_is_false(self) -> None:
        assert SandboxSettings().network is False

    def test_default_writable_roots_empty(self) -> None:
        assert SandboxSettings().writable_roots == []

    def test_default_protected_subpaths_is_git(self) -> None:
        assert SandboxSettings().protected_subpaths == [".git"]

    def test_default_image_is_none(self) -> None:
        assert SandboxSettings().image is None

    def test_default_guard_enabled(self) -> None:
        settings = SandboxSettings()
        assert settings.guard.enabled is True


class TestExplicitValues:
    """Explicit tier values parse from raw data."""

    def test_backend_from_raw_string(self) -> None:
        settings = SandboxSettings.model_validate({"backend": "oci"})
        assert settings.backend is SandboxBackend.OCI

    def test_policy_from_raw_string(self) -> None:
        settings = SandboxSettings.model_validate({"policy": "read-only"})
        assert settings.policy is SandboxPolicy.READ_ONLY

    def test_writable_roots_coerced_to_path(self) -> None:
        settings = SandboxSettings.model_validate({"writable_roots": ["/tmp/x"]})
        assert settings.writable_roots == [Path("/tmp/x")]

    def test_nested_guard_from_raw(self) -> None:
        settings = SandboxSettings.model_validate({"guard": {"enabled": False}})
        assert settings.guard.enabled is False

    def test_full_roundtrip(self) -> None:
        settings = SandboxSettings.model_validate(
            {
                "backend": "local",
                "policy": "workspace-write",
                "network": False,
                "writable_roots": ["/tmp/cache"],
                "protected_subpaths": [".git", ".env"],
                "image": "modex-sandbox:latest",
                "guard": {"enabled": True},
            }
        )
        assert settings.backend is SandboxBackend.LOCAL
        assert settings.policy is SandboxPolicy.WORKSPACE_WRITE
        assert settings.writable_roots == [Path("/tmp/cache")]
        assert settings.protected_subpaths == [".git", ".env"]
        assert settings.image == "modex-sandbox:latest"


class TestInvalidValues:
    """extra=forbid and enum validation reject unknown fields and values."""

    def test_unknown_backend_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SandboxSettings.model_validate({"backend": "docker"})

    def test_unknown_policy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SandboxSettings.model_validate({"policy": "sandbox"})

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SandboxSettings.model_validate({"timeout": 30})

    def test_unknown_guard_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GuardSettings.model_validate({"unknown": True})

    def test_command_pattern_field_removed(self) -> None:
        # The command_pattern toggle could disable the built-in
        # destructive/fork/system deny rules — a mandatory-security
        # input. No supported config disables hard denials, so the
        # field must not exist.
        with pytest.raises(ValidationError):
            GuardSettings.model_validate({"command_pattern": False})


class TestFrozen:
    """frozen=True: settings are immutable after construction."""

    def test_settings_reject_mutation(self) -> None:
        settings = SandboxSettings()
        with pytest.raises(ValidationError):
            settings.backend = SandboxBackend.HOST  # type: ignore[misc]

    def test_nested_guard_reject_mutation(self) -> None:
        settings = SandboxSettings()
        with pytest.raises(ValidationError):
            settings.guard.enabled = False  # type: ignore[misc]
