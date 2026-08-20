"""FINALLY_GRAPH suspend-leg contract: ``result=None`` must mean "not a turn end".

The GraphInterrupt approval-suspend path dispatches FINALLY_GRAPH with
``result=None`` and re-enters ``actual_turn()`` on resume. The contract:

- Outcome-dependent hooks (notifications, deliveries, trace tags — the
  ``OutcomeFinallyHook`` template-method family) must be silent on the
  suspend leg. A violation here is exactly the duplicated subagent-result
  bug: one logical turn delivering two envelopes with different
  ``message_id``s, which the inbox's message-id dedup cannot collapse.
- Cleanup hooks (``CassetteFlushHook``) are exempt — flushing the suspend
  segment's recordings is correct and idempotent.

Every concrete ``FinallyGraphHook`` subclass in ``src/modex_agent`` must
appear in one of the two classification sets below; a new hook that is
missing from both fails ``test_all_finally_hooks_are_classified``.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
from pathlib import Path

import pytest

import modex_agent
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook.abc import FinallyGraphHook, OutcomeFinallyHook
from modex_agent.memory.history import ListMessageHistory

_SRC_ROOT = Path(modex_agent.__file__).parent

#: Outcome-dependent hooks — must skip the suspend leg.
OUTCOME_HOOKS: frozenset[str] = frozenset(
    {"SubagentAutoSendHook", "TurnOutcomeNotifyHook", "TrainingDataHook"}
)

#: Cleanup hooks — suspend-leg side effects are correct (idempotent flush).
CLEANUP_HOOKS: frozenset[str] = frozenset(
    {"CassetteFlushHook", "RootSpanHook"}
)
# RootSpanHook is cleanup-exempt for the *tag* it emits but still must not
# emit a root span at suspend; its suspend skip is asserted directly via
# ``is_suspend_leg`` below (it cannot inherit the template method because
# it also handles the error-carrying dispatch shape).


def _concrete_finally_hook_classes() -> dict[str, type[FinallyGraphHook]]:
    """Import every module under ``modex_agent`` and collect concrete
    ``FinallyGraphHook`` subclasses defined in ``src`` (name → class)."""
    found: dict[str, type[FinallyGraphHook]] = {}
    for module_info in pkgutil.walk_packages(
        modex_agent.__path__, prefix="modex_agent."
    ):
        try:
            module = importlib.import_module(module_info.name)
        except Exception:  # noqa: BLE001 — optional extras may not import
            continue
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, FinallyGraphHook)
                and obj not in (FinallyGraphHook, OutcomeFinallyHook)
                and obj.__module__.startswith("modex_agent")
            ):
                found[obj.__name__] = obj
    return found


def _make_context() -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=SessionInfo.from_str("pfx.agent"),
    )


def test_all_finally_hooks_are_classified() -> None:
    """Every concrete FinallyGraphHook must appear in OUTCOME_HOOKS or
    CLEANUP_HOOKS — a new hook fails here until its suspend behaviour is
    chosen explicitly."""
    found = _concrete_finally_hook_classes()
    classified = OUTCOME_HOOKS | CLEANUP_HOOKS
    missing = set(found) - classified
    assert not missing, (
        f"Unclassified FinallyGraphHook subclass(es): {sorted(missing)}. "
        "Add to OUTCOME_HOOKS (must skip the suspend leg) or CLEANUP_HOOKS "
        "(suspend side effects correct) in this contract test."
    )
    stale = classified - set(found)
    assert not stale, (
        f"Classified but no longer existing: {sorted(stale)} — update the sets."
    )


@pytest.mark.parametrize("hook_name", sorted(OUTCOME_HOOKS))
async def test_outcome_hook_is_silent_on_suspend_leg(hook_name: str) -> None:
    """Outcome hooks must not run user-visible logic when result=None.

    ``finally_graph(ctx, None)`` must return without raising and without
    reaching ``on_outcome`` (guaranteed structurally by
    ``OutcomeFinallyHook``; this pins it against regressions). Instances are
    built via ``object.__new__`` to bypass runtime deps — the template
    method must return before touching any instance state.
    """
    from modex_agent.hook.builtin.subagent_auto_send import SubagentAutoSendHook
    from modex_agent.hook.builtin.training_data import TrainingDataHook
    from modex_agent.hook.notification import TurnOutcomeNotifyHook

    hook_cls: type[OutcomeFinallyHook] = {
        "SubagentAutoSendHook": SubagentAutoSendHook,
        "TurnOutcomeNotifyHook": TurnOutcomeNotifyHook,
        "TrainingDataHook": TrainingDataHook,
    }[hook_name]

    hook = object.__new__(hook_cls)  # noqa: PLC2801
    ctx = _make_context()
    await hook.finally_graph(ctx, None)  # must be a silent no-op


def test_is_suspend_leg_predicate() -> None:
    from modex_agent.hook.abc import is_suspend_leg

    assert is_suspend_leg(None) is True
    assert is_suspend_leg(None, error=RuntimeError("boom")) is False
    assert is_suspend_leg(AgentResult(stop_reason="completed")) is False
    assert is_suspend_leg(AgentResult(stop_reason="error"), RuntimeError()) is False


async def test_root_span_hook_skips_suspend_leg() -> None:
    """RootSpanHook cannot use the template method (error-carrying dispatch)
    but must still be silent at suspend — pinned via the shared predicate."""
    from unittest.mock import AsyncMock, MagicMock

    from modex_agent.trace.root_span_hook import RootSpanHook

    hook = object.__new__(RootSpanHook)  # noqa: PLC2801 — bypass ctor deps
    hook._store = MagicMock()
    hook._store.save_span = AsyncMock()
    hook._score_injector = None
    hook._session = MagicMock()

    ctx = _make_context()
    ctx.runtime = MagicMock()
    await hook.finally_graph(ctx, result=None)

    hook._store.save_span.assert_not_awaited()


async def test_outcome_finally_hook_template_dispatch() -> None:
    """The template method itself: None → skip, concrete result → on_outcome."""

    class _Probe(OutcomeFinallyHook):
        def __init__(self) -> None:
            self.seen: list[AgentResult | None] = []

        async def on_outcome(self, ctx: AgentContext, result: AgentResult) -> None:
            self.seen.append(result)

    probe = _Probe()
    ctx = _make_context()
    await probe.finally_graph(ctx, None)
    assert probe.seen == []
    result = AgentResult(stop_reason="completed")
    await probe.finally_graph(ctx, result)
    assert probe.seen == [result]


# Keep the async loop policy happy for pytest-asyncio auto mode.
_ = asyncio
