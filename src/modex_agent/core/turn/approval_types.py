"""Turn approval vocabulary — enums, audit timeline, and classification.

Shared by every level: turn state (``core.turn.models``), the approval
runtime, the guard/classifier seam, the audit trail, and persistence
adapters live off these closed value sets — core is the only home legal
for all readers (W1 layering surgery; W3b merged
``approval/classification.py`` here so the sandbox classifier speaks the
same outcome type without importing approval).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from modex_agent.core.guard import GuardCategory


class ApprovalDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING = "pending"
    PREEMPTED = "preempted"


class ApprovalTier(StrEnum):
    NORMAL = "normal"
    DANGEROUS = "dangerous"
    SENSITIVE = "sensitive"
    HARDLINE = "hardline"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    PARTIAL = "partial"


class ApprovalAuditDecision(StrEnum):
    """Shared audit vocabulary for guard findings and human decisions.

    ``ESCALATED`` records a guard-driven escalation — the gray zone handed
    to a human. It is deliberately distinct from ``APPROVED``: a guard
    escalation is never an approval, and conflating them lies on the audit
    timeline. Human decisions remain ``APPROVED``/``DENIED``.
    """

    APPROVED = "approved"
    DENIED = "denied"
    ESCALATED = "escalated"


class DecisionActor(StrEnum):
    """Who made a decision that lands on the audit timeline."""

    USER = "user"
    SANDBOX_GUARD = "sandbox_guard"
    REQUEST_CANCEL = "request_cancel"
    """A request-scope cancellation terminated the batch (never an LLM run)."""


class ApprovalAuditSource(StrEnum):
    """Provenance of an audit row — which boundary produced it.

    ``RUNTIME`` is the default (in-process approval/guard decisions).
    ``DELEGATION`` marks subagent delegation-boundary decisions; it is the
    same value :class:`DelegationSnapshot.source` carries.
    """

    RUNTIME = "runtime"
    DELEGATION = "delegation"


class ClassificationSource(StrEnum):
    """Which layer produced the classification."""

    TIER = "tier"
    GUARD = "guard"


class GuardAuditFact(BaseModel):
    """The guard-made decision one classification carries for the audit sink.

    ``ESCALATED`` records the gray zone handed to a human; it is never
    ``APPROVED`` — a guard escalation is not an approval.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ApprovalAuditDecision
    decided_by: DecisionActor = DecisionActor.SANDBOX_GUARD

    @model_validator(mode="after")
    def _guard_never_approves(self) -> GuardAuditFact:
        if self.decided_by is DecisionActor.SANDBOX_GUARD:
            allowed = (ApprovalAuditDecision.DENIED, ApprovalAuditDecision.ESCALATED)
            if self.decision not in allowed:
                raise ValueError(
                    f"a guard-made audit fact must be DENIED or ESCALATED, got {self.decision}"
                )
        return self


class ToolClassification(BaseModel):
    """One classification outcome — pure data, no side effects.

    ``tier_result`` builds the plain tier-rules outcome; guard classifiers
    attach ``guard_category``, the deny/escalation ``reason``, and the
    ``audit`` fact. ``audit`` is present only when the guard decided;
    ``audit`` on a TIER-source result is a contract violation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: ApprovalTier
    source: ClassificationSource = ClassificationSource.TIER
    guard_category: GuardCategory | None = None
    reason: str | None = None
    audit: GuardAuditFact | None = None

    @model_validator(mode="after")
    def _audit_requires_guard_source(self) -> ToolClassification:
        if self.audit is not None and self.source is not ClassificationSource.GUARD:
            raise ValueError("audit fact requires source=GUARD")
        return self

    @classmethod
    def tier_result(cls, tier: ApprovalTier) -> ToolClassification:
        return cls(tier=tier)

    @property
    def deny_reason(self) -> str | None:
        """The deny-side reason, when this classification denies."""
        if self.audit is not None and self.audit.decision is ApprovalAuditDecision.DENIED:
            return self.reason
        return None
