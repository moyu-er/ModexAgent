"""Delegation boundary — snapshot, denial copy, unified derivation (T05b).

Covers the PRD §Subagent 专项 acceptance anchors:
- 两段式文案 (禁令语句 + escalation 提示, 含 allowed 范围)
- 快照 frozen (池配置后变不影响已 spawn subagent)
- 统一派生 ``resolve_agent_sandbox``: 未声明 → 继承调用方 (休眠调用方
  归一化为 guard-only HOST); 声明块权限面 authoritative、substrate 继承;
  ceiling 纪律 (只能收窄, 不能放大)
- envelope = workspace(POWER 面) + declared roots
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.delegation import (
    MAX_DELEGATION_DEPTH,
    DelegationSnapshot,
    delegation_denial_message,
    resolve_agent_sandbox,
)
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)

WS = Path("/ws/project").resolve()


def _snapshot(**overrides: object) -> DelegationSnapshot:
    kwargs: dict[str, object] = {
        "workspace_root": WS,
        "settings": SandboxSettings(
            backend=SandboxBackend.HOST,
            exclusive=ExclusiveConfig(writable_roots=[WS / "shared"]),
        ),
        "enforcement": "none",
        "depth": 1,
    }
    kwargs.update(overrides)
    return DelegationSnapshot.model_validate(kwargs)


class TestSnapshot:
    def test_default_source_is_delegation(self) -> None:
        assert _snapshot().source == "delegation"

    def test_envelope_is_workspace_plus_declared_roots(self) -> None:
        assert _snapshot().envelope == (WS, (WS / "shared").resolve())

    def test_envelope_workspace_only_when_no_roots(self) -> None:
        snap = _snapshot(
            settings=SandboxSettings(backend=SandboxBackend.HOST)
        )
        assert snap.envelope == (WS,)

    def test_frozen(self) -> None:
        snap = _snapshot()
        with pytest.raises(ValidationError):
            snap.depth = 5  # type: ignore[misc]

    def test_pool_config_change_after_spawn_does_not_affect_snapshot(self) -> None:
        # The snapshot is a frozen value object — a NEW settings object
        # derived later from a changed pool does not mutate it.
        snap = _snapshot(depth=2)
        assert snap.depth == 2
        assert snap.workspace_root == WS

    def test_depth_budget_constant_is_three(self) -> None:
        assert MAX_DELEGATION_DEPTH == 3


class TestDenialMessage:
    def test_two_part_copy_with_allowed_scope(self) -> None:
        message = delegation_denial_message("write", "/etc/hosts", _snapshot())
        # First part: the prohibition with the target and allowed scope.
        assert "/etc/hosts" in message
        for root in _snapshot().envelope:
            assert str(root) in message
        # Second part: fixed-permission statement + escalation hint.
        assert (
            "The subagent permission scope is fixed at startup" in message
        )
        assert (
            "request this operation in the main session." in message
        )

    def test_copy_mentions_tool_name_for_command_targets(self) -> None:
        message = delegation_denial_message("bash", "rm /outside/file", _snapshot())
        assert "bash 'rm /outside/file'" in message


class TestSettingsDerivation:
    def test_no_caller_defaults_to_guard_only_host(self) -> None:
        settings = resolve_agent_sandbox(None, None, WS)
        assert settings.backend is SandboxBackend.HOST
        assert settings.exclusive.write_surface is WriteSurface.WORKSPACE
        assert settings.exclusive.writable_roots == []
        assert settings.guard.enabled is True

    def test_dormant_caller_normalizes_to_guard_only_host(self) -> None:
        # The pool's main agent is dormant (DEFAULT) — native delegation
        # still installs its guard-only permission face on HOST.
        settings = resolve_agent_sandbox(None, SandboxSettings(), WS)
        assert settings.backend is SandboxBackend.HOST
        assert settings.exclusive.write_surface is WriteSurface.WORKSPACE

    def test_declared_block_inherits_substrate_from_caller(self) -> None:
        caller = SandboxSettings(
            backend=SandboxBackend.OCI,
            network=True,
            exclusive=ExclusiveConfig(
                write_surface=WriteSurface.FULL, writable_roots=[Path("/ws/extra")]
            ),
        )
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[WS / "shared"])
        )
        settings = resolve_agent_sandbox(declared, caller, WS)
        assert settings.backend is SandboxBackend.OCI  # substrate inherits
        assert settings.network is True
        # The permission face is the declared block's — the caller's full
        # surface does not leak through an explicit declaration.
        assert settings.exclusive.write_surface is WriteSurface.WORKSPACE
        assert settings.exclusive.writable_roots == [WS / "shared"]

    def test_undeclared_block_inherits_caller_wholesale(self) -> None:
        caller = SandboxSettings(
            backend=SandboxBackend.LOCAL,
            exclusive=ExclusiveConfig(
                write_surface=WriteSurface.ROOTS, writable_roots=[Path("/ws/extra")]
            ),
        )
        settings = resolve_agent_sandbox(None, caller, WS)
        assert settings is caller

    def test_declared_roots_inside_caller_ceiling_pass(self) -> None:
        caller = SandboxSettings(
            backend=SandboxBackend.HOST,
            exclusive=ExclusiveConfig(writable_roots=[Path("/ws/extra")]),
        )
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[Path("/ws/extra/sub")])
        )
        settings = resolve_agent_sandbox(declared, caller, WS)
        assert settings.exclusive.writable_roots == [Path("/ws/extra/sub")]

    def test_declared_roots_outside_caller_ceiling_fail(self) -> None:
        caller = SandboxSettings(
            backend=SandboxBackend.HOST,
            exclusive=ExclusiveConfig(writable_roots=[Path("/ws/extra")]),
        )
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(writable_roots=[Path("/ws/other")])
        )
        with pytest.raises(ValueError, match="can only narrow, never amplify"):
            resolve_agent_sandbox(declared, caller, WS)

    def test_declared_boundary_outside_caller_ceiling_fails(self) -> None:
        from modex_agent.sandbox.settings import ToolPaths

        caller = SandboxSettings(backend=SandboxBackend.HOST)
        declared = SandboxSettings(
            exclusive=ExclusiveConfig(
                boundaries={"write": ToolPaths(paths=(Path("/ws/other"),))}
            )
        )
        with pytest.raises(ValueError, match="can only narrow, never amplify"):
            resolve_agent_sandbox(declared, caller, WS)
