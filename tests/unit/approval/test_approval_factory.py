from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.approval import ApprovalRuntime, TieredToolApprovalClassifier
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime


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
