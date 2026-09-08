"""ToolClassification — the single typed outcome of one approval classification.

``ApprovalClassifier.classify`` returns ONE frozen strict value carrying the
tier, the typed classification source (tier rules vs guard verdict), the
guard category, the deny-side reason, and — when the guard made a
decision — the audit fact. The classifier performs no writes and no
suspension; ToolNode derives every downstream decision from this value.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from modex_agent.approval.constants import (
    ApprovalAuditDecision,
    ApprovalTier,
    DecisionActor,
)
from modex_agent.sandbox.verdict import GuardCategory

__all__ = [
    "ClassificationSource",
    "GuardAuditFact",
    "ToolClassification",
]


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
