"""Provider-neutral semantic events emitted during an agent turn."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _TurnEventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TurnTextEvent(_TurnEventBase):
    kind: Literal["text"] = "text"
    text: str
    part_id: str | None = None


class TurnReasoningEvent(_TurnEventBase):
    kind: Literal["reasoning"] = "reasoning"
    text: str
    part_id: str | None = None


class TurnToolCallEvent(_TurnEventBase):
    kind: Literal["tool_call"] = "tool_call"
    tool_name: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    arguments: dict[str, JsonValue]
    part_id: str | None = None


class TurnToolResultEvent(_TurnEventBase):
    """A tool call completed (ADR-0053 presentation seam enrichment).

    ``error`` / ``seq`` carry the tool-error fact and the runtime's
    ordering hint; ``arguments`` carries the originating call's arguments
    when the producer knows them without a preceding
    ``TurnToolCallEvent`` (e.g. a resumed approval turn re-emits only the
    END). All three are optional — older producers keep constructing the
    event with ``output`` alone.
    """

    kind: Literal["tool_result"] = "tool_result"
    tool_name: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    output: str
    error: str | None = None
    seq: int | None = None
    arguments: dict[str, JsonValue] | None = None
    part_id: str | None = None


TurnEvent = Annotated[
    TurnTextEvent | TurnReasoningEvent | TurnToolCallEvent | TurnToolResultEvent,
    Field(discriminator="kind"),
]


__all__ = [
    "TurnEvent",
    "TurnReasoningEvent",
    "TurnTextEvent",
    "TurnToolCallEvent",
    "TurnToolResultEvent",
]
