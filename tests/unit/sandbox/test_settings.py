"""Tests for sandbox settings — the two-class model and frozen pydantic config.

Ticket 02 (docs/design/sandbox-integration/tickets.md) + the unified
two-class permission redesign: ``SandboxSettings`` carries the parallel
(read-only) and exclusive (read-write) class faces; ``WriteSurface``
replaces the retired policy enum (READ_ONLY→none, WORKSPACE_WRITE→
workspace, DANGER_FULL_ACCESS→full, plus the new roots-only tier).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    GuardSettings,
    ParallelConfig,
    SandboxBackend,
    SandboxSettings,
    ToolPaths,
    WriteSurface,
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


class TestWriteSurface:
    """WriteSurface values match the two-class contract."""

    def test_members(self) -> None:
        assert {p.value for p in WriteSurface} == {
            "none",
            "workspace",
            "roots",
            "full",
        }

    def test_from_string_value(self) -> None:
        assert WriteSurface("workspace") is WriteSurface.WORKSPACE
        assert WriteSurface("roots") is WriteSurface.ROOTS


class TestDefaults:
    """Defaults: dormant backend, workspace write surface, unrestricted reads."""

    def test_default_backend_is_default(self) -> None:
        assert SandboxSettings().backend is SandboxBackend.DEFAULT

    def test_default_write_surface_is_workspace(self) -> None:
        assert SandboxSettings().exclusive.write_surface is WriteSurface.WORKSPACE

    def test_default_network_is_false(self) -> None:
        assert SandboxSettings().network is False

    def test_default_writable_roots_empty(self) -> None:
        assert SandboxSettings().exclusive.writable_roots == []

    def test_default_protected_subpaths_is_git(self) -> None:
        assert SandboxSettings().exclusive.protected_subpaths == [".git"]

    def test_default_image_is_none(self) -> None:
        assert SandboxSettings().image is None

    def test_default_guard_enabled(self) -> None:
        settings = SandboxSettings()
        assert settings.guard.enabled is True

    def test_default_parallel_boundaries_empty(self) -> None:
        # The parallel (read-only) class is unrestricted by default.
        assert SandboxSettings().parallel.boundaries == {}

    def test_default_exclusive_boundaries_empty(self) -> None:
        assert SandboxSettings().exclusive.boundaries == {}


class TestExplicitValues:
    """Explicit values parse from raw data."""

    def test_backend_from_raw_string(self) -> None:
        settings = SandboxSettings.model_validate({"backend": "oci"})
        assert settings.backend is SandboxBackend.OCI

    def test_write_surface_from_raw_string(self) -> None:
        settings = SandboxSettings.model_validate(
            {"exclusive": {"write_surface": "none"}}
        )
        assert settings.exclusive.write_surface is WriteSurface.NONE

    def test_writable_roots_coerced_to_path(self) -> None:
        settings = SandboxSettings.model_validate(
            {"exclusive": {"writable_roots": ["/tmp/x"]}}
        )
        assert settings.exclusive.writable_roots == [Path("/tmp/x")]

    def test_nested_guard_from_raw(self) -> None:
        settings = SandboxSettings.model_validate({"guard": {"enabled": False}})
        assert settings.guard.enabled is False

    def test_parallel_boundary_from_raw(self) -> None:
        settings = SandboxSettings.model_validate(
            {"parallel": {"boundaries": {"grep": {"paths": ["./src"]}}}}
        )
        assert settings.parallel.boundaries["grep"].paths == (Path("./src"),)

    def test_full_roundtrip(self) -> None:
        settings = SandboxSettings.model_validate(
            {
                "backend": "local",
                "exclusive": {
                    "write_surface": "roots",
                    "writable_roots": ["/tmp/cache"],
                    "protected_subpaths": [".git", ".env"],
                },
                "network": False,
                "image": "modex-sandbox:latest",
                "guard": {"enabled": True},
            }
        )
        assert settings.backend is SandboxBackend.LOCAL
        assert settings.exclusive.write_surface is WriteSurface.ROOTS
        assert settings.exclusive.writable_roots == [Path("/tmp/cache")]
        assert settings.exclusive.protected_subpaths == [".git", ".env"]
        assert settings.image == "modex-sandbox:latest"


class TestInvalidValues:
    """extra=forbid and enum validation reject unknown fields and values."""

    def test_unknown_backend_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SandboxSettings.model_validate({"backend": "docker"})

    def test_unknown_write_surface_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SandboxSettings.model_validate(
                {"exclusive": {"write_surface": "sandbox"}}
            )

    def test_retired_policy_field_rejected(self) -> None:
        # The flat policy/writable_roots face is retired — the two-class
        # model is the only configuration vocabulary.
        with pytest.raises(ValidationError):
            SandboxSettings.model_validate({"policy": "workspace-write"})
        with pytest.raises(ValidationError):
            SandboxSettings.model_validate({"writable_roots": ["/tmp/x"]})

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

    def test_tool_paths_frozen(self) -> None:
        boundary = ToolPaths(paths=(Path("./src"),))
        with pytest.raises(ValidationError):
            boundary.paths = ()  # type: ignore[misc]

    def test_parallel_config_defaults(self) -> None:
        assert ParallelConfig().boundaries == {}

    def test_exclusive_config_defaults(self) -> None:
        config = ExclusiveConfig()
        assert config.write_surface is WriteSurface.WORKSPACE
        assert config.writable_roots == []
