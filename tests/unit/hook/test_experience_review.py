"""Tests for ExperienceReviewHook."""
import asyncio
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


def _memory_system() -> MagicMock:
    memory_system = MagicMock(spec=MemorySystem)
    memory_system.get_full_history = AsyncMock(
        return_value=[ChatMessage(role=MessageRole.USER, content="full history")] * 6
    )
    return memory_system


@pytest.fixture
def memory_system() -> MagicMock:
    return _memory_system()


@pytest.fixture
def hook(
    tmp_path: Path,
    meta_store: PerFileExperienceMetaStore,
    memory_system: MagicMock,
) -> ExperienceReviewHook:
    agent = MagicMock()
    agent.review = AsyncMock(return_value=True)
    return ExperienceReviewHook(
        review_agent=agent,
        memory_system=memory_system,
        experience_dir=tmp_path,
        meta_store=meta_store,
        min_messages=6,
        exp_cooldown_turns=3,
    )


@pytest.mark.asyncio
async def test_hook_skips_when_not_plain_completion(hook: ExperienceReviewHook):
    """Review should not trigger if stop_reason is not 'completed'."""
    ctx = MagicMock()
    ctx.history = ListMessageHistory([{"role": "user", "content": "hi"}] * 6)
    result = MagicMock(stop_reason="max_iterations", messages=[])

    await hook.after_turn(ctx, result)
    assert not hook._agent.review.called  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_hook_skips_when_insufficient_messages(hook: ExperienceReviewHook):
    """Review should not trigger if history has fewer than min_messages."""
    ctx = MagicMock()
    ctx.history = ListMessageHistory([{"role": "user", "content": "hi"}] * 3)
    result = MagicMock(stop_reason="completed", messages=[])

    await hook.after_turn(ctx, result)
    assert not hook._agent.review.called  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_hook_triggers_on_plain_turn_with_enough_messages(
    hook: ExperienceReviewHook,
):
    """Review should trigger on a plain assistant turn with sufficient history."""
    ctx = MagicMock()
    ctx.history = ListMessageHistory([{"role": "user", "content": "hello"}] * 6)
    result = MagicMock(
        stop_reason="completed",
        messages=[{"role": "assistant", "content": "response"}],
    )
    await hook.after_turn(ctx, result)
    # Give the background task a moment
    await asyncio.sleep(0.05)
    assert hook._agent.review.called  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_hook_skips_when_full_history_is_empty(
    hook: ExperienceReviewHook,
    memory_system: MagicMock,
) -> None:
    memory_system.get_full_history = AsyncMock(return_value=[])
    ctx = MagicMock()
    ctx.session = SessionInfo.from_str("empty-review.main")
    ctx.history = ListMessageHistory([{"role": "user", "content": "hello"}] * 6)
    result = MagicMock(
        stop_reason="completed",
        messages=[{"role": "assistant", "content": "response"}],
    )

    await hook.after_turn(ctx, result)
    await asyncio.sleep(0)

    assert not hook._agent.review.called  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_hook_skips_during_cooldown_with_enough_messages(
    hook: ExperienceReviewHook,
):
    """During cooldown, threshold is doubled; need 12 messages instead of 6."""
    ctx = MagicMock()
    # 8 messages < doubled threshold of 12
    ctx.history = ListMessageHistory([{"role": "user", "content": "hi"}] * 8)
    result = MagicMock(
        stop_reason="completed",
        messages=[{"role": "assistant", "content": "response"}],
    )

    # Simulate exp tool usage at turn 1
    hook._turn_counter = 1
    hook._last_exp_tool_turn = 1

    # Next turn (turn 2): still in cooldown (3 turns), threshold doubled to 12
    await hook.after_turn(ctx, result)
    assert not hook._agent.review.called  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_cooldown_expires_after_enough_turns(
    hook: ExperienceReviewHook,
):
    """After cooldown expires, normal threshold applies."""
    ctx = MagicMock()
    ctx.history = ListMessageHistory([{"role": "user", "content": "hi"}] * 6)
    result = MagicMock(
        stop_reason="completed",
        messages=[{"role": "assistant", "content": "response"}],
    )

    # Exp tool used at turn 1, cooldown=3
    # Turn 5: turns_since = 5-1 = 4 > 3, cooldown expired
    hook._turn_counter = 4
    hook._last_exp_tool_turn = 1

    await hook.after_turn(ctx, result)
    await asyncio.sleep(0.05)
    assert hook._agent.review.called  # type: ignore[reportAttributeAccessIssue]


@pytest.mark.asyncio
async def test_exp_tool_usage_sets_cooldown(
    hook: ExperienceReviewHook,
):
    """Using experience_write this turn should set cooldown."""
    ctx = MagicMock()
    ctx.history = ListMessageHistory([{"role": "user", "content": "hi"}] * 12)
    result = MagicMock(
        stop_reason="completed",
        messages=[{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "function": {"name": "experience_write", "arguments": ""},
            }],
        }],
    )

    hook._turn_counter = 5
    hook._last_exp_tool_turn = 0

    await hook.after_turn(ctx, result)
    assert not hook._agent.review.called  # type: ignore[reportAttributeAccessIssue]
    assert hook._last_exp_tool_turn == 6  # incremented in after_turn then set in _should_review


@pytest.mark.asyncio
async def test_hook_skips_when_mutex_busy(hook: ExperienceReviewHook):
    hook._pending.add(asyncio.create_task(asyncio.sleep(0.001)))
    ctx = MagicMock()
    result = MagicMock(stop_reason="completed", messages=[])
    await hook.after_turn(ctx, result)
    # Mutex busy — review should NOT be called


def test_capture_snapshot_empty(hook: ExperienceReviewHook):
    snapshot = hook._capture_snapshot([])
    assert snapshot == ""


