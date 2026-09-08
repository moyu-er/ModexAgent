"""Tests for ExperienceReviewHook — lifecycle + filesystem outcomes (§18.7).

The retired private-state pins (``_pending``, ``_turn_counter``,
``_last_exp_tool_turn``) are replaced by observable outcomes: reviews
submitted to the supply, cooldown behavior through repeated after_graph
calls, and post-review cleanup effects on disk.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.message import ChatMessage, MessageRole
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


def _supply(tmp_path: Path) -> ExperienceSupply:
    exp_dir = tmp_path / "experiences"
    exp_dir.mkdir(exist_ok=True)
    meta = PerFileExperienceMetaStore(exp_dir)
    return ExperienceSupply(
        pool_name="p",
        catalog=ExperienceCatalog(experience_dir=exp_dir, meta_store=meta),
        experience_dir=exp_dir,
        meta_store=meta,
        pool_config=ExperiencePoolConfig(),
        review_config_by_agent={"main": ExperienceReviewConfig()},
        review_provider=MagicMock(),
    )


def _hook(
    tmp_path: Path,
    supply: ExperienceSupply,
    review_agent: MagicMock,
    memory_system: MagicMock,
    *,
    min_messages: int = 6,
    exp_cooldown_turns: int = 3,
) -> ExperienceReviewHook:
    return ExperienceReviewHook(
        agent_name="main",
        supply=supply,
        memory_system=memory_system,
        catalog=supply.catalog,
        min_messages=min_messages,
        exp_cooldown_turns=exp_cooldown_turns,
    )


def _memory_system(messages: list[ChatMessage] | None = None) -> MagicMock:
    memory_system = MagicMock(spec=MemorySystem)
    if messages is None:
        messages = [ChatMessage(role=MessageRole.USER, content="full history")] * 6
    memory_system.get_full_history = AsyncMock(return_value=messages)
    return memory_system


def _ctx(history_messages: list[dict[str, object]], session: str = "review.main"):
    ctx = MagicMock()
    ctx.history = ListMessageHistory(history_messages)  # type: ignore[arg-type]
    ctx.session = SessionInfo.from_str(session)
    ctx.workspace_snapshot = None
    return ctx


def _result(
    stop_reason: str = "completed",
    messages: list[dict[str, object]] | None = None,
) -> MagicMock:
    return MagicMock(stop_reason=stop_reason, messages=messages or [])


@pytest.fixture
def review_agent() -> MagicMock:
    agent = MagicMock()
    agent.review = AsyncMock(return_value=True)
    return agent


@pytest.fixture
def supply(tmp_path: Path) -> ExperienceSupply:
    return _supply(tmp_path)


@pytest.fixture
def hook(
    tmp_path: Path,
    supply: ExperienceSupply,
    review_agent: MagicMock,
) -> ExperienceReviewHook:
    supply.register_review_agent("main", review_agent)
    return _hook(tmp_path, supply, review_agent, _memory_system())


async def _drain(supply: ExperienceSupply) -> None:
    for _ in range(100):
        if not supply.review_in_flight("main"):
            return
        await asyncio.sleep(0.01)


class TestTriggerGates:
    async def test_skips_when_not_plain_completion(
        self, hook: ExperienceReviewHook, review_agent: MagicMock
    ) -> None:
        ctx = _ctx([{"role": "user", "content": "hi"}] * 6)
        await hook.after_graph(ctx, _result(stop_reason="max_iterations"))
        await _drain(hook._supply)  # noqa: SLF001
        assert not review_agent.review.called

    async def test_skips_when_insufficient_messages(
        self, hook: ExperienceReviewHook, review_agent: MagicMock
    ) -> None:
        ctx = _ctx([{"role": "user", "content": "hi"}] * 3)
        await hook.after_graph(ctx, _result())
        await _drain(hook._supply)  # noqa: SLF001
        assert not review_agent.review.called

    async def test_triggers_on_plain_turn_with_enough_messages(
        self,
        tmp_path: Path,
        supply: ExperienceSupply,
        review_agent: MagicMock,
    ) -> None:
        supply.register_review_agent("main", review_agent)
        hook = _hook(tmp_path, supply, review_agent, _memory_system())
        ctx = _ctx([{"role": "user", "content": "hello"}] * 6)
        result = _result(messages=[{"role": "assistant", "content": "response"}])
        await supply.start()
        await hook.after_graph(ctx, result)
        await _drain(supply)
        await supply.stop()
        assert review_agent.review.called

    async def test_skips_when_full_history_is_empty(
        self,
        tmp_path: Path,
        supply: ExperienceSupply,
        review_agent: MagicMock,
    ) -> None:
        memory_system = _memory_system(messages=[])
        supply.register_review_agent("main", review_agent)
        hook = _hook(tmp_path, supply, review_agent, memory_system)
        ctx = _ctx([{"role": "user", "content": "hello"}] * 6)
        await hook.after_graph(ctx, _result())
        await _drain(supply)
        assert not review_agent.review.called


class TestCooldownBehavior:
    async def test_exp_tool_usage_sets_cooldown_via_turns(
        self,
        tmp_path: Path,
        supply: ExperienceSupply,
        review_agent: MagicMock,
    ) -> None:
        """After an experience write turn, the doubled threshold gates the
        next turns until the cooldown window passes (observable: review not
        called during cooldown, called after)."""
        supply.register_review_agent("main", review_agent)
        hook = _hook(tmp_path, supply, review_agent, _memory_system())
        write_result = _result(
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"function": {"name": "experience_write", "arguments": ""}}
                    ],
                }
            ]
        )
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 12), write_result)

        # Turn 2 (still in cooldown): 8 messages < doubled threshold (12)
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 8), _result())
        await _drain(supply)
        assert not review_agent.review.called

        # Turns 3-5: cooldown window (turns_since 2, 3) still doubles the
        # threshold; the turn after expiry (turns_since 4 > 3) passes at
        # the normal threshold.
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 6), _result())
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 6), _result())
        await _drain(supply)
        assert not review_agent.review.called

        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 6), _result())
        await _drain(supply)
        assert review_agent.review.called

    async def test_unified_experience_tool_write_action_sets_cooldown(
        self,
        tmp_path: Path,
        supply: ExperienceSupply,
        review_agent: MagicMock,
    ) -> None:
        supply.register_review_agent("main", review_agent)
        hook = _hook(tmp_path, supply, review_agent, _memory_system())
        result = _result(
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "function": {
                                "name": "experience",
                                "arguments": '{"action": "write"}',
                            }
                        }
                    ],
                }
            ]
        )
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 12), result)
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 8), _result())
        await _drain(supply)
        assert not review_agent.review.called

    async def test_cooldown_is_isolated_by_session(
        self,
        tmp_path: Path,
        supply: ExperienceSupply,
        review_agent: MagicMock,
    ) -> None:
        supply.register_review_agent("main", review_agent)
        hook = _hook(tmp_path, supply, review_agent, _memory_system())
        write_result = _result(
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"function": {"name": "experience_write", "arguments": ""}}
                    ],
                }
            ]
        )
        await supply.start()

        await hook.after_graph(
            _ctx([{"role": "user", "content": "hi"}] * 12, session="a.main"),
            write_result,
        )
        await hook.after_graph(
            _ctx([{"role": "user", "content": "hi"}] * 6, session="b.main"),
            _result(),
        )
        await _drain(supply)
        await supply.stop()

        assert review_agent.review.call_count == 1


class TestMutexGate:
    async def test_skips_when_review_in_flight(
        self,
        tmp_path: Path,
        supply: ExperienceSupply,
        review_agent: MagicMock,
    ) -> None:
        supply.register_review_agent("main", review_agent)
        hook = _hook(tmp_path, supply, review_agent, _memory_system())

        gate = asyncio.Event()

        async def slow_review(**_kwargs: object) -> bool:
            await gate.wait()
            return True

        review_agent.review = AsyncMock(side_effect=slow_review)
        await supply.start()
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 6), _result())
        await asyncio.sleep(0.05)  # first submission now in flight
        assert supply.review_in_flight("main")

        calls_before = review_agent.review.call_count
        await hook.after_graph(_ctx([{"role": "user", "content": "hi"}] * 6), _result())
        assert review_agent.review.call_count == calls_before

        gate.set()
        await supply.stop()


class TestFailSoftMissingReviewer:
    async def test_no_registered_reviewer_skips_with_warning(
        self,
        tmp_path: Path,
        supply: ExperienceSupply,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """§10.6: no review LLM → review skipped with a warning; nothing
        raises; the turn is unaffected."""
        hook = _hook(tmp_path, supply, MagicMock(), _memory_system())  # no reviewer registered
        await supply.start()
        await hook.after_graph(
            _ctx([{"role": "user", "content": "hi"}] * 6),
            _result(messages=[{"role": "assistant", "content": "ok"}]),
        )
        await _drain(supply)
        await supply.stop()
        assert any("no review provider" in r.message.lower() for r in caplog.records)


class TestScanAndCleanup:
    def test_scan_skips_hidden_and_non_experience_dirs(
        self, tmp_path: Path, supply: ExperienceSupply
    ) -> None:
        hook = _hook(tmp_path, supply, MagicMock(), _memory_system())
        for name in ("exp-a", "exp-b", ".archive", "traces"):
            (tmp_path / "experiences" / name).mkdir(parents=True, exist_ok=True)
        for name in ("exp-a", "exp-b"):
            (tmp_path / "experiences" / name / "EXPERIENCE.md").write_text(
                f"---\nname: {name}\ndescription: x\n---\n\nBody"
            )
        scanned = hook._scan_experience_dir(tmp_path / "experiences")  # noqa: SLF001
        assert set(scanned) == {"exp-a", "exp-b"}

    async def test_cleanup_removes_invalid_experience(
        self, tmp_path: Path, supply: ExperienceSupply
    ) -> None:
        hook = _hook(tmp_path, supply, MagicMock(), _memory_system())
        exp_dir = tmp_path / "experiences" / "bad-exp"
        exp_dir.mkdir(parents=True)
        exp_md = exp_dir / "EXPERIENCE.md"
        exp_md.write_text("# No frontmatter here")

        await hook._cleanup({}, {"bad-exp": exp_md.stat().st_mtime}, tmp_path / "experiences")  # noqa: SLF001

        assert not exp_md.exists()
        assert not exp_dir.exists()
        assert supply.meta_store.get("bad-exp") is None

    async def test_cleanup_fixes_frontmatter_name_mismatch(
        self, tmp_path: Path, supply: ExperienceSupply
    ) -> None:
        hook = _hook(tmp_path, supply, MagicMock(), _memory_system())
        exp_dir = tmp_path / "experiences" / "old-name"
        exp_dir.mkdir(parents=True)
        exp_md = exp_dir / "EXPERIENCE.md"
        exp_md.write_text("---\nname: new-name\ndescription: test\n---\n\nBody content here.\n")

        await hook._cleanup({}, {"old-name": exp_md.stat().st_mtime}, tmp_path / "experiences")  # noqa: SLF001

        assert exp_dir.exists()
        assert "name: old-name" in exp_md.read_text(encoding="utf-8")


class TestSnapshotHelpers:
    def test_capture_snapshot_round_trip(self, supply: ExperienceSupply) -> None:
        hook = _hook(Path("."), supply, MagicMock(), _memory_system())
        snapshot = hook._capture_snapshot(  # noqa: SLF001
            [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi!"}]
        )
        assert "[user]: Hello" in snapshot
        assert "[assistant]: Hi!" in snapshot
