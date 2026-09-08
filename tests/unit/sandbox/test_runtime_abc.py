"""Tests for the SandboxRuntime ABC, HostRuntime, and typed selection.

Ticket 03 (docs/design/sandbox-integration/tickets.md), converged on the
typed selection layer:

- ``SandboxRuntime.resolve()`` → ``ResolvedSandbox`` (enforcement / shell argv
  / mount table)
- selection matrix (in ``selection.resolve_selection``): AUTO × 3
  platforms, LOCAL, OCI, HOST — covered exhaustively in
  ``test_selection.py``; this module keeps the value/ABC/snapshot anchors
- DEFAULT never reaches selection (it is "unconfigured", not a runtime tier)
- enforcement snapshot written to ``runtime.state`` via TurnCustomKey
- HostRuntime carries the degradation reason from the selector
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.sandbox.runtime import (
    HostRuntime,
    ResolvedSandbox,
    SandboxRuntime,
    write_enforcement_snapshot,
)
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings
from modex_agent.sandbox.types import EnforcementLevel

# ---------------------------------------------------------------------------
# ResolvedSandbox value model
# ---------------------------------------------------------------------------


class TestResolvedSandbox:
    def test_rejects_default_backend(self) -> None:
        """DEFAULT is not a runtime tier — it must never appear as resolved."""
        with pytest.raises(ValidationError):
            ResolvedSandbox(
                backend=SandboxBackend.DEFAULT,
                enforcement=EnforcementLevel.NONE,
                shell_argv=[],
                one_shot_command_argv_prefix=[],
            )

    def test_accepts_host(self) -> None:
        resolved = ResolvedSandbox(
            backend=SandboxBackend.HOST,
            enforcement=EnforcementLevel.NONE,
            shell_argv=["/bin/bash", "-i"],
            one_shot_command_argv_prefix=[],
        )
        assert resolved.backend is SandboxBackend.HOST
        assert resolved.degraded_reason is None
        assert resolved.mount_table is None

    def test_frozen(self) -> None:
        resolved = ResolvedSandbox(
            backend=SandboxBackend.HOST,
            enforcement=EnforcementLevel.NONE,
            shell_argv=[],
            one_shot_command_argv_prefix=[],
        )
        with pytest.raises(ValidationError):
            resolved.backend = SandboxBackend.OCI  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DEFAULT gate — the assembly layer keeps the sandbox dormant instead
# ---------------------------------------------------------------------------


async def test_default_rejected_by_selection() -> None:
    """DEFAULT must never reach selection — program error if it leaks through."""
    from modex_agent.sandbox.selection import resolve_selection

    with pytest.raises(ValueError, match="DEFAULT"):
        await resolve_selection(SandboxBackend.DEFAULT)


# ---------------------------------------------------------------------------
# SandboxRuntime ABC + HostRuntime
# ---------------------------------------------------------------------------


class TestSandboxRuntimeAbc:
    def test_abc_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            SandboxRuntime()  # type: ignore[abstract]

    def test_hostruntime_is_subclass(self) -> None:
        assert issubclass(HostRuntime, SandboxRuntime)


class TestHostRuntime:
    async def test_resolve_reports_host_none_enforcement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime = HostRuntime()
        resolved = await runtime.resolve(SandboxSettings(backend=SandboxBackend.HOST), Path("/ws"))
        assert resolved.backend is SandboxBackend.HOST
        assert resolved.enforcement is EnforcementLevel.NONE
        assert resolved.degraded_reason is None
        assert resolved.mount_table is None

    async def test_resolve_shell_argv_is_host_bash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "modex_agent.sandbox.runtime._resolve_host_shell",
            lambda: "/usr/bin/bash",
        )
        runtime = HostRuntime()
        resolved = await runtime.resolve(SandboxSettings(backend=SandboxBackend.HOST), Path("/ws"))
        assert resolved.shell_argv == ["/usr/bin/bash", "--noprofile", "--norc", "-i"]

    async def test_shell_argv_empty_when_no_bash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "modex_agent.sandbox.runtime._resolve_host_shell",
            lambda: None,
        )
        runtime = HostRuntime()
        resolved = await runtime.resolve(SandboxSettings(backend=SandboxBackend.HOST), Path("/ws"))
        assert resolved.shell_argv == []

    async def test_one_shot_prefix_empty_on_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime = HostRuntime()
        resolved = await runtime.resolve(SandboxSettings(backend=SandboxBackend.HOST), Path("/ws"))
        assert resolved.one_shot_command_argv_prefix == []

    async def test_degraded_reason_lands_in_resolved_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HostRuntime constructed by the selector with a degradation reason
        reports it verbatim — the honest-reporting obligation."""
        monkeypatch.setattr(
            "modex_agent.sandbox.runtime._resolve_host_shell",
            lambda: "/usr/bin/bash",
        )
        runtime = HostRuntime(degraded_reason="bwrap unavailable: not found on PATH")
        resolved = await runtime.resolve(
            SandboxSettings(backend=SandboxBackend.AUTO), Path("/ws")
        )
        assert resolved.degraded_reason == "bwrap unavailable: not found on PATH"
        assert resolved.enforcement is EnforcementLevel.NONE


