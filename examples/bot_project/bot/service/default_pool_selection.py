"""Unified default-pool selection for NEW conversations (PA-07).

One pure rule answering: given the user's configured preference, the declared
(configured) pools, and the runtime-available pools, which pool does a NEW
choice default to? Every entry (WebUI composer, REST create, WS attach, IM
first message) consumes this one decision — no entry invents its own default,
and no entry silently falls back to ``main``/first-item/first-declared when
the preference is absent or invalid (DESIGN §3.3): the decision reports
``NONE_CONFIGURED`` / ``PREFERRED_UNAVAILABLE`` and the caller must surface a
selection prompt. The one deterministic fallback the product allows is the
shipped *pristine* preference value ``default`` (held by PA-06, not by this
function): callers pass it as ``preferred`` like any other configured value,
and its unavailability is reported — never substituted.

Priority for the *effective default* (explicit client choices, existing
attribution/routing always win upstream — see S5 ``ResolvePoolStage``):

1. configured preferred pool, iff declared AND runtime-available
2. none — caller prompts (``NONE_CONFIGURED`` / ``PREFERRED_UNAVAILABLE``)

The configured-vs-runtime split is explicit: ``declared_pools`` is what the
scope declaration + preferences say CAN exist (declaration order = user
intent order); ``runtime_pools`` is what is actually assembled right now
(external CLI present, pool not deleted, restart applied). A preferred pool
that is declared but not running yields ``PREFERRED_UNAVAILABLE`` — never a
substitute.

Rule-12: frozen Pydantic decision value; the function itself is pure.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DefaultPoolStatus(StrEnum):
    """Why the decision is what it is — the caller surfaces this."""

    PREFERRED = "preferred"
    PREFERRED_UNAVAILABLE = "preferred_unavailable"
    NONE_CONFIGURED = "none_configured"


class DefaultPoolDecision(BaseModel):
    """The selection outcome. ``pool is None`` ⇒ the caller must prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str | None
    status: DefaultPoolStatus
    reason: str = ""


def resolve_default_pool(
    *,
    preferred: str | None,
    declared_pools: list[str] | tuple[str, ...],
    runtime_pools: set[str] | frozenset[str],
) -> DefaultPoolDecision:
    """Resolve the effective default pool for a NEW conversation choice."""
    if preferred is None:
        # No preference configured (never saved, or unreadable config — the
        # preference owner surfaces the read error separately). DESIGN §3.3:
        # NEVER pick a first/other pool on the user's behalf; the caller must
        # ask for an explicit choice.
        return DefaultPoolDecision(
            pool=None,
            status=DefaultPoolStatus.NONE_CONFIGURED,
            reason="no default pool preference is configured; select a pool",
        )
    if preferred in runtime_pools and preferred in declared_pools:
        return DefaultPoolDecision(
            pool=preferred,
            status=DefaultPoolStatus.PREFERRED,
        )
    if preferred in declared_pools:
        return DefaultPoolDecision(
            pool=None,
            status=DefaultPoolStatus.PREFERRED_UNAVAILABLE,
            reason=(
                f"preferred pool {preferred!r} is declared but not "
                "currently available (not restarted, or its runtime "
                "dependencies such as an external CLI are missing)"
            ),
        )
    return DefaultPoolDecision(
        pool=None,
        status=DefaultPoolStatus.PREFERRED_UNAVAILABLE,
        reason=(
            f"preferred pool {preferred!r} is not declared; it may have "
            "been removed from the scope declaration"
        ),
    )
