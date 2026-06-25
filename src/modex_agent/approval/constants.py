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
