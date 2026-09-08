"""Approval system constants."""

from enum import StrEnum


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


class ApprovalAuditSource(StrEnum):
    """Provenance of an audit row — which boundary produced it.

    ``RUNTIME`` is the default (in-process approval/guard decisions).
    ``DELEGATION`` marks subagent delegation-boundary decisions; it is the
    same value :class:`DelegationSnapshot.source` carries.
    """

    RUNTIME = "runtime"
    DELEGATION = "delegation"
