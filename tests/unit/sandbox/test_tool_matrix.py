"""Typed tool-effect seam — ToolEffect catalog, extraction, judgment wiring.

Coverage for the descriptor seam replacing the copied EXECUTION/READ/
WRITE/WEB name sets (one deep module owns the vocabulary; decision,
classifier, interceptor/presentation, and the approval anchor all
consume it):

- every default-registered built-in carries a conscious effect (roster
  ratchet: a newly registered tool fails here until it is classified)
- ``ast_grep_replace`` is WRITE — outside-envelope calls get BOUNDARY and
  read-only refusals exactly like ``write``/``edit``
- ``bash_input`` / ``process`` are EXECUTION_INPUT (the vocabulary for
  the later fail-closed shell wave; judgment today stays pass-clean)
- unknown / MCP names get the NONE default — no path claim, existing
  approval and tool policy still apply unchanged
- extraction is declarative (the descriptor names its target argument,
  so a schema using ``file_path`` declares that) and returns a typed
  result
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.sandbox.decision import GuardCategory, SecurityDecisionService
from modex_agent.sandbox.guard_presentation import verdict_to_denial
from modex_agent.sandbox.settings import SandboxSettings, WriteSurface
from modex_agent.sandbox.tool_matrix import (
    ToolCallTarget,
    ToolEffect,
    ToolSecurityDescriptor,
    approval_anchor,
    describe_tool_security,
    extract_call_target,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

WS = Path("/ws/project")

# The expected effect for every name a default assembly can register:
# the standard builder roster plus the runtime-registered names from
# ``register_default_tools`` and the bundled capability packages.
_ROSTER_EFFECTS: dict[str, ToolEffect] = {
    "read": ToolEffect.READ,
    "ls": ToolEffect.READ,
    "glob": ToolEffect.READ,
    "grep": ToolEffect.READ,
    "ast_grep_search": ToolEffect.READ,
    "write": ToolEffect.WRITE,
    "edit": ToolEffect.WRITE,
    "aci_edit": ToolEffect.WRITE,
    "ast_grep_replace": ToolEffect.WRITE,
    "web_reader": ToolEffect.WEB,
    "web_search": ToolEffect.NONE,
    "bash": ToolEffect.EXECUTE,
    "bash_input": ToolEffect.EXECUTION_INPUT,
    "process": ToolEffect.EXECUTION_INPUT,
    "terminal": ToolEffect.NONE,
    "todo_write": ToolEffect.NONE,
    "todo_read": ToolEffect.NONE,
    "experience": ToolEffect.NONE,
    "task": ToolEffect.NONE,
    "send_to_agent": ToolEffect.NONE,
    "send_to_peer": ToolEffect.NONE,
}


class _FixedRoot(WorkspaceRootProvider):
    """WorkspaceRootProvider returning a fixed root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _service(policy: WriteSurface = WriteSurface.WORKSPACE) -> SecurityDecisionService:
    settings = SandboxSettings.model_validate(
        {"backend": "host", "exclusive": {"write_surface": policy.value}}
    )
    return SecurityDecisionService(settings=settings, workspace_root_provider=_FixedRoot(WS))


class TestCatalogClassification:
    @pytest.mark.parametrize(
        ("name", "effect"),
        sorted(_ROSTER_EFFECTS.items()),
        ids=sorted(_ROSTER_EFFECTS),
    )
    def test_registered_tool_has_expected_effect(self, name: str, effect: ToolEffect) -> None:
        assert describe_tool_security(name).effect is effect

    def test_roster_ratchet_covers_every_default_registered_name(self) -> None:
        from modex_agent.plugins.defaults.tools import _STANDARD_TOOL_BUILDERS

        registered = set(_STANDARD_TOOL_BUILDERS) | {
            # register_default_tools / bundled capabilities (roster opt-in
            # names + the bash companion + capability-owned names).
            "bash",
            "process",
            "terminal",
            "bash_input",
            "todo_write",
            "todo_read",
            "experience",
            "task",
            "send_to_agent",
            "send_to_peer",
        }
        assert set(_ROSTER_EFFECTS) == registered

    def test_unknown_and_mcp_names_default_to_none(self) -> None:
        assert describe_tool_security("mcp_github__create_issue").effect is ToolEffect.NONE
        assert describe_tool_security("").effect is ToolEffect.NONE


class TestAstGrepReplaceIsWrite:
    ARGS = {
        "pattern": "(function_definition) @fd",
        "replacement": "def replacement(): ...",
        "language": "python",
        "path": "/etc/hosts",
    }

    def test_outside_workspace_path_maps_to_boundary(self) -> None:
        verdict = _service().evaluate_tool_call("ast_grep_replace", self.ARGS)
        assert verdict.category is GuardCategory.BOUNDARY
        assert verdict.reason is not None

    def test_denial_copy_matches_write_tool_copy(self) -> None:
        write_verdict = _service().evaluate_tool_call(
            "write", {"path": "/etc/hosts", "content": "x"}
        )
        verdict = _service().evaluate_tool_call("ast_grep_replace", self.ARGS)
        assert verdict.category is write_verdict.category
        assert verdict.reason == write_verdict.reason
        copy = verdict_to_denial(
            verdict, WriteSurface.WORKSPACE, str(WS), "ast_grep_replace", "/etc/hosts"
        )
        write_copy = verdict_to_denial(
            write_verdict, WriteSurface.WORKSPACE, str(WS), "write", "/etc/hosts"
        )
        assert copy is not None and copy == write_copy

    def test_read_only_policy_refuses_like_write(self) -> None:
        verdict = _service(WriteSurface.NONE).evaluate_tool_call(
            "ast_grep_replace", self.ARGS
        )
        assert verdict.category is GuardCategory.DENY_RULE
        denial = verdict_to_denial(
            verdict, WriteSurface.NONE, str(WS), "ast_grep_replace", "/etc/hosts"
        )
        assert denial is not None
        assert "ast_grep_replace" in denial
        assert "write surface: none" in denial

    def test_inside_workspace_path_is_clean(self) -> None:
        args = dict(self.ARGS, path="src/main.py")
        assert _service().evaluate_tool_call("ast_grep_replace", args).is_clean


