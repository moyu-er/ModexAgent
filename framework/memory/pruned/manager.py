"""Orchestrates writing pruned batches, building the index, and eviction.

Storage is session-scoped: each session_id gets its own sub-directory under
*pruned_base_dir*, created lazily on first write.
"""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, TYPE_CHECKING

from framework.memory.pruned.models import PrunedIndexEntry
from framework.utils.timezone import get_user_timezone

if TYPE_CHECKING:
    from framework.memory.pruned.storage import PrunedStorage, FilePrunedStorage  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


class PrunedManager:
    """Manages pruned memory catalog: writes on cleanup, reads for injection.

    Not a MemorySystem layer — a standalone component shared by cleanup
    and injection.  Independent of archive configuration.

    Storage is session-scoped — the same manager instance serves all
    sessions under *pruned_base_dir* and creates a per-session
    ``FilePrunedStorage`` lazily.
    """

    def __init__(
        self,
        pruned_base_dir: Path,
        max_files: int = 50,
        topic_max_chars: int = 200,
    ) -> None:
        self._base_dir = pruned_base_dir
        self._max_files = max_files
        self._topic_max = topic_max_chars
        # Per-session storage instances (created lazily)
        self._storages: dict[str, PrunedStorage] = {}

    # -- public API ----------------------------------------------------------

    async def write_pruned(
        self,
        pruned_messages: list[dict],
        topic: str | None,
        cleanup_time: datetime,
        *,
        session_id: str = "",
    ) -> None:
        storage = self._get_storage(session_id)
        start, end = self._extract_time_range(pruned_messages)
        filename = self._generate_filename(start, end, cleanup_time)
        resolved_topic = self._resolve_topic(topic, start, end, cleanup_time, len(pruned_messages))
        serializable = self._serialize_messages(pruned_messages)
        storage.write_pruned(filename, serializable)
        entry = self._build_index_entry(
            pruned_messages, resolved_topic, cleanup_time, filename, start, end, session_id,
        )
        storage.append_index(entry)
        storage.prune_oldest(self._max_files)

    async def refresh_from_archives(
        self,
        archive_storage: Any,
        *,
        session_id: str = "",
    ) -> int:
        """Full refresh pruned index from all archive index.md files.

        Traverses all archive directories, reads index.md, rebuilds the pruned index.
        On failure or empty archives, returns 0 (caller falls back).

        Returns: number of index entries loaded.
        """
        if archive_storage is None:
            return 0

        try:
            archive_ids = await archive_storage.list_archives()
        except Exception:
            logger.warning("Failed to list archives for index refresh", exc_info=True)
            return 0

        if not archive_ids:
            return 0

        # Process in ascending order so id=1 is the oldest archive
        entries: list[PrunedIndexEntry] = []
        now_ts = int(datetime.now(get_user_timezone()).timestamp())
        now_display = datetime.now(get_user_timezone()).strftime("%Y-%m-%d %H:%M")

        for aid in sorted(archive_ids):
            try:
                content = await archive_storage.read_archive_file(aid, "index.md")
                if not content or not content.strip():
                    continue
                # First non-empty line becomes the topic
                topic = content.strip().split("\n")[0].strip()
                if not topic:
                    continue
            except Exception:
                logger.debug(
                    "Skipping archive %d — failed to read index.md", aid, exc_info=True,
                )
                continue

            entries.append(PrunedIndexEntry(
                id=aid,
                cleanup_time=now_ts,
                cleanup_time_display=now_display,
                message_count=0,
                content_filename=f"archive/{aid}/context.md",
                topic=topic,
            ))

        if not entries:
            return 0

        storage = self._get_storage(session_id)
        try:
            storage.save_index(entries)
        except Exception:
            logger.warning(
                "Failed to save refreshed index: session=%s", session_id, exc_info=True,
            )
            return 0

        logger.info(
            "Pruned index refreshed from archives: entries=%d session=%s",
            len(entries), session_id,
        )
        return len(entries)

    def get_injection_xml(self, *, session_id: str = "") -> str | None:
        storage = self._get_storage(session_id)
        if not storage.has_content():
            return None
        path = escape(storage.get_directory_path())
        return (
            "<memory_archives>\n"
            "<!-- Pruned conversation segments are stored as read-only files in the directory below.\n"
            "     An index.jsonl in the same directory catalogs each segment with topic, time range,\n"
            "     and file path.\n"
            "     NOTE: index.jsonl is editable — you should update it to improve topic descriptions\n"
            "     or categorization when you have better context. The pruned segment files themselves\n"
            "     must NOT be modified. -->\n"
            f'  <directory path="{path}"/>\n'
            "</memory_archives>"
        )

    # -- private helpers -----------------------------------------------------

    def _get_storage(self, session_id: str) -> PrunedStorage:
        if not session_id:
            logger.warning(
                "PrunedManager._get_storage called with empty session_id — "
                "pruned content from sessions without IDs will share a single directory"
            )
        if session_id not in self._storages:
            from framework.memory.pruned.storage import FilePrunedStorage
            from framework.memory.stores.utils import sanitize_scope_key

            safe = sanitize_scope_key(session_id)
            self._storages[session_id] = FilePrunedStorage(self._base_dir / safe)
        return self._storages[session_id]

    @staticmethod
    def _serialize_messages(messages: list[dict]) -> list[dict]:
        result: list[dict] = []
        for msg in messages:
            out: dict = {}
            for k, v in msg.items():
                if isinstance(v, datetime):
                    out[k] = v.isoformat()
                else:
                    out[k] = v
            result.append(out)
        return result

    def _generate_filename(
        self,
        start: datetime | None,
        end: datetime | None,
        cleanup_time: datetime,
    ) -> str:
        fmt = "%Y-%m-%d_%H.%M"
        if start is not None and end is not None:
            return f"pruned_{start.strftime(fmt)}-{end.strftime(fmt)}.jsonl"
        return f"pruned_{cleanup_time.strftime(fmt)}.jsonl"

    def _build_index_entry(
        self,
        pruned_messages: list[dict],
        topic: str,
        cleanup_time: datetime,
        filename: str,
        start: datetime | None,
        end: datetime | None,
        session_id: str,
    ) -> PrunedIndexEntry:
        display_fmt = "%Y-%m-%d %H:%M"
        # Derive per-session monotonic id
        storage = self._get_storage(session_id)
        existing = storage.read_index()
        next_id = max((e.id for e in existing), default=0) + 1
        return PrunedIndexEntry(
            id=next_id,
            cleanup_time=int(cleanup_time.timestamp()),
            cleanup_time_display=cleanup_time.strftime(display_fmt),
            message_count=len(pruned_messages),
            content_filename=filename,
            start_time=int(start.timestamp()) if start is not None else 0,
            end_time=int(end.timestamp()) if end is not None else 0,
            start_time_display=start.strftime(display_fmt) if start is not None else "",
            end_time_display=end.strftime(display_fmt) if end is not None else "",
            topic=topic,
        )

    def _resolve_topic(
        self,
        topic: str | None,
        start: datetime | None,
        end: datetime | None,
        cleanup_time: datetime,
        count: int,
    ) -> str:
        display_fmt = "%Y-%m-%d %H:%M"
        if topic is not None:
            return topic[: self._topic_max]
        if start is not None and end is not None:
            result = f"{start.strftime(display_fmt)} ~ {end.strftime(display_fmt)} ({count} messages)"
        else:
            result = f"{cleanup_time.strftime(display_fmt)} ({count} messages)"
        return result[: self._topic_max]

    @staticmethod
    def _extract_time_range(
        messages: list[dict],
    ) -> tuple[datetime | None, datetime | None]:
        datetimes: list[datetime] = []
        for msg in messages:
            raw = msg.get("created_at")
            if raw is None:
                continue
            if isinstance(raw, datetime):
                dt = raw
            else:
                dt = datetime.fromisoformat(str(raw))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=get_user_timezone())
            datetimes.append(dt)
        if not datetimes:
            return None, None
        return min(datetimes), max(datetimes)
