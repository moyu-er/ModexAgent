from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from framework.core.experience.curator import ExperienceCurator
from framework.core.experience.meta import ExperienceMetaRecord, PerFileExperienceMetaStore


def _setup_exp(
    tmp_path: Path,
    meta_store: PerFileExperienceMetaStore,
    name: str,
    last_used_at: str | None = None,
    pinned: bool = False,
) -> None:
    """Create an experience directory with metadata."""
    exp_dir = tmp_path / name
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "EXPERIENCE.md").write_text(
        f"---\nname: {name}\ndescription: x\n---\n\nBody.\n"
    )
    if last_used_at is not None:
        record = ExperienceMetaRecord(last_used_at=last_used_at, pinned=pinned)
        meta_store.set(name, record)


@pytest.fixture
def meta_store(tmp_path: Path) -> PerFileExperienceMetaStore:
    return PerFileExperienceMetaStore(tmp_path)


@pytest.mark.asyncio
async def test_under_limit_no_eviction(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    """When experiences are at or under max, nothing is deleted."""
    now = datetime.now(timezone.utc).isoformat()
    _setup_exp(tmp_path, meta_store, "exp-a", last_used_at=now, pinned=False)
    _setup_exp(tmp_path, meta_store, "exp-b", last_used_at=now, pinned=False)

    curator = ExperienceCurator(tmp_path, meta_store, max_experiences=5)
    counts = await curator.run()
    assert counts["evicted"] == 0
    assert (tmp_path / "exp-a").exists()
    assert (tmp_path / "exp-b").exists()


@pytest.mark.asyncio
async def test_lru_eviction_excess(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    """When experiences exceed max, LRU ones are deleted."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=5)).isoformat()
    older = (now - timedelta(days=10)).isoformat()

    for i in range(5):
        ts = older if i < 2 else old
        _setup_exp(tmp_path, meta_store, f"exp-{i}", last_used_at=ts, pinned=False)

    curator = ExperienceCurator(tmp_path, meta_store, max_experiences=3)
    counts = await curator.run()
    # 5 total, max=3 → 2 evicted (the older ones: exp-0, exp-1)
    assert counts["evicted"] == 2
    assert not (tmp_path / "exp-0").exists()  # deleted
    assert not (tmp_path / "exp-1").exists()  # deleted
    assert (tmp_path / "exp-2").exists()
    assert meta_store.get("exp-0") is None
    assert meta_store.get("exp-2") is not None


@pytest.mark.asyncio
async def test_lru_pinned_exempt_from_eviction(tmp_path: Path, meta_store: PerFileExperienceMetaStore):
    """Pinned experiences are NOT evicted by LRU."""
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    _setup_exp(tmp_path, meta_store, "exp-0", last_used_at=old, pinned=True)
    _setup_exp(tmp_path, meta_store, "exp-1", last_used_at=old, pinned=False)
    _setup_exp(tmp_path, meta_store, "exp-2", last_used_at=old, pinned=False)

    curator = ExperienceCurator(tmp_path, meta_store, max_experiences=1)
    counts = await curator.run()
    # 3 total (1 pinned), max=1 → 2 evicted, pinned exp-0 survives
    assert counts["evicted"] == 2
    assert (tmp_path / "exp-0").exists()  # pinned survives
    assert not (tmp_path / "exp-1").exists()
    assert not (tmp_path / "exp-2").exists()
