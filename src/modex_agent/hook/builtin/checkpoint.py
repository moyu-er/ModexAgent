"""CheckpointHook — persist a turn snapshot after each ReAct iteration.

Repro Path B1 (default-on). An ``AfterIterationHook`` that captures a
``TurnSnapshot`` with ``SnapshotReason.ITERATION`` via the existing
``ReActSnapshotPolicy`` and persists it through ``TurnStateStore.save_turn()``.
Composition over inheritance: the hook HAS a ``ReActSnapshotPolicy`` and IS an
``AfterIterationHook`` — it does not subclass the policy.

A checkpoint failure is non-fatal: the hook logs a warning and returns so a
persistence hiccup never breaks the in-flight turn.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.agents.react.state import ReActSnapshotPolicy, get_react_state
from modex_agent.hook.abc import AfterIterationHook
from modex_agent.runtime.enums import SnapshotReason
from modex_agent.runtime.models import StateQueryScope, TurnIdentity, TurnSnapshot

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.runtime.store import TurnStateStore

logger = logging.getLogger(__name__)

_POLICY = ReActSnapshotPolicy()


class CheckpointHook(AfterIterationHook):
    """Persist one ``TurnSnapshot`` per ReAct iteration (``SnapshotReason.ITERATION``).

    Stateless: every ``after_iteration`` invocation reads the current
    ``ReActTurnState`` from ``ctx.runtime.state`` and the store from
    ``ctx.runtime.services.turn_store``, so pool-mode session reuse is safe.
    """

    @property
    def name(self) -> str:
        return "checkpoint"

    async def after_iteration(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        if state is None:
            return
        runtime = ctx.runtime
        if runtime is None:
            return
        store = runtime.services.turn_store
        if store is None:
            return
        snapshot = _POLICY.capture(state, SnapshotReason.ITERATION)
        try:
            await store.save_turn(snapshot)
        except Exception:
            logger.warning(
                "CheckpointHook failed to persist iteration %s snapshot for turn %s",
                state.iteration,
                state.identity.turn_id,
                exc_info=True,
            )


async def list_iteration_checkpoints(
    store: TurnStateStore, identity: TurnIdentity
) -> list[TurnSnapshot]:
    """Return iteration checkpoints for a turn, ordered by iteration number.

    Queries the store for ``SnapshotReason.ITERATION`` records scoped to the
    turn's agent + session, then keeps only those whose ``turn_id`` matches.
    The iteration is read back from the snapshot's ``state_payload`` for
    ordering.
    """
    scope = StateQueryScope(
        agent_id=identity.agent_id,
        session_id=str(identity.session),
        reason=SnapshotReason.ITERATION,
    )
    matches = await store.list_active_turns(scope)
    own = [s for s in matches if s.identity.turn_id == identity.turn_id]
    own.sort(key=_iteration_of)
    return own


def _iteration_of(snapshot: TurnSnapshot) -> int:
    raw = snapshot.state_payload.get("iteration")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0