class TestJudgmentBehavior:
    def test_bash_input_and_process_use_command_guards(self) -> None:
        # EXECUTION_INPUT effects dispatch through the command pipeline —
        # an absolute escape is envelope-bounded (BOUNDARY), proving the
        # dispatch (traversal is deprecated usage and off by default).
        service = _service()
        assert service.evaluate_tool_call("bash_input", {"line": "rm /etc/hosts"}).category is GuardCategory.BOUNDARY
        assert service.evaluate_tool_call("process", {"data": "y"}).is_clean

    def test_unknown_tool_makes_no_path_claim(self) -> None:
        verdict = _service().evaluate_tool_call("mcp_github__create_issue", {"path": "/etc/x"})
        assert verdict.is_clean

    def test_write_effect_via_descriptor_matches_name_dispatch(self) -> None:
        args = {"path": "/etc/hosts", "content": "x"}
        assert _service().evaluate_tool_call("aci_edit", args).category is GuardCategory.BOUNDARY


class TestExtraction:
    def test_path_target_from_declared_argument(self) -> None:
        descriptor = describe_tool_security("ast_grep_replace")
        target = extract_call_target(descriptor, {"path": "src/a.py"})
        assert target == ToolCallTarget(path="src/a.py")

    def test_declared_argument_name_is_data_not_convention(self) -> None:
        descriptor = ToolSecurityDescriptor(effect=ToolEffect.WRITE, target_argument="file_path")
        target = extract_call_target(descriptor, {"file_path": "src/a.py"})
        assert target.path == "src/a.py"

    def test_command_and_url_targets(self) -> None:
        bash = extract_call_target(describe_tool_security("bash"), {"command": "ls"})
        web = extract_call_target(describe_tool_security("web_reader"), {"url": "https://x"})
        assert bash.command == "ls"
        assert web.url == "https://x"

    def test_missing_or_non_string_argument_yields_no_claim(self) -> None:
        descriptor = describe_tool_security("write")
        assert extract_call_target(descriptor, {}).path is None
        assert extract_call_target(descriptor, {"path": ""}).path is None
        assert extract_call_target(descriptor, {"path": 3}).path is None

    def test_unclassified_effect_yields_empty_target(self) -> None:
        target = extract_call_target(describe_tool_security("mcp_x"), {"path": "/etc/x"})
        assert target == ToolCallTarget()

    def test_models_are_frozen_strict(self) -> None:
        descriptor = describe_tool_security("write")
        with pytest.raises(ValidationError):
            descriptor.effect = ToolEffect.READ  # type: ignore[misc]
        with pytest.raises(ValidationError):
            ToolSecurityDescriptor(effect=ToolEffect.WRITE, bogus="x")  # type: ignore[call-arg]
        target = ToolCallTarget(path="a")
        with pytest.raises(ValidationError):
            target.path = "b"  # type: ignore[misc]


class TestAnchorOnTheSeam:
    def test_ast_grep_replace_anchor_canonicalizes_like_write(self) -> None:
        dotted = approval_anchor("ast_grep_replace", {"path": "sub/../notes/a.py"}, WS)
        plain = approval_anchor("write", {"path": "notes/a.py"}, WS)
        assert dotted is not None and dotted == plain

    def test_matrix_tool_anchors_unchanged(self) -> None:
        assert approval_anchor("bash", {"command": "ls /tmp"}, None) == "ls /tmp"
        assert approval_anchor("web_reader", {"url": "https://x.example"}, None) == (
            "https://x.example"
        )

    def test_input_anchor_is_the_checked_line(self) -> None:
        anchor = approval_anchor("bash_input", {"line": "y"}, None)
        assert anchor == "y"

    def test_file_tool_without_path_is_not_markable(self) -> None:
        assert approval_anchor("write", {}, WS) is None
        assert approval_anchor("ast_grep_replace", {"path": ""}, WS) is None


class TestPermissionClass:
    """The two-class derivation: parallel reads vs exclusive read-writes."""

    def test_read_family_is_parallel(self) -> None:
        from modex_agent.sandbox.tool_matrix import PermissionClass

        for name in ("read", "ls", "glob", "grep", "ast_grep_search", "web_reader"):
            assert describe_tool_security(name).permission_class is PermissionClass.PARALLEL, name

    def test_write_family_is_exclusive(self) -> None:
        from modex_agent.sandbox.tool_matrix import PermissionClass

        for name in (
            "write", "edit", "aci_edit", "ast_grep_replace", "bash", "bash_input", "process",
        ):
            assert describe_tool_security(name).permission_class is PermissionClass.EXCLUSIVE, name

    def test_uncatalogued_tools_belong_to_no_class(self) -> None:
        assert describe_tool_security("mcp_unknown").permission_class is None
