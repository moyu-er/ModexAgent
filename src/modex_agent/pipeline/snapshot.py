"""Framework-level per-turn data snapshot base class.

The agent pipeline reads its per-turn stores (context manager, turn store,
trace store) from an injected snapshot so a workspace switch
mid-turn cannot corrupt the in-flight turn.  The concrete snapshot type lives
in business code (``bot.workspace.bundle.pool_data.PoolData``); the framework
must not import that type.  This base dataclass declares every field the
framework reads during a turn; business subclasses may add fields used at
wiring time.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.core.context import ContextManager
    from modex_agent.memory.pruned.manager import PrunedManager
    from modex_agent.runtime.approval_decision import ApprovalDecisionCoordinator
    from modex_agent.runtime.store import TurnStateStore
    from modex_agent.trace.otel_store import OtelSpanTraceStore


@dataclass(frozen=True)
class PoolDataSnapshot(ABC):
    """Base dataclass for the per-turn data snapshot the pipeline consumes.

    The concrete snapshot may carry experience / memory fields used by
    hooks and other consumers; ``experience_dir`` is declared here because
    ``ExperienceReviewHook`` may resolve it from
    ``AgentContext.workspace_snapshot`` at turn time (``None`` on the
    migrated tree — the experience capability supply owns the dir and the
    hook falls back to its factory-supplied one). Business-specific wiring
    fields live in subclasses.
    """

    context_manager: ContextManager
    turn_store: TurnStateStore
    trace_store: OtelSpanTraceStore | None
    memory_dir: Path | None
    runtime_dir: Path | None
    pruned_manager: PrunedManager | None
    experience_dir: Path | None = None
    """``None`` on the migrated tree: the experience capability supply is
    the experience dir's construction authority (SPEC §8.3) and
    ``ExperienceReviewHook`` falls back to its factory-supplied dir — the
    field stays for custom snapshots that carry a per-turn dir."""
    decision_coordinator: ApprovalDecisionCoordinator | None = field(
        default=None,
        kw_only=True,
    )
