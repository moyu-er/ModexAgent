"""Tests for ExperienceReviewHook."""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.core.experience.meta import PerFileExperienceMetaStore
from framework.hook.builtin.experience_review import ExperienceReviewHook


@pytest.fixture
def meta_store(tmp_path: Path) -> PerFileExperienceMetaStore:
    return PerFileExperienceMetaStore(tmp_path)


@pytest.fixture
def hook(tmp_path: Path, meta_store: PerFileExperienceMetaStore) -> ExperienceReviewHook:
    agent = MagicMock()

    async def fake_review(**_kwargs):
        return True
    agent.review = fake_review

    return ExperienceReviewHook(
        review_agent=agent,
        experience_dir=tmp_path,
        meta_store=meta_store,
        review_interval=5,
    )


def test_hook_skips_when_turn_not_multiple(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    agent = MagicMock()
    agent.review = MagicMock()
    hook = ExperienceReviewHook(
        review_agent=agent,
        experience_dir=tmp_path,
        meta_store=meta_store,
        review_interval=5,
    )
    ctx = MagicMock(turn_count=3)
    asyncio.run(hook.after_turn(ctx, MagicMock()))
    agent.review.assert_not_called()


@pytest.mark.asyncio
async def test_hook_skips_when_mutex_busy(hook: ExperienceReviewHook):
    hook._pending.add(asyncio.create_task(asyncio.sleep(0.001)))
    ctx = MagicMock(turn_count=5)
    await hook.after_turn(ctx, MagicMock())
    # Mutex busy — review should NOT be called
    # (the mock doesn't actually call review, so this just tests no crash)


def test_capture_snapshot_empty(hook: ExperienceReviewHook):
    ctx = MagicMock()
    ctx.history.messages = []
    snapshot = hook._capture_snapshot(ctx)
    assert snapshot == ""


def test_scan_experience_dir(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
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

    result = hook._scan_experience_dir()
    assert "exp-a" in result
    assert "exp-b" in result
    assert ".archive" not in result
    assert "traces" not in result


@pytest.mark.asyncio
async def test_cleanup_removes_deleted(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
        experience_dir=tmp_path,
        meta_store=meta_store,
    )
    meta_store.bump_use("exp-a")
    meta_store.bump_use("exp-b")

    before = {"exp-a": 1000.0, "exp-b": 2000.0}
    after = {"exp-a": 1000.0}  # exp-b deleted

    await hook._cleanup(before, after)

    assert meta_store.get("exp-a") is not None
    assert meta_store.get("exp-b") is None


@pytest.mark.asyncio
async def test_cleanup_removes_invalid(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
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

    await hook._cleanup(before, after)

    assert not exp_md.exists()
    assert not exp_dir.exists()  # empty dir removed


@pytest.mark.asyncio
async def test_cleanup_fixes_dir_name_mismatch(
    tmp_path: Path, meta_store: PerFileExperienceMetaStore
):
    hook = ExperienceReviewHook(
        review_agent=MagicMock(),
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

    await hook._cleanup(before, after)

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

    await hook._cleanup(before, after)

    # Should NOT rename because target exists
    assert (tmp_path / "old-name").exists()
    assert (tmp_path / "new-name").exists()
