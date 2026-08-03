"""ReAct business ``StateFactory`` — wraps ``ReActTurnState`` for ``StateRegistry``.

Per ticket 08: business ``StateFactory`` implementations live in ``modex_agent``
and are registered with a ``StateRegistry`` under a name so a ``GraphSpec`` with
``state_schema = REACT_STATE_FACTORY_NAME`` can reference them by name.

``ReactStateFactory`` is ``SimpleStateFactory`` bound to ``ReActTurnState`` —
the business name (and the ``REACT_STATE_FACTORY_NAME`` registration constant)
is the only addition over the generic implementation.
"""

from __future__ import annotations

from modex_graph.state_factory import SimpleStateFactory

from .state import ReActTurnState

REACT_STATE_FACTORY_NAME = "react_turn_state"


class ReactStateFactory(SimpleStateFactory):
    """Named business factory creating/restoring ``ReActTurnState`` instances.

    Inherits ``SimpleStateFactory``'s three methods unchanged:

    - ``create_state()`` — returns a fresh ``ReActTurnState()``.
    - ``state_schema()`` — introspects ``ReActTurnState``'s fields.
    - ``restore_state(data)`` — calls ``ReActTurnState.from_checkpoint(data)``.

    Register with a ``StateRegistry`` under ``REACT_STATE_FACTORY_NAME``.
    """

    def __init__(self) -> None:
        super().__init__(ReActTurnState)


__all__ = ["REACT_STATE_FACTORY_NAME", "ReactStateFactory"]
