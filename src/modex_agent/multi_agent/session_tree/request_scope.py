"""RequestScope — one interactive user request owned by the session tree.

DESIGN.md §6 (acp-adapter): an awaitable, cancellable "request scope" is the
tree owner's record that the CURRENT user request occupies a session tree.
It is an attribution fact inside the existing tree/poller machinery — NOT a
second executor, store, or completion signal:

- waiting delegates to ``SessionTreeManager.wait_quiesce`` (the only wait);
- cancellation delegates to the poller's single-flight cancel + finalizer;
- approval stays with the original resumer/coordinator transaction.

Ordinary records carry no scope and keep their exact prior semantics; a
session whose policy is request-scoped rejects tokenless prompts even when
idle (no other entrance may bypass the scope).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.emitter import AgentResult


class RequestScopeState(StrEnum):
    """Lifecycle of one request scope (DESIGN.md §6.2)."""

    RESERVED = "reserved"
    """Admission token issued; nothing delivered yet (two-phase begin)."""

    ACTIVE = "active"
    """Submitted; root/child turns running or pending."""

    AWAITING_APPROVAL = "awaiting_approval"
    """A root turn suspended on a pending approval batch."""

    CANCELLING = "cancelling"
    """Admission closed; draining turns/tracks/approvals."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    """Terminal states. INTERRUPTED = restart settlement, never a replay."""


_TERMINAL_STATES: frozenset[RequestScopeState] = frozenset({
    RequestScopeState.COMPLETED,
    RequestScopeState.CANCELLED,
    RequestScopeState.FAILED,
    RequestScopeState.INTERRUPTED,
})


REQUEST_SCOPE_ID_KEY = "request_scope_id"
"""``InputMessage.metadata`` key carrying the submitting scope's id.

``run_input`` stamps it so the tree's deliver-time admission gate can bind
the prompt to its reservation; ordinary (tokenless) messages never carry it.
"""


def is_terminal(state: RequestScopeState) -> bool:
    return state in _TERMINAL_STATES


class RequestOutcome(StrEnum):
    """Terminal outcome of a request scope. Frozen before the waiter wakes."""

    FINISHED = "finished"
    """Root AgentResult available (``agent_result``)."""

    HANDLED = "handled"
    """A notice/command already emitted its own output; no root result."""

    CANCELLED = "cancelled"

    FAILED = "failed"

    INTERRUPTED = "interrupted"


class RequestScopeRecord(BaseModel):
    """Persisted attribution fact for the current/last request on a tree.

    Stored in the owning root session's SessionRegistry metadata under
    :attr:`SessionTreeMetadata.REQUEST_SCOPE` (same serialized ``register``
    path as BINDING/PAUSED — one write mechanism, no second store).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_id: str = Field(description="Unique id of this user request.")
    session_id: str = Field(description="Scope root session id (prompt target).")
    tree_id: str = Field(description="Owning session tree id.")
    state: RequestScopeState
    created_at: int = Field(description="Epoch-ms (ADR-0029).")
    updated_at: int = Field(description="Epoch-ms (ADR-0029).")
    pending_approval_turn_uuid: str | None = Field(
        default=None,
        description="Turn uuid of the suspended approval batch, when awaiting.",
    )
    outcome: RequestOutcome | None = Field(
        default=None,
        description="Terminal outcome. Frozen BEFORE the waiting waiter wakes.",
    )
    agent_result: AgentResult | None = Field(
        default=None,
        description="Final ROOT result. Persisted so restart keeps the fact.",
    )
    fail_reason: str | None = Field(default=None)

    def with_state(self, state: RequestScopeState, *, now_ms: int) -> RequestScopeRecord:
        return self.model_copy(update={"state": state, "updated_at": now_ms})


class RequestReservation(BaseModel):
    """Two-phase admission token returned by ``begin_request``.

    Carried through prepare (before any user-visible write) and consumed by
    ``submit_request``/``run_input``. Not persistable identity — the durable
    fact is the :class:`RequestScopeRecord`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_id: str
    session_id: str
    issued_at: int = Field(description="Epoch-ms (ADR-0029).")


class RequestResult(BaseModel):
    """Terminal read result for one request scope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_id: str
    outcome: RequestOutcome
    agent_result: AgentResult | None = None
    fail_reason: str | None = None


class RequestTurnFactKind(StrEnum):
    """Kind of a per-turn fact noted into the owning request scope."""

    FINISHED = "finished"
    SUSPENDED = "suspended"
    HANDLED = "handled"
    FAILED = "failed"


class DispatchDecision(StrEnum):
    """Poller admission decision for one envelope in a scoped tree."""

    DISPATCH = "dispatch"
    """Start the turn now."""

    HOLD = "hold"
    """Leave pending (not acknowledged) — e.g. the approval continuation
    of a suspended request goes first."""

    ARCHIVE = "archive"
    """Stale carrier of a closed/superseded request — acknowledge and drop,
    never start a turn."""


class RequestTurnFact(BaseModel):
    """One dispatch turn's fact, noted by the pool into the scope owner.

    The pool maps the pipeline's typed turn outcome (or a dispatch failure)
    onto this multi_agent-local record so the session-tree manager never
    imports pipeline modules. Only ROOT-session facts influence the scope.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RequestTurnFactKind
    agent_result: AgentResult | None = Field(
        default=None,
        description="Root result; present only for FINISHED facts.",
    )
    pending_approval_turn_uuid: str | None = Field(
        default=None,
        description="Turn uuid of the suspended approval batch; SUSPENDED only.",
    )
    fail_reason: str | None = Field(
        default=None,
        description="Failure description; FAILED only.",
    )


class RequestScopeError(Exception):
    """Base error for the request-scope contract."""


class RequestBusyError(RequestScopeError):
    """The session already has a live scope (second prompt / already open)."""


class ScopeRequiredError(RequestScopeError):
    """A tokenless prompt targeted a request-scoped session (even idle)."""


class ScopeMismatchError(RequestScopeError):
    """A causal message carries a conflicting scope, or a stale continuation."""


class ReservationLostError(RequestScopeError):
    """The reservation is gone (released, consumed, or superseded)."""
