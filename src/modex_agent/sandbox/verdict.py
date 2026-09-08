"""Guard verdict vocabulary — the categories and verdicts guards produce.

These are the pure types every security layer speaks: the decision
service produces them, the classifier maps them onto approval tiers,
the interceptor words them into denial copy. Facts, never presentation
copy; the owning service lives in ``decision.py``.

Category semantics:

- ``DENY_RULE`` — a deny rule or write-under-READ_ONLY hit.
  Deterministic danger / hard policy refuse; never approvable (HARDLINE
  in the tier table). READ_ONLY refusing write tools is a *policy*, not
  a boundary-outside path: the deployment forbids file-tool writes, and no
  human approval rewrites that declaration mid-session. It therefore
  lands here, not in BOUNDARY.
- ``SSRF`` — private/internal URL target. APPROVABLE gray zone: the
  classifier escalates it like BOUNDARY (card on main agents, direct
  denial on subagents) — network is not path-permission scope.
- ``BOUNDARY`` — a path/command outside the declared sandbox envelope
  (workspace + writable_roots). The gray zone: human-arbitrable
  (DANGEROUS) for main agents with approval enabled; otherwise denied.
- ``CLEAN`` — no finding; the caller falls back to its own baseline.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

__all__ = [
    "APPROVABLE_CATEGORIES",
    "GuardCategory",
    "GuardVerdict",
]


class GuardCategory(StrEnum):
    """What kind of security finding a verdict reports."""

    DENY_RULE = "deny_rule"  # Deny rule or READ_ONLY write refusal; not approvable
    SSRF = "ssrf"  # Private/internal URL target
    BOUNDARY = "boundary"  # Outside allowed roots; human escalation when enabled
    CLEAN = "clean"  # No finding, not proof of containment


# The categories a human approval channel arbitrates (card on main agents
# with escalation). The ``SecurityClassifier`` escalation branches AND the
# ``SandboxGuardInterceptor`` approval-marker waiver consume this one set —
# a category listed here can be approved on a card, so its execution-time
# denial must honor the approval marker; a category outside it can never.
APPROVABLE_CATEGORIES: frozenset[GuardCategory] = frozenset(
    {GuardCategory.BOUNDARY, GuardCategory.SSRF}
)


class GuardVerdict(BaseModel):
    """One security-judgment outcome — facts, never presentation copy.

    ``reason`` is the guard's original reason (deny/SSRF findings) or a
    short fixed phrase identifying the fact (read-only refuse, boundary
    escape). Presentation copy is the interceptor's job.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: GuardCategory
    reason: str | None = None
    target: str | None = None
    allowed_roots: tuple[Path, ...] = ()

    @property
    def is_clean(self) -> bool:
        return self.category is GuardCategory.CLEAN
