"""Orchestrates writing pruned batches, building the index, and eviction.

Storage is session-scoped: each session_id gets its own sub-directory under
*pruned_base_dir*, created lazily on first write.
"""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from framework.memory.tags import PrunedTag
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

    def get_injection_xml(self, *, session_id: str = "") -> str | None:
        storage = self._get_storage(session_id)
        if not storage.has_content():
            return None
        path = escape(storage.get_directory_path())
        ct = PrunedTag.CONTAINER.value
        tt = PrunedTag.TRANSCRIPT.value
        lines: list[str] = [
            f"<{ct}>",
            "<!-- Complete transcripts of earlier conversations are stored in the directory below.",
            "     An index.jsonl in that directory lists each transcript file with its topic,",
            "     time range, and message count — use it to find relevant past conversations.",
            "     The transcripts themselves are read-only. The index.jsonl is editable —",
            "     you may update topic descriptions there when you understand the content better. -->",
            f'  <directory path="{path}"/>',
        ]

        # Embed recent index entries so the agent sees what happened
        # without having to read files first.
        entries = storage.read_index()
        recent = entries[-3:] if entries else []
        if recent:
            lines.append("  <available>")
            for e in recent:
                time_range = (
                    f"{e.start_time_display} ~ {e.end_time_display}"
                    if e.start_time_display and e.end_time_display
                    else e.cleanup_time_display
                )
                topic = e.topic or f"Conversation {e.id} ({e.message_count} messages)"
                if len(topic) > 200:
                    topic = topic[:200] + "..."
                lines.append(
                    f'    <{tt} time="{escape(time_range)}"'
                    f' messages="{e.message_count}">'
                    f"{escape(topic)}"
                    f"</{tt}>"
                )
            lines.append("  </available>")

        lines.append(f"</{ct}>")
        return "\n".join(lines)

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
