"""ApprovalRuntime factory — converts the raw ``approval:`` declaration
mapping into the framework ApprovalRuntime + TieredToolApprovalClassifier
(W3a: moved from the deleted ``ioc/factories/approval.py``; W1-B2: moved
into the capability bundle and re-faced onto the raw declaration — the
schema holds the raw face, the bundle interprets it via
``ApprovalConfig.model_validate`` at entry).

Without an explicit sandbox, disabled approval or an empty tools map returns
None. With a sandbox, guard classification remains active: disabled approval
denies findings without prompts, while enabled main-agent approval escalates
BOUNDARY even with an empty tools map. Independent toggles share one
classification and transaction path.

An ``ArgumentMatcher`` is injected for enabled per-tool rules: without it
the classifier cannot evaluate path patterns (``["./*"]``), and every gated
tool would wrongly classify as DANGEROUS even inside the project.

``sandbox=``: a non-DEFAULT
:class:`~modex_agent.plugins.defaults.capabilities.sandbox.settings.SandboxSettings` wraps the tiered
classifier in the composite
:class:`~modex_agent.plugins.defaults.capabilities.approval.security.SecurityClassifier`
sharing the same root provider — the single assembly point where
``runtime.services.tool_gate`` gains the guard layer. Assembly-time
containment (approval ``allowed_paths`` ⊆ sandbox envelope) fails fast
here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.core.workspace_root import WorkspaceRootProvider
from modex_agent.plugins.defaults.capabilities.approval.argument_matcher import (
    ArgumentMatcher,
)
from modex_agent.plugins.defaults.capabilities.approval.config import (
    AgentApprovalConfig,
    ApprovalConfig,
    ToolApprovalConfig,
)
from modex_agent.plugins.defaults.capabilities.approval.runtime import (
    ApprovalClassifier,
    ApprovalRuntime,
    TieredToolApprovalClassifier,
)
from modex_agent.plugins.defaults.capabilities.approval.security import (
    SecurityClassifier,
    guard_only_runtime,
)
from modex_agent.plugins.defaults.capabilities.sandbox.approval_envelope import (
    validate_approval_envelope,
)
from modex_agent.plugins.defaults.capabilities.sandbox.decision import SecurityDecisionService
from modex_agent.plugins.defaults.capabilities.sandbox.settings import (
    SandboxBackend,
    SandboxSettings,
)


def build_approval_runtime(
    cfg: dict[str, Any] | None,
    *,
    project_root: Path | None = None,
    root_provider: WorkspaceRootProvider | None = None,
    sandbox: SandboxSettings | None = None,
) -> ApprovalRuntime | None:
    """Build an ``ApprovalRuntime`` from the raw approval declaration
    mapping, or None when it is a no-op.

    ``cfg`` is the RAW ``approval:`` declaration (or the capability
    override mapping — same shape): it is validated through
    ``ApprovalConfig.model_validate`` at entry, so a malformed
    declaration fails loudly HERE, at the single assembly point, rather
    than at a downstream classification.

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
      when the validated config's ``enabled`` is true, even with no configured
      tools. Otherwise use the guard-only composite
      (``escalate=False``, inner all-NORMAL) so gray-zone verdicts deny
      without a card channel.
      ``root_provider`` is required in this mode (the guard's boundary
      follows the live workspace root).
    """
    validated = ApprovalConfig.model_validate(cfg) if cfg is not None else None

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
        validate_approval_envelope(validated, settings=sandbox, root_provider=root_provider)
        service = SecurityDecisionService(settings=sandbox, workspace_root_provider=root_provider)
        if validated is None or not validated.enabled:
            return guard_only_runtime(decision=service)
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
                        for name, entry in validated.tools.items()
                    },
                ),
                argument_matcher=ArgumentMatcher(
                    project_root=project_root, root_provider=root_provider
                ),
            ),
            escalate_enabled=True,
        )
        return ApprovalRuntime(classifier=inner)

    if validated is None or not validated.enabled or not validated.tools:
        return None

    framework_tools = {
        name: ToolApprovalConfig(
            allowed_paths=list(entry.allowed_paths),
            allow_patterns=list(entry.allow_patterns),
        )
        for name, entry in validated.tools.items()
    }
    classifier: ApprovalClassifier = TieredToolApprovalClassifier(
        config=AgentApprovalConfig(enabled=True, tools=framework_tools),
        argument_matcher=ArgumentMatcher(project_root=project_root, root_provider=root_provider),
    )
    return ApprovalRuntime(classifier=classifier)
