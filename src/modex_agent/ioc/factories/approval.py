"""ApprovalRuntime factory — converts ioc ApprovalConfig (YAML) into the
framework ApprovalRuntime + TieredToolApprovalClassifier.

Without an explicit sandbox, disabled approval or an empty tools map returns
None. With a sandbox, guard classification remains active: disabled approval
denies findings without prompts, while enabled main-agent approval escalates
BOUNDARY even with an empty tools map. Independent toggles share one
classification and transaction path.

An ``ArgumentMatcher`` is injected for enabled per-tool rules: without it
the classifier cannot evaluate path patterns (``["./*"]``), and every gated
tool would wrongly classify as DANGEROUS even inside the project.

``sandbox=``: a non-DEFAULT
:class:`~modex_agent.sandbox.settings.SandboxSettings` wraps the tiered
classifier in the composite
:class:`~modex_agent.sandbox.security_classifier.SecurityClassifier`
sharing the same root provider — the single assembly point where
``runtime.services.approval`` gains the guard layer. Assembly-time
containment (approval ``allowed_paths`` ⊆ sandbox envelope) fails fast
here.
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.approval.runtime import (
    ApprovalClassifier,
    ApprovalRuntime,
    TieredToolApprovalClassifier,
)
from modex_agent.interceptor.builtin.tool_approval import ArgumentMatcher
from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.sandbox.decision import SecurityDecisionService
from modex_agent.sandbox.security_classifier import (
    SecurityClassifier,
    guard_only_runtime,
    validate_approval_envelope,
)
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


def build_approval_runtime(
    cfg: ApprovalConfig | None,
    *,
    project_root: Path | None = None,
    root_provider: WorkspaceRootProvider | None = None,
    sandbox: SandboxSettings | None = None,
) -> ApprovalRuntime | None:
    """Build an ``ApprovalRuntime`` from ioc config, or None when it is a no-op.

    ``root_provider`` (preferred) supplies the live active-workspace working
    dir, read on every classification so ``./*`` follows a workspace switch with
    no re-wiring — the SAME provider the file tools use. ``project_root`` is a
    static fallback for callers without a workspace (None resolves ``.`` against
    the process cwd). Passing a static ``project_root`` that is NOT the active
    workspace (e.g. the bot project dir) wrongly gates in-workspace writes as
    DANGEROUS — pass ``root_provider`` in workspace deployments.

    ``sandbox`` activates the composite:

    - ``None`` or ``backend == DEFAULT`` uses only independent tier approval,
      or returns None when no per-tool rules are active.
    - An explicit backend → ``validate_approval_envelope`` fails fast on
      approval ``allowed_paths`` outside the sandbox envelope, then the
      classifier is wrapped in ``SecurityClassifier``: escalation is enabled
      when ``cfg.enabled`` is true, even with no configured tools. Otherwise
      use the guard-only composite
      (``escalate=False``, inner all-NORMAL) so gray-zone verdicts deny
      without a card channel.
      ``root_provider`` is required in this mode (the guard's boundary
      follows the live workspace root).
    """
    if sandbox is not None and sandbox.backend is SandboxBackend.DEFAULT:
        sandbox = None

    if sandbox is not None:
        if root_provider is None:
            raise ValueError(
                "sandbox-activated approval assembly requires root_provider "
                "(the live workspace root source the guard boundary and the "
                "approval patterns share) — pass the same provider the file "
                "tools use"
            )
        validate_approval_envelope(cfg, settings=sandbox, root_provider=root_provider)
        if cfg is None or not cfg.enabled:
            return guard_only_runtime(settings=sandbox, root_provider=root_provider)
        service = SecurityDecisionService(settings=sandbox, workspace_root_provider=root_provider)
        inner: ApprovalClassifier = SecurityClassifier(
            decision=service,
            inner=TieredToolApprovalClassifier(
                config=AgentApprovalConfig(
                    enabled=True,
                    tools={
                        name: ToolApprovalConfig(
                            allowed_paths=list(entry.allowed_paths),
                            allow_patterns=list(entry.allow_patterns),
                        )
                        for name, entry in cfg.tools.items()
                    },
                ),
                argument_matcher=ArgumentMatcher(
                    project_root=project_root, root_provider=root_provider
                ),
            ),
            escalate_enabled=True,
        )
        return ApprovalRuntime(classifier=inner)

    if cfg is None or not cfg.enabled or not cfg.tools:
        return None

    framework_tools = {
        name: ToolApprovalConfig(
            allowed_paths=list(entry.allowed_paths),
            allow_patterns=list(entry.allow_patterns),
        )
        for name, entry in cfg.tools.items()
    }
    classifier: ApprovalClassifier = TieredToolApprovalClassifier(
        config=AgentApprovalConfig(enabled=True, tools=framework_tools),
        argument_matcher=ArgumentMatcher(project_root=project_root, root_provider=root_provider),
    )
    return ApprovalRuntime(classifier=classifier)
