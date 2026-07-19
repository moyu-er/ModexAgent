"""Unit tests for the TurnRunner ABC (ADR-0025 D3, Ticket 1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from modex_agent.pipeline.turn_runner_abc import TurnRunner

if TYPE_CHECKING:
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.types import InputMessage
    from modex_agent.multi_agent.router import RouteResult


def test_turn_runner_abc_cannot_be_instantiated() -> None:
    """The ABC itself cannot be instantiated (abstract method present)."""
    with pytest.raises(TypeError):
        TurnRunner()  # type: ignore[abstract]


class _ConcreteRunner(TurnRunner):
    """Minimal subclass implementing process_locked for instantiation tests."""

    async def process_locked(
        self,
        input_msg: InputMessage,
        session_id: str,
        route_result: RouteResult | None = None,
        *,
        session: SessionInfo,
    ) -> AgentResult | None:
        return None


def test_concrete_subclass_can_be_instantiated() -> None:
    """A subclass implementing process_locked can be instantiated."""
    runner = _ConcreteRunner()
    assert isinstance(runner, TurnRunner)
