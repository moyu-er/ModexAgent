from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from modex_agent.core.experience.meta import ExperienceMetaStore

logger = logging.getLogger(__name__)

DEFAULT_MAX_EXPERIENCES = 20


class ExperienceCurator:
    """Limit the number of active experiences via LRU eviction.

    When the count of non-pinned experiences exceeds *max_experiences*,
    the least-recently-used entries are deleted permanently.  Pinned
    experiences are counted toward the total but are never eligible for
    eviction.
    """

    def __init__(
        self,
        experience_dir: Path | Callable[[], Path],
        meta_store: ExperienceMetaStore,
        max_experiences: int = DEFAULT_MAX_EXPERIENCES,
    ) -> None:
        self._get_dir = experience_dir if callable(experience_dir) else lambda: experience_dir
        self._meta_store = meta_store
        self._max_experiences = max_experiences

    async def run(self) -> dict[str, int]:
        """Evict excess experiences and return counts."""
        counts: dict[str, int] = {"checked": 0, "evicted": 0}

        records = self._meta_store.list_all()
        counts["checked"] = len(records)

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
            return counts

        # Sort by last_used ascending (None = never used → evict first)
        evictable.sort(key=lambda x: x[1] if x[1] is not None else datetime.min.replace(tzinfo=UTC))

        for name, _ in evictable[:excess]:
            self._delete(name)
            counts["evicted"] += 1

        return counts

    def _delete(self, name: str) -> None:
        """Permanently delete an experience directory and its meta record."""
        src = self._get_dir() / name
        if src.exists():
            try:
                shutil.rmtree(src)
            except OSError:
                logger.warning("Failed to delete experience: %s", name, exc_info=True)
                return
        self._meta_store.remove(name)
        logger.info("Experience evicted: %s", name)