# ---------------------------------------------------------------------------
# Enforcement snapshot
# ---------------------------------------------------------------------------


class TestEnforcementSnapshot:
    """Snapshot writes go through the real TurnStateBase.custom seam."""

    def _react_custom(self) -> dict[str, Any]:
        """Real turn-state custom dict — the actual write seam (test rule 2)."""
        state = ReActTurnState()
        return state.custom

    def test_write_snapshot_into_custom(self) -> None:
        state_custom = self._react_custom()
        resolved = ResolvedSandbox(
            backend=SandboxBackend.LOCAL,
            enforcement=EnforcementLevel.FULL,
            shell_argv=["bwrap"],
            one_shot_command_argv_prefix=["bwrap"],
        )
        write_enforcement_snapshot(state_custom, resolved)
        assert TurnCustomKey.SANDBOX_ENFORCEMENT in state_custom

    def test_snapshot_fields(self) -> None:
        state_custom = self._react_custom()
        resolved = ResolvedSandbox(
            backend=SandboxBackend.LOCAL,
            enforcement=EnforcementLevel.PARTIAL,
            shell_argv=["bwrap"],
            one_shot_command_argv_prefix=["bwrap"],
            degraded_reason="bwrap network namespace unavailable",
        )
        write_enforcement_snapshot(state_custom, resolved)
        snapshot = state_custom[TurnCustomKey.SANDBOX_ENFORCEMENT]
        data = snapshot.model_dump()
        assert data == {
            "backend": "local",
            "enforcement": "partial",
            "degraded_reason": "bwrap network namespace unavailable",
        }

    def test_overwrite_latest_wins(self) -> None:
        state_custom = self._react_custom()
        first = ResolvedSandbox(
            backend=SandboxBackend.HOST,
            enforcement=EnforcementLevel.NONE,
            shell_argv=[],
            one_shot_command_argv_prefix=[],
        )
        second = ResolvedSandbox(
            backend=SandboxBackend.OCI,
            enforcement=EnforcementLevel.FULL,
            shell_argv=["docker"],
            one_shot_command_argv_prefix=["docker"],
        )
        write_enforcement_snapshot(state_custom, first)
        write_enforcement_snapshot(state_custom, second)
        snapshot = state_custom[TurnCustomKey.SANDBOX_ENFORCEMENT]
        assert snapshot.model_dump()["backend"] == "oci"


def test_sandbox_enforcement_key_value() -> None:
    assert TurnCustomKey.SANDBOX_ENFORCEMENT.value == "_sandbox_enforcement"
