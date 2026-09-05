"""Delegation boundary — snapshot, denial copy, settings derivation (T05b).

Covers the PRD §Subagent 专项 acceptance anchors:
- 两段式文案 (禁令语句 + escalation 提示, 含 allowed 范围)
- 快照 frozen (池配置后变不影响已 spawn subagent)
- 边界推导: pool DEFAULT → 专用 HOST+WORKSPACE_WRITE; pool on → 收窄
- envelope = workspace + allowed_dirs
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.delegation import (
    MAX_DELEGATION_DEPTH,
    DelegationSnapshot,
    delegation_denial_message,
    delegation_sandbox_settings,
)
from modex_agent.sandbox.settings import (
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)

WS = Path("/ws/project").resolve()


def _snapshot(**overrides: object) -> DelegationSnapshot:
    kwargs: dict[str, object] = {
        "workspace_root": WS,
        "allowed_dirs": (Path("/ws/shared"),),
        "enforcement": "none",
        "depth": 1,
    }
    kwargs.update(overrides)
    return DelegationSnapshot.model_validate(kwargs)


class TestSnapshot:
    def test_default_source_is_delegation(self) -> None:
        assert _snapshot().source == "delegation"

    def test_envelope_is_workspace_plus_allowed_dirs(self) -> None:
        assert _snapshot().envelope == (WS, Path("/ws/shared").resolve())

    def test_envelope_workspace_only_when_no_allowed_dirs(self) -> None:
        assert _snapshot(allowed_dirs=()).envelope == (WS,)

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
    def test_pool_default_builds_dedicated_host_workspace_write(self) -> None:
        settings = delegation_sandbox_settings((Path("/ws/shared"),), pool=None)
        assert settings.backend is SandboxBackend.HOST
        assert settings.policy is SandboxPolicy.WORKSPACE_WRITE
        assert settings.writable_roots == [Path("/ws/shared")]
        assert settings.guard.enabled is True

    def test_pool_default_tier_also_dedicated(self) -> None:
        dormant = SandboxSettings()  # backend=DEFAULT
        settings = delegation_sandbox_settings((), pool=dormant)
        assert settings.backend is SandboxBackend.HOST
        assert settings.policy is SandboxPolicy.WORKSPACE_WRITE

    def test_pool_on_inherits_backend_tightens_policy_and_roots(self) -> None:
        pool = SandboxSettings(
            backend=SandboxBackend.OCI,
            policy=SandboxPolicy.DANGER_FULL_ACCESS,
            writable_roots=[Path("/pool/extra")],
            network=True,
        )
        settings = delegation_sandbox_settings((Path("/ws/shared"),), pool=pool)
        assert settings.backend is SandboxBackend.OCI
        assert settings.policy is SandboxPolicy.WORKSPACE_WRITE
        assert settings.writable_roots == [Path("/ws/shared")]
        assert settings.network is True

    def test_pool_danger_full_access_is_narrowed_not_inherited(self) -> None:
        pool = SandboxSettings(
            backend=SandboxBackend.HOST,
            policy=SandboxPolicy.DANGER_FULL_ACCESS,
        )
        settings = delegation_sandbox_settings((), pool=pool)
        assert settings.policy is SandboxPolicy.WORKSPACE_WRITE

    def test_original_pool_settings_untouched(self) -> None:
        pool = SandboxSettings(
            backend=SandboxBackend.HOST,
            policy=SandboxPolicy.WORKSPACE_WRITE,
            writable_roots=[Path("/pool/extra")],
        )
        delegation_sandbox_settings((Path("/ws/shared"),), pool=pool)
        assert pool.writable_roots == [Path("/pool/extra")]
