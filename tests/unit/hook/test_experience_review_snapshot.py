"""Tests for ExperienceReviewHook snapshot-based dir resolution.

Unit C: when the AgentContext carries a ``workspace_snapshot`` with an
``experience_dir``, the hook must resolve that dir for the turn instead
of its configured fallback (``_get_dir``). Without a snapshot it falls
back to the configured dir.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.experience.meta import PerFileExperienceMetaStore
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import MessageRole
from modex_agent.hook.builtin.experience_review import ExperienceReviewHook
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.history import ListMessageHistory


@pytest.fixture
def meta_store(tmp_path: Path) -> PerFileExperienceMetaStore:
    return PerFileExperienceMetaStore(tmp_path)


def _make_ctx(snapshot: object | None) -> MagicMock:
    """Build a minimal AgentContext-like object for _resolve_dir."""
    ctx = MagicMock()
    ctx.workspace_snapshot = snapshot
    return ctx


class _Snapshot:
    def __init__(self, experience_dir: Path) -> None:
        self.experience_dir = experience_dir


def _memory_system() -> MagicMock:
    memory_system = MagicMock(spec=MemorySystem)
    memory_system.get_full_history = AsyncMock(
        return_value=[ChatMessage(role=MessageRole.USER, content="full history")]
    )
    return memory_system


def test_resolve_dir_prefers_snapshot_over_fallback(
    tmp_path: Path, meta_store: PerFileExperienceMetaStore
):
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()

    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        memory_system=_memory_system(),
        experience_dir=fallback_dir,
        meta_store=meta_store,
    )
    ctx = _make_ctx(_Snapshot(snapshot_dir))

    assert hook._resolve_dir(ctx) == snapshot_dir
    assert hook._resolve_dir(ctx) != fallback_dir


def test_resolve_dir_falls_back_when_no_snapshot(
    tmp_path: Path, meta_store: PerFileExperienceMetaStore
):
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()

    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        memory_system=_memory_system(),
        experience_dir=fallback_dir,
        meta_store=meta_store,
    )
    ctx = _make_ctx(None)

    assert hook._resolve_dir(ctx) == fallback_dir


@pytest.mark.asyncio
async def test_after_turn_uses_snapshot_dir_for_review(
    tmp_path: Path, meta_store: PerFileExperienceMetaStore
):
    """End-to-end: after_turn triggers a review whose experience_dir is the
    snapshot dir, even though the hook's configured fallback is different."""
    fallback_dir = tmp_path / "fallback"
    fallback_dir.mkdir()
    snapshot_dir = tmp_path / "snap"
    snapshot_dir.mkdir()

    agent = MagicMock()
    agent.review = AsyncMock()
    hook = ExperienceReviewHook(
        review_agent=agent,
        memory_system=_memory_system(),
        experience_dir=fallback_dir,
        meta_store=meta_store,
        min_messages=2,
        exp_cooldown_turns=0,
    )

    ctx = MagicMock()
    ctx.session = SessionInfo.from_str("snapshot-review.main")
    ctx.workspace_snapshot = _Snapshot(snapshot_dir)
    ctx.history = ListMessageHistory(
        [{"role": "user", "content": "hi"}] * 3
    )
    result = MagicMock(
        stop_reason="completed",
        messages=[{"role": "assistant", "content": "response"}],
    )

    await hook.after_turn(ctx, result)
    # Let the background review task run to completion, then drain it.
    import asyncio

    await asyncio.sleep(0.1)
    await hook.cancel_pending()

    assert agent.review.called
    _args, kwargs = agent.review.call_args
    assert kwargs["experience_dir"] == snapshot_dir
