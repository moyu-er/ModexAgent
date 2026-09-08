from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.agents.react.nodes.tool_classification import decision_of
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier
from modex_agent.approval.runtime import ApprovalRuntime, TieredToolApprovalClassifier
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.memory.history import ListMessageHistory
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


def test_returns_none_when_config_disabled():
    cfg = ApprovalConfig(enabled=False, tools={"write_file": ToolApprovalEntry(allowed_paths=["./*"])})
    assert build_approval_runtime(cfg, project_root=Path("/proj")) is None


def test_returns_none_when_no_tools():
    cfg = ApprovalConfig(enabled=True, tools={})
    assert build_approval_runtime(cfg, project_root=Path("/proj")) is None


def test_returns_none_when_config_none():
    assert build_approval_runtime(None) is None


def test_builds_runtime_with_path_tier_classifier():
    cfg = ApprovalConfig(
        enabled=True,
        tools={
            "write_file": ToolApprovalEntry(allowed_paths=["./*"]),
            "edit_file": ToolApprovalEntry(allowed_paths=["./*"]),
        },
    )
    rt = build_approval_runtime(cfg, project_root=Path("/proj"))
    assert isinstance(rt, ApprovalRuntime)
    assert isinstance(rt.classifier, TieredToolApprovalClassifier)
    assert rt.classifier.config.enabled is True
    assert set(rt.classifier.config.tools) == {"write_file", "edit_file"}
    assert rt.classifier.config.tools["write_file"].allowed_paths == ["./*"]
    # CRITICAL: a matcher must be injected or path-tiering silently breaks
    assert rt.classifier.argument_matcher is not None
    assert rt.classifier.argument_matcher.project_root == Path("/proj")


@pytest.mark.parametrize("sandbox", [None, SandboxSettings()])
def test_path_tiering_classifies_in_project_normal_and_outside_dangerous(sandbox):
    cfg = ApprovalConfig(
        enabled=True,
        tools={"write_file": ToolApprovalEntry(allowed_paths=["./*"])},
    )
    rt = build_approval_runtime(cfg, project_root=Path("/proj"), sandbox=sandbox)
    assert rt is not None
    classifier = rt.classifier
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.agent"),
    )
    inside = ToolCall(tool_name="write_file", arguments={"path": "./inside.txt"}, call_id="c1")
    outside = ToolCall(tool_name="write_file", arguments={"path": "/etc/passwd"}, call_id="c2")
    assert classifier.classify(inside, ctx).tier is ApprovalTier.NORMAL
    assert classifier.classify(outside, ctx).tier is ApprovalTier.DANGEROUS


@pytest.mark.parametrize("backend", [None, SandboxBackend.DEFAULT, SandboxBackend.HOST])
@pytest.mark.parametrize(
    ("cfg", "active_outside", "dormant_outside", "inside_decision"),
    [
        (None, ApprovalDecision.DENIED, None, ApprovalDecision.ALLOWED),
        (ApprovalConfig(enabled=False), ApprovalDecision.DENIED, None, ApprovalDecision.ALLOWED),
        (ApprovalConfig(enabled=True), ApprovalDecision.PENDING, None, ApprovalDecision.ALLOWED),
        (
            ApprovalConfig(enabled=True, tools={"write": ToolApprovalEntry(allowed_paths=["./*"])}),
            ApprovalDecision.PENDING, ApprovalDecision.PENDING, ApprovalDecision.ALLOWED,
        ),
        (
            ApprovalConfig(enabled=True, tools={"write": ToolApprovalEntry(allowed_paths=[])}),
            ApprovalDecision.PENDING, ApprovalDecision.PENDING, ApprovalDecision.PENDING,
        ),
    ],
    ids=["missing", "disabled", "enabled-empty", "workspace-allowed", "always-ask"],
)
def test_factory_approval_channel_matrix(
    tmp_path: Path,
    backend: SandboxBackend | None,
    cfg: ApprovalConfig | None,
    active_outside: ApprovalDecision,
    dormant_outside: ApprovalDecision | None,
    inside_decision: ApprovalDecision,
) -> None:
    class Root(WorkspaceRootProvider):
        def current(self) -> Path:
            return tmp_path

    runtime = build_approval_runtime(
        cfg, root_provider=Root(),
        sandbox=SandboxSettings(
            backend=backend,
            exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE),
        )
        if backend is not None else None,
    )
    expected = active_outside if backend is SandboxBackend.HOST else dormant_outside
    if expected is None:
        assert runtime is None
        return
    assert runtime is not None
    ctx = AgentContext(
        system_prompt="test", history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(), session=SessionInfo.from_str("test.main"),
    )
    inside = ToolCall(tool_name="write", arguments={"path": str(tmp_path / "inside.txt")}, call_id="in")
    outside = ToolCall(tool_name="write", arguments={"path": str(tmp_path.parent / "outside.txt")}, call_id="out")
    assert decision_of(runtime.classifier.classify(inside, ctx)) is inside_decision
    assert decision_of(runtime.classifier.classify(outside, ctx)) is expected