def test_capture_snapshot_with_messages(hook: ExperienceReviewHook):
    snapshot = hook._capture_snapshot([
        {"role": "user", "content": "Hello there"},
        {"role": "assistant", "content": "Hi!"},
    ])
    assert "[user]: Hello there" in snapshot
    assert "[assistant]: Hi!" in snapshot


def test_extract_tool_names(hook: ExperienceReviewHook):
    result = MagicMock()
    result.messages = [{
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "read_file", "arguments": ""}},
            {"function": {"name": "experience_write", "arguments": ""}},
        ],
    }]
    names = hook._extract_tool_names(result)
    assert names == {"read_file", "experience_write"}


def test_detect_exp_edit_direct_tools(hook: ExperienceReviewHook):
    result = MagicMock()
    result.messages = [{
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "experience_write", "arguments": ""}},
        ],
    }]
    assert hook._detect_exp_edit({"experience_write"}, result) is True


def test_detect_exp_edit_unified_experience_tool(hook: ExperienceReviewHook):
    result = MagicMock()
    result.messages = [{
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "experience", "arguments": '{"action": "write"}'}},
        ],
    }]
    assert hook._detect_exp_edit({"experience"}, result) is True


def test_detect_exp_edit_unified_experience_read(hook: ExperienceReviewHook):
    result = MagicMock()
    result.messages = [{
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": "experience", "arguments": '{"action": "read"}'}},
        ],
    }]
    assert hook._detect_exp_edit({"experience"}, result) is False


def test_scan_experience_dir(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        memory_system=_memory_system(),
        experience_dir=tmp_path,
        meta_store=meta_store,
    )
    # Create two experiences
    (tmp_path / "exp-a").mkdir()
    (tmp_path / "exp-a" / "EXPERIENCE.md").write_text("---\nname: exp-a\ndescription: x\n---\n\nBody")
    (tmp_path / "exp-b").mkdir()
    (tmp_path / "exp-b" / "EXPERIENCE.md").write_text("---\nname: exp-b\ndescription: x\n---\n\nBody")
    (tmp_path / ".archive").mkdir()  # should be skipped
    (tmp_path / "traces").mkdir()    # should be skipped

    result = hook._scan_experience_dir(tmp_path)
    assert "exp-a" in result
    assert "exp-b" in result
    assert ".archive" not in result
    assert "traces" not in result


@pytest.mark.asyncio
async def test_cleanup_removes_deleted(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        memory_system=_memory_system(),
        experience_dir=tmp_path,
        meta_store=meta_store,
    )
    meta_store.bump_use("exp-a")
    meta_store.bump_use("exp-b")

    before = {"exp-a": 1000.0, "exp-b": 2000.0}
    after = {"exp-a": 1000.0}  # exp-b deleted

    await hook._cleanup(before, after, tmp_path)

    assert meta_store.get("exp-a") is not None
    assert meta_store.get("exp-b") is None


@pytest.mark.asyncio
async def test_cleanup_removes_invalid(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        memory_system=_memory_system(),
        experience_dir=tmp_path,
        meta_store=meta_store,
    )
    # Create invalid experience (no frontmatter)
    exp_dir = tmp_path / "bad-exp"
    exp_dir.mkdir()
    exp_md = exp_dir / "EXPERIENCE.md"
    exp_md.write_text("# No frontmatter here")

    before = {}
    after = {"bad-exp": exp_md.stat().st_mtime}

    await hook._cleanup(before, after, tmp_path)

    assert not exp_md.exists()
    assert not exp_dir.exists()  # empty dir removed


@pytest.mark.asyncio
async def test_cleanup_fixes_dir_name_mismatch(
    tmp_path: Path, meta_store: PerFileExperienceMetaStore
):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        memory_system=_memory_system(),
        experience_dir=tmp_path,
        meta_store=meta_store,
    )
    # Directory is "old-name" but frontmatter says "new-name"
    exp_dir = tmp_path / "old-name"
    exp_dir.mkdir()
    exp_md = exp_dir / "EXPERIENCE.md"
    exp_md.write_text(
        "---\nname: new-name\ndescription: test\n---\n\nBody content here.\n"
    )

    before = {}
    after = {"old-name": exp_md.stat().st_mtime}

    await hook._cleanup(before, after, tmp_path)

    # The hook's cleanup auto-corrects frontmatter name, NOT renames directory.
    # So old-name directory stays, but frontmatter name is corrected to "old-name".
    assert (tmp_path / "old-name").exists()
    assert (tmp_path / "old-name" / "EXPERIENCE.md").exists()
    saved = (tmp_path / "old-name" / "EXPERIENCE.md").read_text()
    assert "name: old-name" in saved


@pytest.mark.asyncio
async def test_cleanup_skips_name_mismatch_if_target_exists(
    tmp_path: Path, meta_store: PerFileExperienceMetaStore
):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        memory_system=_memory_system(),
        experience_dir=tmp_path,
        meta_store=meta_store,
    )
    # old-name with frontmatter name=new-name, but new-name already exists
    exp_dir = tmp_path / "old-name"
    exp_dir.mkdir()
    exp_md = exp_dir / "EXPERIENCE.md"
    exp_md.write_text(
        "---\nname: new-name\ndescription: test\n---\n\nBody content here.\n"
    )
    existing = tmp_path / "new-name"
    existing.mkdir()
    (existing / "EXPERIENCE.md").write_text(
        "---\nname: new-name\ndescription: existing\n---\n\nBody.\n"
    )

    before = {}
    after = {"old-name": exp_md.stat().st_mtime}

    await hook._cleanup(before, after, tmp_path)

    # Should NOT rename because target exists
    assert (tmp_path / "old-name").exists()
    assert (tmp_path / "new-name").exists()
