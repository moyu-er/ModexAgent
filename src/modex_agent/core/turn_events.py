"""Provider-neutral semantic events emitted during an agent turn."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _TurnEventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TurnTextEvent(_TurnEventBase):
    kind: Literal["text"] = "text"
    text: str


class TurnReasoningEvent(_TurnEventBase):
    kind: Literal["reasoning"] = "reasoning"
    text: str


class TurnToolCallEvent(_TurnEventBase):
    kind: Literal["tool_call"] = "tool_call"
    tool_name: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    arguments: dict[str, JsonValue]


class TurnToolResultEvent(_TurnEventBase):
    kind: Literal["tool_result"] = "tool_result"
    tool_name: Annotated[str, Field(min_length=1)]
    call_id: Annotated[str, Field(min_length=1)]
    output: str


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
