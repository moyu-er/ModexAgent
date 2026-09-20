"""Internal turn outcome — the single producer of turn results.

DESIGN.md §6.3 (acp-adapter): the internal unified result is
``Finished(AgentResult)``, ``Suspended`` (real suspension identity), or
``Handled``. ``AgentPipeline.process_message`` keeps its public
``AgentResult | None`` contract by MAPPING this same fact — it never
re-derives the outcome independently. Pool dispatch
(``AgentPool.dispatch_envelope``) consumes the typed fact so the
request-scope owner gets precise terminal/suspension information.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from modex_agent.approval.views import ApprovalRequestView
from modex_agent.core.emitter import AgentResult


class TurnOutcomeKind(StrEnum):
    """Closed set of internal turn outcomes (DESIGN.md §6.3)."""

    FINISHED = "finished"
    """A real ``AgentResult`` was produced by the turn."""

    SUSPENDED = "suspended"
    """The turn suspended on an approval batch (snapshot persisted)."""

    HANDLED = "handled"
    """A notice/command/duplicate path already consumed the input; no turn
    result exists and nothing is pending on this input."""


class TurnSuspension(BaseModel):
    """Real suspension identity for one approval suspension."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    turn_uuid: str | None = Field(
        description="Turn uuid minted for the suspended turn, when known.",
    )
    requests: list[ApprovalRequestView] = Field(
        description="The pending approval batch views, owner-serialized.",
    )

    @property
    def has_pending_requests(self) -> bool:
        return bool(self.requests)


class TurnOutcome(BaseModel):
    """Typed result of processing one input message.

    Kind/field combination is validated: FINISHED always carries a result,
    SUSPENDED always carries a real suspension identity, HANDLED carries
    neither — a ``finished`` outcome can never masquerade as handled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    kind: TurnOutcomeKind
    result: AgentResult | None = Field(
        default=None,
        description="Root result; present only for FINISHED.",
    )
    suspension: TurnSuspension | None = Field(
        default=None,
        description="Suspension identity; present only for SUSPENDED.",
    )

    @model_validator(mode="after")
    def _kind_matches_payload(self) -> TurnOutcome:
        if self.kind is TurnOutcomeKind.FINISHED:
            if self.result is None:
                raise ValueError("FINISHED outcome requires a result")
            if self.suspension is not None:
                raise ValueError("FINISHED outcome cannot carry a suspension")
        if self.kind is TurnOutcomeKind.SUSPENDED:
            if self.suspension is None:
                raise ValueError("SUSPENDED outcome requires a suspension identity")
            if self.result is not None:
                raise ValueError("SUSPENDED outcome cannot carry a result")
        if self.kind is TurnOutcomeKind.HANDLED and (
            self.result is not None or self.suspension is not None
        ):
            raise ValueError("HANDLED outcome carries neither result nor suspension")
        return self

    @staticmethod
    def finished(result: AgentResult) -> TurnOutcome:
        return TurnOutcome(kind=TurnOutcomeKind.FINISHED, result=result)

    @staticmethod
    def suspended(suspension: TurnSuspension) -> TurnOutcome:
        return TurnOutcome(kind=TurnOutcomeKind.SUSPENDED, suspension=suspension)

    @staticmethod
    def handled() -> TurnOutcome:
        return TurnOutcome(kind=TurnOutcomeKind.HANDLED)
