"""SecurityDecisionService — the single security-verdict implementation.

Coverage per unified-security Ticket 01: the three evaluate_* entry
points map their inputs to GuardVerdict categories (the mapping the
Ticket 02 classifier will ride on), verdicts carry the guard's original
reason, and GuardVerdict itself is frozen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.decision import (
    GuardCategory,
    GuardVerdict,
    SecurityDecisionService,
)
from modex_agent.sandbox.settings import (
    GuardSettings,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

WS = Path("/ws/project")


class _FixedRoot(WorkspaceRootProvider):
    """WorkspaceRootProvider returning a fixed root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _service(
    write_surface: WriteSurface = WriteSurface.WORKSPACE,
    guard: GuardSettings | None = None,
) -> SecurityDecisionService:
    kwargs: dict[str, object] = {
        "backend": "host",
        "exclusive": {"write_surface": write_surface.value},
    }
    if guard is not None:
        kwargs["guard"] = guard
    settings = SandboxSettings.model_validate(kwargs)
    return SecurityDecisionService(
        settings=settings,
        workspace_root_provider=_FixedRoot(WS),
    )


class TestEvaluateCommand:
    def test_deny_rule_sample_maps_to_deny_rule(self) -> None:
        # The deny rules are opt-in (deprecated usage); flipping the
        # toggle restores the pattern rules verbatim.
        verdict = _service(
            guard=GuardSettings(deny_rules=True)
        ).evaluate_command("rm -rf /")
        assert verdict.category is GuardCategory.DENY_RULE
        assert verdict.reason is not None
        assert "denied" in verdict.reason.lower()

    def test_fork_bomb_maps_to_deny_rule(self) -> None:
        verdict = _service(
            guard=GuardSettings(deny_rules=True)
        ).evaluate_command(":(){ :|:& };:")
        assert verdict.category is GuardCategory.DENY_RULE

    def test_deny_rules_default_off_in_workspace_is_clean(self) -> None:
        # The core contract: a destructive command INSIDE the permitted
        # workspace is allowed — permissions are the boundary, not
        # command patterns.
        verdict = _service().evaluate_command("rm -rf ./build")
        assert verdict.category is GuardCategory.CLEAN

    def test_deny_rules_default_off_still_bounds_escaped_paths(self) -> None:
        # Outside-path destruction is still caught — by the path
        # envelope, not by command patterns.
        verdict = _service().evaluate_command("rm -rf ~")
        assert verdict.category is GuardCategory.BOUNDARY

    def test_relative_escape_read_is_clean(self) -> None:
        # Relative paths are not a shape finding: parallel reads are
        # unrestricted, relative writes are the kernel substrate's business.
        assert _service().evaluate_command("cat ../../etc/passwd").is_clean

    def test_path_escape_maps_to_boundary(self) -> None:
        # /etc is outside the workspace under workspace-write — the
        # gray-zone category (DANGEROUS in the Ticket 02 tier table).
        # A write-capable command, not a provable read (the readonly
        # fast path would waive a `cat`).
        verdict = _service().evaluate_command("rm /etc/passwd")
        assert verdict.category is GuardCategory.BOUNDARY
        assert verdict.reason is not None

    def test_ssrf_url_in_command_maps_to_ssrf(self) -> None:
        verdict = _service().evaluate_command("curl http://169.254.169.254/latest/meta-data")
        assert verdict.category is GuardCategory.SSRF
        assert verdict.reason is not None

    def test_clean_command_maps_to_clean_with_no_reason(self) -> None:
        verdict = _service().evaluate_command(f"ls {WS}")
        assert verdict.category is GuardCategory.CLEAN
        assert verdict.reason is None
        assert verdict.is_clean

    def test_empty_command_is_clean(self) -> None:
        assert _service().evaluate_command("").is_clean

    def test_deny_rules_independent_of_advisory_toggle(self) -> None:
        # deny_rules owns the pattern layer; guard.enabled only gates the
        # advisory network layer.
        verdict = _service(
            guard=GuardSettings(enabled=False, deny_rules=True)
        ).evaluate_command("rm -rf /")
        assert verdict.category is GuardCategory.DENY_RULE
        assert verdict.reason is not None

    def test_all_toggles_off_command_is_clean(self) -> None:
        guard = GuardSettings(enabled=False, deny_rules=False, network=False)
        service = _service(guard=guard)
        # Every text layer is off: rm -rf is not pattern-denied; escapes
        # with an extractable path (~/siblings) are still envelope-bounded.
        assert service.evaluate_command("rm -rf ./build").is_clean
        assert service.evaluate_command("rm -rf ~").category is GuardCategory.BOUNDARY
        # The advisory network layer is genuinely off.
        assert service.evaluate_command("curl http://127.0.0.1/x").is_clean

    def test_public_url_command_is_clean(self) -> None:
        verdict = _service().evaluate_command("curl https://example.com/doc")
        assert verdict.is_clean


