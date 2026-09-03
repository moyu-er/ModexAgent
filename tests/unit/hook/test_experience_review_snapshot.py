"""ExperienceReviewHook snapshot-based dir resolution.

When the AgentContext carries a ``workspace_snapshot`` with an
``experience_dir``, the hook must resolve that dir for the turn instead
of its catalog root. Without a snapshot it falls back to the catalog.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.history import ListMessageHistory
from modex_agent.plugins.defaults.capabilities.experience.catalog import ExperienceCatalog
from modex_agent.plugins.defaults.capabilities.experience.config import (
    ExperiencePoolConfig,
    ExperienceReviewConfig,
)
from modex_agent.plugins.defaults.capabilities.experience.metadata import (
    PerFileExperienceMetaStore,
)
from modex_agent.plugins.defaults.capabilities.experience.review_hook import (
    ExperienceReviewHook,
)
from modex_agent.plugins.defaults.capabilities.experience.supply import ExperienceSupply


class _Snapshot:
    def __init__(self, experience_dir: Path) -> None:
        self.experience_dir = experience_dir


def _make_ctx(snapshot: object | None) -> MagicMock:
    ctx = MagicMock()
    ctx.workspace_snapshot = snapshot
    return ctx


def _supply(exp_root: Path) -> ExperienceSupply:
    meta = PerFileExperienceMetaStore(exp_root)
    return ExperienceSupply(
        pool_name="p",
        catalog=ExperienceCatalog(experience_dir=exp_root, meta_store=meta),
        experience_dir=exp_root,
        meta_store=meta,
        pool_config=ExperiencePoolConfig(),
        review_config_by_agent={"main": ExperienceReviewConfig()},
        review_provider=MagicMock(),
    )


def _memory_system() -> MagicMock:
    memory_system = MagicMock(spec=MemorySystem)
    memory_system.get_full_history = AsyncMock(return_value=[MagicMock()])
    return memory_system


def test_resolve_dir_prefers_snapshot_over_fallback(tmp_path: Path) -> None:
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()

    hook = ExperienceReviewHook(
        agent_name="main",
        supply=_supply(fallback_dir),
        memory_system=_memory_system(),
        catalog=_supply(fallback_dir).catalog,
    )
    ctx = _make_ctx(_Snapshot(snapshot_dir))

    assert hook._resolve_dir(ctx) == snapshot_dir  # noqa: SLF001
    assert hook._resolve_dir(ctx) != fallback_dir  # noqa: SLF001


def test_resolve_dir_falls_back_when_no_snapshot(tmp_path: Path) -> None:
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()

    supply = _supply(fallback_dir)
    hook = ExperienceReviewHook(
        agent_name="main",
        supply=supply,
        memory_system=_memory_system(),
        catalog=supply.catalog,
    )
    ctx = _make_ctx(None)

    assert hook._resolve_dir(ctx) == fallback_dir  # noqa: SLF001


async def test_after_graph_uses_snapshot_dir_for_review(tmp_path: Path) -> None:
    """End-to-end: after_graph triggers a review whose experience_dir is the
    snapshot dir, even though the catalog's root is different."""
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()

    supply = _supply(fallback_dir)
    agent = MagicMock()
    agent.review = AsyncMock()
    supply.register_review_agent("main", agent)
    hook = ExperienceReviewHook(
        agent_name="main",
        supply=supply,
        memory_system=_memory_system(),
        catalog=supply.catalog,
        min_messages=2,
        exp_cooldown_turns=0,
    )

    ctx = MagicMock()
    ctx.session = SessionInfo.from_str("snapshot-review.main")
    ctx.workspace_snapshot = _Snapshot(snapshot_dir)
    ctx.history = ListMessageHistory([{"role": "user", "content": "hi"}] * 3)
    result = MagicMock(
        stop_reason="completed",
        messages=[{"role": "assistant", "content": "response"}],
    )

    await supply.start()
    await hook.after_graph(ctx, result)
    for _ in range(100):
        if agent.review.called:
            break
        await asyncio.sleep(0.01)
    await supply.stop()

    assert agent.review.called
    _args, kwargs = agent.review.call_args
    assert kwargs["experience_dir"] == snapshot_dir
