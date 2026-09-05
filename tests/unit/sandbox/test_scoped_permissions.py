"""Permission regressions through the shared tool-call decision seam."""

from pathlib import Path

import pytest

from modex_agent.sandbox.decision import GuardCategory, SecurityDecisionService
from modex_agent.sandbox.delegation import DelegationSnapshot, delegation_sandbox_settings
from modex_agent.sandbox.settings import (
    GuardSettings,
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)
from modex_agent.sandbox.tool_matrix import (
    approval_anchor,
    describe_tool_security,
    extract_call_target,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


class FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self.root = root

    def current(self) -> Path:
        return self.root


def test_snapshot_canonicalizes_relative_roots(tmp_path: Path) -> None:
    snapshot = DelegationSnapshot(workspace_root=tmp_path / "ws", allowed_dirs=(Path("../shared"),))
    assert snapshot.envelope == (tmp_path / "ws", tmp_path / "shared")
    assert snapshot.enforcement is None


def test_readonly_parent_is_not_elevated() -> None:
    settings = delegation_sandbox_settings((), pool=SandboxSettings(
        backend=SandboxBackend.HOST, policy=SandboxPolicy.READ_ONLY,
    ))
    assert settings.policy is SandboxPolicy.READ_ONLY


@pytest.mark.parametrize("guard", [GuardSettings(enabled=False), GuardSettings(network=False)])
@pytest.mark.parametrize("tool,argument", [("bash", "command"), ("bash_input", "line"), ("process", "data")])
def test_path_boundary_is_mandatory(tmp_path: Path, guard: GuardSettings, tool: str, argument: str) -> None:
    service = SecurityDecisionService(SandboxSettings(
        backend=SandboxBackend.HOST, policy=SandboxPolicy.WORKSPACE_WRITE, guard=guard,
    ), FixedRoot(tmp_path / "ws"))
    verdict = service.evaluate_tool_call(tool, {argument: f'cat "{tmp_path / "outside"}"'})
    assert verdict.category is GuardCategory.BOUNDARY
    assert service.evaluate_tool_call(tool, {argument: "echo hello"}).is_clean


def test_ssrf_has_priority_over_path_boundary(tmp_path: Path) -> None:
    service = SecurityDecisionService(SandboxSettings(
        backend=SandboxBackend.HOST, policy=SandboxPolicy.WORKSPACE_WRITE,
    ), FixedRoot(tmp_path / "ws"))
    verdict = service.evaluate_tool_call("bash", {"command": f'curl http://127.0.0.1 -o "{tmp_path / "out"}"'})
    assert verdict.category is GuardCategory.SSRF


def test_explicit_shell_working_directory_is_checked(tmp_path: Path) -> None:
    service = SecurityDecisionService(SandboxSettings(
        backend=SandboxBackend.HOST, policy=SandboxPolicy.WORKSPACE_WRITE,
    ), FixedRoot(tmp_path / "ws"))
    verdict = service.evaluate_tool_call("bash", {"command": "echo ok", "working_dir": str(tmp_path)})
    assert verdict.category is GuardCategory.BOUNDARY
    assert verdict.target == str(tmp_path)
    assert verdict.allowed_roots == (tmp_path / "ws",)
    hard = service.evaluate_tool_call("bash", {"command": "curl http://127.0.0.1", "working_dir": str(tmp_path)})
    assert hard.category is GuardCategory.SSRF


def test_shell_approval_anchor_binds_the_checked_working_directory(tmp_path: Path) -> None:
    first = approval_anchor("bash", {"command": "echo ok", "working_dir": "first"}, tmp_path)
    second = approval_anchor("bash", {"command": "echo ok", "working_dir": "second"}, tmp_path)
    assert first != second
    assert first == approval_anchor("bash", {"command": "echo ok", "working_dir": str(tmp_path / "first")}, tmp_path)


def test_relative_extra_root_and_protected_paths(tmp_path: Path) -> None:
    service = SecurityDecisionService(SandboxSettings(
        backend=SandboxBackend.HOST, policy=SandboxPolicy.WORKSPACE_WRITE,
        writable_roots=[Path("../shared")],
    ), FixedRoot(tmp_path / "ws"))
    assert service.evaluate_tool_call("read", {"path": "../shared/a"}).is_clean
    assert service.evaluate_tool_call("write", {"path": "../shared/.git/config"}).category is GuardCategory.DENY_RULE
    assert service.evaluate_tool_call("read", {"path": "../shared/.git/config"}).is_clean


@pytest.mark.parametrize("tool", ["ls", "glob", "grep", "ast_grep_search"])
def test_default_search_path_is_anchored(tool: str, tmp_path: Path) -> None:
    target = extract_call_target(describe_tool_security(tool), {})
    assert target.path == "."
    assert approval_anchor(tool, {}, tmp_path) == str(tmp_path)


def test_unclassified_anchor_does_not_serialize_arbitrary_objects(tmp_path: Path) -> None:
    assert approval_anchor("custom", {"value": object()}, tmp_path) is None


@pytest.mark.parametrize("tool", ["read", "write", "edit", "aci_edit", "ast_grep_replace"])
@pytest.mark.parametrize("arguments", [{}, {"path": None}, {"path": ""}, {"path": 3}])
def test_required_path_never_invents_an_approval_target(tool: str, arguments: dict[str, object], tmp_path: Path) -> None:
    assert extract_call_target(describe_tool_security(tool), arguments).path is None
    assert approval_anchor(tool, arguments, tmp_path) is None
    service = SecurityDecisionService(SandboxSettings(
        backend=SandboxBackend.HOST, policy=SandboxPolicy.WORKSPACE_WRITE,
    ), FixedRoot(tmp_path))
    assert service.evaluate_tool_call(tool, arguments).category is GuardCategory.DENY_RULE


@pytest.mark.parametrize("tool", ["lsp_navigation", "lsp_diagnostics"])
def test_lsp_uses_its_real_file_argument(tool: str, tmp_path: Path) -> None:
    service = SecurityDecisionService(SandboxSettings(
        backend=SandboxBackend.HOST, policy=SandboxPolicy.WORKSPACE_WRITE,
    ), FixedRoot(tmp_path / "ws"))
    assert service.evaluate_tool_call(tool, {"file": str(tmp_path / "outside")}).category is GuardCategory.BOUNDARY
