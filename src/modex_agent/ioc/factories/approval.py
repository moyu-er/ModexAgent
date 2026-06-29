"""ApprovalRuntime factory — converts ioc ApprovalConfig (YAML) into the
framework ApprovalRuntime + TieredToolApprovalClassifier.

Returns None when approval is effectively a no-op (disabled, or no tools gated)
so callers can skip wiring entirely when approval would be a no-op.

An ``ArgumentMatcher`` is always injected when a runtime is built: without it
the classifier cannot evaluate path patterns (``["./*"]``), and every gated
tool would wrongly classify as DANGEROUS even inside the project.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.approval import ApprovalRuntime, TieredToolApprovalClassifier
from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


def build_approval_runtime(
    cfg: ApprovalConfig | None,
    *,
    project_root: Path | None = None,
    root_provider: WorkspaceRootProvider | None = None,
) -> ApprovalRuntime | None:
    """Build an ``ApprovalRuntime`` from ioc config, or None when it is a no-op.

    ``root_provider`` (preferred) supplies the live active-workspace working
    dir, read on every classification so ``./*`` follows a workspace switch with
    no re-wiring — the SAME provider the file tools use. ``project_root`` is a
    static fallback for callers without a workspace (None resolves ``.`` against
    the process cwd). Passing a static ``project_root`` that is NOT the active
    workspace (e.g. the bot project dir) wrongly gates in-workspace writes as
    DANGEROUS — pass ``root_provider`` in workspace deployments.
    """
    if cfg is None or not cfg.enabled or not cfg.tools:
        return None

    framework_tools = {
        name: ToolApprovalConfig(allowed_paths=list(entry.allowed_paths))
        for name, entry in cfg.tools.items()
    }

    classifier = TieredToolApprovalClassifier(
        config=AgentApprovalConfig(enabled=True, tools=framework_tools),
        argument_matcher=ArgumentMatcher(
            project_root=project_root, root_provider=root_provider
        ),
    )
    return ApprovalRuntime(classifier=classifier)
