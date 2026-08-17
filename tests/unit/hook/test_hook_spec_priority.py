from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from modex_agent.hook.abc import BeforeGraphHook, HookErrorPolicy, HookPoint, HookSpec
from modex_agent.hook.runner import HookRunner

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext


class _RecordingHook(BeforeGraphHook):
    """BeforeGraphHook that appends its name to a shared log on execution."""

    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    @property
    def name(self) -> str:
        return self._name

    async def before_graph(self, ctx: AgentContext) -> None:
        self._log.append(self._name)


def _spec(name: str, log: list[str], *, priority: int = 0) -> HookSpec:
    return HookSpec(
        hook=_RecordingHook(name, log),
        on_error=HookErrorPolicy.LOG,
        priority=priority,
    )


def test_priority_defaults_to_zero() -> None:
    spec = HookSpec(hook=_RecordingHook("h", []), on_error=HookErrorPolicy.LOG)

    assert spec.priority == 0


def test_explicit_priority() -> None:
    spec = HookSpec(
        hook=_RecordingHook("h", []),
        on_error=HookErrorPolicy.LOG,
        priority=-1000,
    )

    assert spec.priority == -1000


async def test_dispatch_sorts_by_priority() -> None:
    log: list[str] = []
    runner = HookRunner([
        _spec("high", log, priority=10),
        _spec("low", log, priority=-10),
        _spec("mid", log, priority=0),
    ])

    await runner.dispatch(HookPoint.BEFORE_GRAPH, MagicMock())  # type: ignore[arg-type]

    assert log == ["low", "mid", "high"]


async def test_dispatch_stable_sort_preserves_registration_order() -> None:
    log: list[str] = []
    runner = HookRunner([
        _spec("first", log, priority=0),
        _spec("second", log, priority=0),
    ])

    await runner.dispatch(HookPoint.BEFORE_GRAPH, MagicMock())  # type: ignore[arg-type]

    assert log == ["first", "second"]
