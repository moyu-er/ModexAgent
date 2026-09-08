"""LRU curation of excess experiences."""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from modex_agent.plugins.defaults.capabilities.experience.metadata import ExperienceMetaStore
from modex_agent.plugins.defaults.capabilities.experience.models import CurationResult

logger = logging.getLogger(__name__)


class ExperienceCurator:
    """Limit the number of active experiences via LRU eviction.

    When the count of non-pinned experiences exceeds *max_experiences*,
    the least-recently-used entries are deleted permanently.  Pinned
    experiences are counted toward the total but are never eligible for
    eviction.

    Regular runtime class (plan §6.1): holds live store/dir handles.
    """

    def __init__(
        self,
        experience_dir: Path | Callable[[], Path],
        meta_store: ExperienceMetaStore,
        max_experiences: int = 20,
    ) -> None:
        self._get_dir = experience_dir if callable(experience_dir) else lambda: experience_dir
        self._meta_store = meta_store
        self._max_experiences = max_experiences

    @property
    def max_experiences(self) -> int:
        return self._max_experiences

    async def run(self) -> CurationResult:
        """Evict excess experiences and return counts."""
        records = self._meta_store.list_all()
        checked = len(records)

        # Categorize and count
        total = 0
        evictable: list[tuple[str, datetime | None]] = []
        for name, record in records.items():
            total += 1
            if record.pinned:
                continue  # counted but immune
            last_used = record.last_used_at
            last_dt = None
            if last_used:
                with contextlib.suppress(ValueError, TypeError):
                    last_dt = datetime.fromisoformat(last_used)
            evictable.append((name, last_dt))

        excess = total - self._max_experiences
        if excess <= 0:
            return CurationResult(checked=checked, evicted=0)

        # Sort by last_used ascending (None = never used -> evict first)
        evictable.sort(key=lambda x: x[1] if x[1] is not None else datetime.min.replace(tzinfo=UTC))

        evicted = 0
        for name, _ in evictable[:excess]:
            if self._delete(name):
                evicted += 1

        return CurationResult(checked=checked, evicted=evicted)

    def _delete(self, name: str) -> bool:
        """Permanently delete an experience directory and its meta record."""
        src = self._get_dir() / name
        if src.exists():
            try:
                shutil.rmtree(src)
            except OSError:
                logger.warning("Failed to delete experience: %s", name, exc_info=True)
                return False
        self._meta_store.remove(name)
        logger.info("Experience evicted: %s", name)
        return True
