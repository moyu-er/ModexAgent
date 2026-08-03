"""Channel primitives, ``GraphState``, state schema and factory.

Sub-package grouping the state/channel layer (ADR-0033 D4). Pydantic-first
dual-mode state access with per-field channel declarations and automated
checkpoint/restore. Zero outbound dependencies on other ``modex_graph``
sub-packages — this is the self-contained foundation for typed graph state.

Modules:

- ``channel`` — ``BaseChannel`` ABC + ``LastValue`` + ``ReducerChannel`` +
  ``Codec`` registry. Exactly two channel types ship (ADR-0033 D4); additional
  types deferred to Phase c per ADR-0007.
- ``state`` — ``GraphState(BaseModel)`` with ``Annotated[T, ChannelSpec]``
  per-field channel declaration; ``checkpoint()`` / ``from_checkpoint()``
  automate per-channel snapshot.
- ``state_schema`` — ``StateSchema`` / ``StateFieldSpec`` for declarative
  field spec validation.
- ``state_factory`` — ``StateFactory`` ABC + ``SimpleStateFactory`` /
  ``DynamicStateFactory`` / ``StateRegistry``.
"""

from __future__ import annotations

from .channel import (
    BaseChannel,
    Codec,
    JsonValue,
    LastValue,
    ReducerChannel,
    register_codec,
)
from .state import GraphState
from .state_factory import (
    DynamicStateFactory,
    SimpleStateFactory,
    StateFactory,
    StateRegistry,
)
from .state_schema import StateFieldSpec, StateSchema

__all__ = [
    "BaseChannel",
    "Codec",
    "JsonValue",
    "LastValue",
    "ReducerChannel",
    "register_codec",
    "GraphState",
    "DynamicStateFactory",
    "SimpleStateFactory",
    "StateFactory",
    "StateRegistry",
    "StateFieldSpec",
    "StateSchema",
]
