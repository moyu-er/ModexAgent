"""SecurityClassifier — guard verdicts riding the approval tier channel.

Maps guard verdicts onto the existing ``ApprovalTransaction`` and
``GraphInterrupt`` path. Sandbox and human approval have independent
toggles but share classification; disabling prompts does not disable guards.

The mapping is fixed; hard findings cannot be configured as approvable:

- ``DENY_RULE`` → ``HARDLINE``
  (``ToolNode`` maps HARDLINE to ``ApprovalDecision.DENIED`` — a hard
  error ToolResult, never a card).
- ``BOUNDARY`` / ``SSRF`` are the approvable gray zones
  (``APPROVABLE_CATEGORIES``): ``DANGEROUS`` when ``escalate_enabled``
  — a human arbitrates them on the standard card; without a human
  channel the tier result is ``HARDLINE`` — the only tier ``ToolNode``
  denies. The two categories share one branch on purpose: whatever the
  classifier approves must be waivable at execution time, and the
  interceptor's marker waiver keys on the same set.
  The immutable classification carries its reason and audit fact; there
  is no mutable last-denial side channel.
- ``CLEAN`` → ``inner.classify(...)`` verbatim: the existing
  :class:`~modex_agent.approval.runtime.TieredToolApprovalClassifier`
  applies per-tool prompt exemptions without expanding the guard envelope.

Tool dispatch rides the typed tool-effect seam
(``SecurityDecisionService.evaluate_tool_call`` over
``sandbox.tool_matrix``). Tools outside the vocabulary pass through
to the inner classifier with no guard judgment.

Resumed approved calls skip reclassification. At execution time,
``SandboxGuardInterceptor`` honors ``TurnCustomKey.HUMAN_APPROVED_CALLS``
only for the matching call's BOUNDARY finding. Approval cannot waive hard
findings, kernel restrictions, or authorize automatic HOST replay.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from modex_agent.approval.classification import (
    ClassificationSource,
    GuardAuditFact,
    ToolClassification,
)
from modex_agent.approval.constants import ApprovalAuditDecision, ApprovalTier
from modex_agent.approval.runtime import ApprovalClassifier, ApprovalRuntime
from modex_agent.sandbox.approval_envelope import validate_approval_envelope
from modex_agent.sandbox.decision import SecurityDecisionService
from modex_agent.sandbox.settings import SandboxSettings
from modex_agent.sandbox.verdict import (
    APPROVABLE_CATEGORIES,
    GuardCategory,
    GuardVerdict,
)

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.message import ToolCall
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

__all__ = [
    "SecurityClassifier",
    "guard_only_runtime",
    "validate_approval_envelope",
]


class SecurityClassifier(ApprovalClassifier):
    """Composite classifier: guard verdict first, inner tiers second.

    Construction injects a ``SecurityDecisionService``, the
    existing tiered classifier as ``inner``, and the ``escalate_enabled``
    capability bit. ``classify`` returns a pure
    :class:`~modex_agent.approval.classification.ToolClassification` —
    the guard's deny/escalation reason and audit fact ride the value
    itself; the classifier holds no mutable state.

    ``deny_message_builder`` lets a deployment re-shape the
    classification-time denial into its own copy: when set, every HARDLINE
    classification routes the verdict reason through the builder and the
    RESULT becomes the classification ``reason``. The delegation
    deployment passes
    :func:`~modex_agent.sandbox.delegation.delegation_denial_message` so
    subagent denials name the restriction and direct requests to the main session.
    """

    def __init__(
        self,
        *,
        decision: SecurityDecisionService,
        inner: ApprovalClassifier,
        escalate_enabled: bool,
        deny_message_builder: Callable[[str, str, str], str] | None = None,
    ) -> None:
        self._decision = decision
        self._inner = inner
        self._escalate_enabled = escalate_enabled
        self._deny_message_builder = deny_message_builder

    @property
    def inner(self) -> ApprovalClassifier:
        """The wrapped tiered classifier (the CLEAN fallback)."""
        return self._inner

    @property
    def escalate_enabled(self) -> bool:
        """Whether BOUNDARY findings escalate to a human-arbitrated card."""
        return self._escalate_enabled

    def _deny(
        self,
        verdict: GuardVerdict,
        tool_name: str,
    ) -> ToolClassification:
        """HARDLINE with the deny reason (raw or builder-shaped) + audit fact."""
        reason = verdict.reason or ""
        if self._deny_message_builder is not None:
            reason = self._deny_message_builder(reason, tool_name, verdict.target or reason)
        return ToolClassification(
            tier=ApprovalTier.HARDLINE,
            source=ClassificationSource.GUARD,
            guard_category=verdict.category,
            reason=reason,
            audit=GuardAuditFact(decision=ApprovalAuditDecision.DENIED),
        )

    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ToolClassification:
        verdict = self._evaluate(tool_call)
        if verdict.category is GuardCategory.CLEAN:
            return self._inner.classify(tool_call, ctx)
        if verdict.category in APPROVABLE_CATEGORIES and self._escalate_enabled:
            # The human-arbitrable gray zones (BOUNDARY / SSRF) share one
            # escalation branch: whatever lands on a card must be waivable
            # at execution time via APPROVABLE_CATEGORIES.
            return ToolClassification(
                tier=ApprovalTier.DANGEROUS,
                source=ClassificationSource.GUARD,
                guard_category=verdict.category,
                reason=verdict.reason,
                audit=GuardAuditFact(decision=ApprovalAuditDecision.ESCALATED),
            )
        return self._deny(verdict, tool_call.tool_name)

    def _evaluate(self, tool_call: ToolCall) -> GuardVerdict:
        """Judge via the service's typed tool-effect seam (one dispatch)."""
        return self._decision.evaluate_tool_call(
            tool_call.tool_name, dict(tool_call.arguments or {})
        )


def guard_only_runtime(
    *,
    settings: SandboxSettings,
    root_provider: WorkspaceRootProvider,
    deny_message_builder: Callable[[str, str, str], str] | None = None,
) -> ApprovalRuntime:
    """Build the escalate-off composite runtime (one mechanism, all callers).

    Used by explicit sandbox assembly without approval, native delegation,
    and graph turns without human arbitration. Keep this runtime rather than
    setting ``approval=None``: guard findings must still deny without prompts.
    Delegation supplies its denial renderer and frozen-root provider.

    The inner tiered classifier is all-NORMAL (``enabled=False``): every
    tier decision comes from the guard layer.
    """
    from modex_agent.approval.config import AgentApprovalConfig
    from modex_agent.approval.runtime import TieredToolApprovalClassifier
    from modex_agent.sandbox.decision import SecurityDecisionService

    return ApprovalRuntime(
        classifier=SecurityClassifier(
            decision=SecurityDecisionService(
                settings=settings, workspace_root_provider=root_provider
            ),
            inner=TieredToolApprovalClassifier(
                config=AgentApprovalConfig(enabled=False),
            ),
            escalate_enabled=False,
            deny_message_builder=deny_message_builder,
        )
    )
