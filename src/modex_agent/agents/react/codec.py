"""Channel codec registrations for ReAct state types.

Per ADR-0033 D14: register Pydantic ``model_dump(mode="json")`` /
``model_validate()`` as the universal channel codec for the 5 ReAct state
types migrated to ``BaseModel`` in Stage 1.

The codec is used by ``modex_graph``'s channel ``checkpoint()`` /
``restore()`` to round-trip non-primitive state field values through
``JsonValue``. Pydantic ``BaseModel`` subclasses are handled universally
by ``modex_graph.channel.encode_value`` / ``decode_value`` without
registration, but registering explicitly makes the intent visible and
ensures the codec is discoverable via ``_find_codec``.

Stage 1 status: registrations exist but are NOT referenced by the graph
engine yet (ReAct still uses the old ``core/graph/`` engine). Stage 2
wires ``ReActTurnState`` as a ``GraphState`` subclass, at which point
these codecs are exercised by ``state.checkpoint()`` /
``state.from_checkpoint()``.
"""

from __future__ import annotations

from pydantic import BaseModel

from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    ToolBatchState,
    ToolCallState,
)
from modex_graph.channel import Codec, register_codec


def _pydantic_codec(model_cls: type[BaseModel]) -> Codec:
    """Build a ``Codec`` that round-trips a Pydantic ``BaseModel`` through JSON."""
    return Codec(
        encode=lambda v: v.model_dump(mode="json"),
        decode=lambda d: model_cls.model_validate(d),
    )


register_codec(ApprovalTransaction, _pydantic_codec(ApprovalTransaction))
register_codec(ApprovalRequestState, _pydantic_codec(ApprovalRequestState))
register_codec(ToolBatchState, _pydantic_codec(ToolBatchState))
register_codec(ToolCallState, _pydantic_codec(ToolCallState))
register_codec(ToolArguments, _pydantic_codec(ToolArguments))


__all__ = ["_pydantic_codec"]