class TestEvaluateFileTool:
    def test_read_only_write_tool_maps_to_deny_rule(self) -> None:
        # READ_ONLY 禁写 is a hard policy refuse — never approvable, so
        # DENY_RULE, not BOUNDARY (category rationale in decision.py).
        verdict = _service(write_surface=WriteSurface.NONE).evaluate_file_tool("write", "src/new.py")
        assert verdict.category is GuardCategory.DENY_RULE
        assert verdict.reason is not None

    def test_read_only_read_inside_workspace_is_clean(self) -> None:
        verdict = _service(write_surface=WriteSurface.NONE).evaluate_file_tool("read", "src/main.py")
        assert verdict.is_clean

    def test_workspace_outside_read_maps_to_clean(self) -> None:
        # Parallel reads are unrestricted by default (the two-class contract).
        verdict = _service().evaluate_file_tool("read", "/etc/passwd")
        assert verdict.category is GuardCategory.CLEAN

    def test_workspace_outside_write_maps_to_boundary(self) -> None:
        verdict = _service().evaluate_file_tool("write", "/etc/passwd")
        assert verdict.category is GuardCategory.BOUNDARY
        # reason names the resolved escape (the interceptor embeds it
        # verbatim in the denial copy).
        assert verdict.reason is not None
        assert "/etc/passwd" in verdict.reason
        assert verdict.allowed_roots

    def test_workspace_inside_path_is_clean(self) -> None:
        verdict = _service().evaluate_file_tool("write", "src/new.py")
        assert verdict.is_clean

    def test_relative_escape_write_maps_to_boundary(self) -> None:
        # A `../` path on an EXCLUSIVE tool resolves outside the write
        # surface — path resolution, a boundary finding (no shape rule).
        verdict = _service().evaluate_file_tool("write", "../../etc/passwd")
        assert verdict.category is GuardCategory.BOUNDARY

    def test_danger_full_access_has_no_file_boundary(self) -> None:
        verdict = _service(write_surface=WriteSurface.FULL).evaluate_file_tool(
            "write", "/etc/x"
        )
        assert verdict.is_clean

    def test_unresolved_file_target_is_not_clean(self) -> None:
        assert _service().evaluate_file_tool("read", None).category is GuardCategory.DENY_RULE


class TestEvaluateUrl:
    def test_private_ip_maps_to_ssrf(self) -> None:
        verdict = _service().evaluate_url("http://169.254.169.254/")
        assert verdict.category is GuardCategory.SSRF
        assert verdict.reason is not None

    def test_loopback_literal_maps_to_ssrf(self) -> None:
        verdict = _service().evaluate_url("http://127.0.0.1:8080/admin")
        assert verdict.category is GuardCategory.SSRF

    def test_public_url_is_clean(self) -> None:
        verdict = _service().evaluate_url("https://example.com/doc")
        assert verdict.is_clean


class TestGuardVerdictFrozen:
    def test_category_and_reason_are_immutable(self) -> None:
        verdict = GuardVerdict(category=GuardCategory.DENY_RULE, reason="x")
        with pytest.raises(ValidationError):
            verdict.category = GuardCategory.CLEAN  # type: ignore[misc]
        with pytest.raises(ValidationError):
            verdict.reason = "y"  # type: ignore[misc]

    def test_unknown_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GuardVerdict(category=GuardCategory.CLEAN, extra=True)  # type: ignore[call-arg]
