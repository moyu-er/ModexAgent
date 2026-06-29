"""Path-security regression tests for tool approval classification.

These tests pin two bugs that the existing suite missed because every prior
test used the non-existent tool name ``write_file`` instead of the real
``write``:

* Bug A — config/tool name mismatch: the production write tool is named
  ``write`` (``WriteFileTool.name``), not ``write_file``. A config that gates
  ``write_file`` gates nothing.
* Bug B — ``..`` traversal: ``ArgumentMatcher`` must resolve paths to their
  real absolute form and apply directory-containment, not fnmatch over raw
  concatenated strings (whose ``*`` crosses ``/`` and matches ``../`` escapes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.agents.react.approval import TieredToolApprovalClassifier
from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
from modex_agent.memory.history import ListMessageHistory
from modex_agent.tools.standard.file_tool import EditFileTool, ReadFileTool, WriteFileTool
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


# ---------------------------------------------------------------------------
# Bug A — canonical tool-name contract
# ---------------------------------------------------------------------------


def test_canonical_file_tool_names():
    """The config must gate tools by their REAL registered names."""
    assert WriteFileTool().name == "write"
    assert EditFileTool().name == "edit"
    assert ReadFileTool().name == "read"


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


def _classifier(
    project_root: Path,
    *,
    allowed_paths: list[str] | None = None,
) -> TieredToolApprovalClassifier:
    cfg = AgentApprovalConfig(
        enabled=True,
        tools={"write": ToolApprovalConfig(allowed_paths=allowed_paths or ["./*"])},
    )
    return TieredToolApprovalClassifier(
        config=cfg,
        argument_matcher=ArgumentMatcher(project_root=project_root),
    )


def _ctx() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )


def _call(path: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(tool_name="write", arguments={"path": path}, call_id=call_id)


# ---------------------------------------------------------------------------
# Bug B — path resolution must reject traversal escapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "./inside.txt",            # plain in-project
        "./sub/deeper/a.txt",      # nested in-project
        "./a/../b.txt",            # .. that collapses back INSIDE the project
    ],
)
def test_in_project_paths_are_normal(tmp_path: Path, path: str) -> None:
    # Anchor an absolute in-project path so it resolves under tmp_path.
    abs_inside = (tmp_path / "real.txt").resolve()
    assert _classifier(tmp_path).classify(_call(str(abs_inside)), _ctx()) == ApprovalTier.NORMAL
    assert _classifier(tmp_path).classify(_call(path), _ctx()) == ApprovalTier.NORMAL


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",             # absolute, outside
        "../escape.txt",           # parent of project root
        "./../../etc/passwd",      # multi-level .. escape
        "../../../etc/passwd",     # bare .. escape
    ],
)
def test_outside_and_traversal_paths_are_dangerous(tmp_path: Path, path: str) -> None:
    assert _classifier(tmp_path).classify(_call(path), _ctx()) == ApprovalTier.DANGEROUS


def test_matcher_dotdot_escape_is_not_allowed(tmp_path: Path) -> None:
    """Direct matcher contract: ``./*`` must NOT match a ``..`` escape."""
    matcher = ArgumentMatcher(project_root=tmp_path)
    assert matcher.matches({"path": "./../../etc/passwd"}, ["./*"]) is False
    assert matcher.matches({"path": "./inside.txt"}, ["./*"]) is True


def test_absolute_outside_path_is_not_allowed(tmp_path: Path) -> None:
    matcher = ArgumentMatcher(project_root=tmp_path)
    outside = (tmp_path.parent / "sibling_escape.txt").resolve()
    assert matcher.matches({"path": str(outside)}, ["./*"]) is False


def test_double_star_recursive_pattern(tmp_path: Path) -> None:
    """``./src/**`` allows anything under src/ but not outside it."""
    matcher = ArgumentMatcher(project_root=tmp_path)
    assert matcher.matches({"path": "./src/a/b/c.txt"}, ["./src/**"]) is True
    assert matcher.matches({"path": "./outside.txt"}, ["./src/**"]) is False


# ---------------------------------------------------------------------------
# Absolute allowed_paths + multi-path (OR) semantics
# ---------------------------------------------------------------------------


class TestAbsoluteAndMultiAllowedPaths:
    """allowed_paths may be absolute, relative, or a mix; multiple entries OR."""

    def test_absolute_allowed_accepts_inside_rejects_outside(self, tmp_path: Path) -> None:
        allowed = tmp_path / "safe"
        allowed.mkdir()
        matcher = ArgumentMatcher(project_root=tmp_path)
        assert matcher.matches({"path": str(allowed / "f.txt")}, [str(allowed)]) is True
        # Same tree but outside the absolute allowed root.
        assert matcher.matches({"path": str(tmp_path / "out.txt")}, [str(allowed)]) is False

    def test_absolute_allowed_independent_of_project_root(self, tmp_path: Path) -> None:
        """An absolute allowed root must NOT be re-anchored to project_root."""
        project = tmp_path / "proj"
        project.mkdir()
        allowed = tmp_path / "elsewhere"
        allowed.mkdir()
        matcher = ArgumentMatcher(project_root=project)
        # Tool path under the ABSOLUTE allowed dir (not under project_root).
        assert matcher.matches({"path": str(allowed / "x.txt")}, [str(allowed)]) is True
        # A relative tool path still anchors to project_root, not the allowed dir.
        assert matcher.matches({"path": "./x.txt"}, [str(allowed)]) is False

    def test_multiple_allowed_paths_or(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        matcher = ArgumentMatcher(project_root=tmp_path)
        patterns = [str(a), str(b)]
        assert matcher.matches({"path": str(a / "1.txt")}, patterns) is True
        assert matcher.matches({"path": str(b / "2.txt")}, patterns) is True
        assert matcher.matches({"path": str(tmp_path / "c" / "3.txt")}, patterns) is False

    def test_mixed_absolute_and_relative_patterns(self, tmp_path: Path) -> None:
        abs_dir = tmp_path / "abs"
        abs_dir.mkdir()
        matcher = ArgumentMatcher(project_root=tmp_path)
        patterns = [str(abs_dir), "./*"]  # absolute dir + project tree
        assert matcher.matches({"path": str(abs_dir / "x")}, patterns) is True   # via absolute
        assert matcher.matches({"path": "./inside.txt"}, patterns) is True       # via ./*
        sibling = tmp_path.parent / "sibling_escape"
        assert matcher.matches({"path": str(sibling)}, patterns) is False

    @pytest.mark.parametrize("suffix", ["", "/*", "/**", "/"])
    def test_absolute_allowed_glob_and_slash_variants(
        self, tmp_path: Path, suffix: str
    ) -> None:
        allowed = tmp_path / "safe"
        allowed.mkdir()
        matcher = ArgumentMatcher(project_root=tmp_path)
        pattern = str(allowed) + suffix
        assert matcher.matches({"path": str(allowed / "deep" / "f.txt")}, [pattern]) is True
        assert matcher.matches({"path": str(tmp_path / "out.txt")}, [pattern]) is False

    def test_home_tilde_allowed_path(self, tmp_path: Path) -> None:
        matcher = ArgumentMatcher(project_root=tmp_path)
        notes = Path.home() / "approval_notes_dir"
        assert matcher.matches({"path": str(notes / "x")}, ["~/approval_notes_dir"]) is True
        assert matcher.matches({"path": str(tmp_path / "x")}, ["~/approval_notes_dir"]) is False

    def test_dotdot_declared_grants_outside_project(self, tmp_path: Path) -> None:
        """An explicitly-declared ``../*`` grants the parent tree, outside the
        project. Declared escapes are honoured; only undeclared paths deny."""
        matcher = ArgumentMatcher(project_root=tmp_path)
        sibling = tmp_path.parent / "sibling_dir"
        assert matcher.matches({"path": str(sibling / "x.txt")}, ["../*"]) is True
        # "../*" covers exactly one level up; two levels up is still denied.
        far = tmp_path.parent.parent / "far"
        assert matcher.matches({"path": str(far / "y")}, ["../*"]) is False

    def test_absolute_outside_project_declared_grants(self, tmp_path: Path) -> None:
        """An absolute allowed root far outside the project is honoured."""
        outside = tmp_path.parent / "external_vault"
        matcher = ArgumentMatcher(project_root=tmp_path)
        assert matcher.matches({"path": str(outside / "a" / "b")}, [str(outside)]) is True


# ---------------------------------------------------------------------------
# Classifier-level end-to-end with absolute allowed_paths
# ---------------------------------------------------------------------------


def test_classifier_absolute_allowed_paths_end_to_end(tmp_path: Path) -> None:
    allowed = tmp_path / "vault"
    allowed.mkdir()
    classifier = _classifier(tmp_path, allowed_paths=[str(allowed)])
    inside = ToolCall(
        tool_name="write",
        arguments={"path": str(allowed / "secret.txt")},
        call_id="c1",
    )
    outside = ToolCall(
        tool_name="write",
        arguments={"path": str(tmp_path / "leak.txt")},
        call_id="c2",
    )
    assert classifier.classify(inside, _ctx()) == ApprovalTier.NORMAL
    assert classifier.classify(outside, _ctx()) == ApprovalTier.DANGEROUS


# ---------------------------------------------------------------------------
# Workspace-anchored classification: the matcher reads the live
# WorkspaceRootProvider (the SAME provider the file tools use), so ``./*``
# follows the active workspace instead of a static bot project_dir.
# ---------------------------------------------------------------------------


class _StubRootProvider(WorkspaceRootProvider):
    """Live root provider whose value can change to model a /cd switch."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def current(self) -> Path:
        return self.root


def test_classifier_anchors_to_live_workspace_not_static_project_root(
    tmp_path: Path,
) -> None:
    """``./*`` resolves against the workspace, not a stale bot project_dir."""
    bot_project_dir = tmp_path / "bot_project"
    bot_project_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    cfg = AgentApprovalConfig(
        enabled=True,
        tools={"write": ToolApprovalConfig(allowed_paths=["./*"])},
    )
    classifier = TieredToolApprovalClassifier(
        config=cfg,
        argument_matcher=ArgumentMatcher(root_provider=_StubRootProvider(workspace)),
    )

    assert classifier.classify(_call(str(workspace / "f.txt")), _ctx()) == ApprovalTier.NORMAL
    assert classifier.classify(_call(str(bot_project_dir / "f.txt")), _ctx()) == ApprovalTier.DANGEROUS


def test_matcher_follows_workspace_switch(tmp_path: Path) -> None:
    """A live provider means a workspace switch needs no re-wiring."""
    ws_a = tmp_path / "a"
    ws_a.mkdir()
    ws_b = tmp_path / "b"
    ws_b.mkdir()
    provider = _StubRootProvider(ws_a)
    matcher = ArgumentMatcher(root_provider=provider)

    assert matcher.matches({"path": str(ws_a / "x.txt")}, ["./*"]) is True
    assert matcher.matches({"path": str(ws_b / "y.txt")}, ["./*"]) is False

    provider.root = ws_b  # user /cd'd into ws_b
    assert matcher.matches({"path": str(ws_b / "y.txt")}, ["./*"]) is True
    assert matcher.matches({"path": str(ws_a / "x.txt")}, ["./*"]) is False


def test_root_provider_takes_precedence_over_static_project_root(
    tmp_path: Path,
) -> None:
    """When both are supplied, the live provider wins (project_root is a fallback)."""
    static_root = tmp_path / "static"
    static_root.mkdir()
    live_root = tmp_path / "live"
    live_root.mkdir()
    matcher = ArgumentMatcher(
        project_root=static_root, root_provider=_StubRootProvider(live_root)
    )
    assert matcher.matches({"path": str(live_root / "f.txt")}, ["./*"]) is True
    assert matcher.matches({"path": str(static_root / "f.txt")}, ["./*"]) is False
