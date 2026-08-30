"""Orchestrates writing pruned batches, building the index, and eviction.

Storage is session-scoped: each session_id gets its own sub-directory under
*pruned_base_dir*, created lazily on first write.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.types import MessageRole
from modex_agent.memory.pruned.models import PrunedIndexEntry
from modex_agent.memory.pruned.render import render_transcript
from modex_agent.memory.tags import PrunedTag
from modex_agent.utils.timezone import get_user_timezone
from modex_agent.utils.xml import xml_attr, xml_text

if TYPE_CHECKING:
    from modex_agent.memory.pruned.storage import (
        PrunedStorage,
    )

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
        """Write a pruned batch as a markdown transcript and index it.

        COMPACT-role messages are framework-generated summaries, not original
        conversation memory — they are excluded here, at the entry point. An
        all-COMPACT batch is skipped entirely (no file, no index entry).
        """
        storage = self._get_storage(session_id)
        original = [
            m for m in pruned_messages if str(m.get("role", "")) != str(MessageRole.COMPACT)
        ]
        if not original:
            return
        start, end = self._extract_time_range(original)
        next_id = max((e.id for e in storage.read_index()), default=0) + 1
        resolved_topic = self._resolve_topic(topic, start, end, cleanup_time, len(original))
        filename = self._generate_filename(start, end, cleanup_time, next_id)
        text = render_transcript(next_id, resolved_topic, original, start, end)
        storage.write_transcript(filename, text)
        display_fmt = "%Y-%m-%d %H:%M"
        entry = PrunedIndexEntry(
            id=next_id,
            cleanup_time=int(cleanup_time.timestamp()),
            cleanup_time_display=cleanup_time.strftime(display_fmt),
            message_count=len(original),
            content_filename=filename,
            start_time=int(start.timestamp()) if start is not None else 0,
            end_time=int(end.timestamp()) if end is not None else 0,
            start_time_display=start.strftime(display_fmt) if start is not None else "",
            end_time_display=end.strftime(display_fmt) if end is not None else "",
            topic=resolved_topic,
            content_chars=len(text),
        )
        storage.append_index(entry)
        storage.prune_oldest(self._max_files)

    def get_injection_xml(self, *, session_id: str = "") -> str | None:
        storage = self._get_storage(session_id)
        if not storage.has_content():
            return None
        path = xml_attr(storage.get_directory_path())
        ct = PrunedTag.CONTAINER.value
        tt = PrunedTag.TRANSCRIPT.value
        heading = (
            "### Previous Conversation Transcripts\n\n"
            "The directory below stores complete transcripts of previous conversations\n"
            "(not the current one). The <history> section is a partial preview — read\n"
            "index.jsonl for the full catalog (topic, time range, message count, size),\n"
            "then read specific transcripts when needed.\n\n"
            "Transcripts are information-dense: each file holds an entire pruned\n"
            "conversation, and tool-result blocks can be very large. NEVER read a whole\n"
            "file. Use this discipline:\n"
            '1. grep -n "^## \\[" <file> — message table-of-contents with line numbers\n'
            "2. read a narrow offset/limit window around the messages you need\n"
            "3. grep keywords first when hunting a specific detail (error text, path)\n"
            "Transcripts are read-only; you may update topic descriptions in index.jsonl.\n\n"
        )
        lines: list[str] = [
            heading,
            f"<{ct}>",
            f'  <directory path="{path}"/>',
        ]

        # Embed recent index entries so the agent sees what happened
        # without having to read files first.
        entries = storage.read_index()
        recent_entries = entries[-3:] if entries else []
        if recent_entries:
            history = PrunedTag.HISTORY.value
            lines.append(f"  <{history}>")
            for e in recent_entries:
                time_range = (
                    f"{e.start_time_display} ~ {e.end_time_display}"
                    if e.start_time_display and e.end_time_display
                    else e.cleanup_time_display
                )
                topic = e.topic or f"Conversation {e.id} ({e.message_count} messages)"
                if len(topic) > 200:
                    topic = topic[:200] + "..."
                lines.append(
                    f'    <{tt} time="{xml_attr(time_range)}"'
                    f' messages="{e.message_count}" chars="{e.content_chars}">'
                    f"\n{xml_text(topic)}\n"
                    f"</{tt}>"
                )
            lines.append(f"  </{history}>")

        lines.append(f"</{ct}>")
        return "\n".join(lines)

    def get_version(self, *, session_id: str = "") -> str:
        """Return the current version of pruned content for the given session.

        Version is the max entry ID from the index. Returns "0" when empty,
        "" on read error (triggers refresh in provider).
        """
        try:
            storage = self._get_storage(session_id)
            entries = storage.read_index()
            if not entries:
                return "0"
            return str(max(e.id for e in entries))
        except Exception:
            return ""

    # -- private helpers -----------------------------------------------------

    def _get_storage(self, session_id: str) -> PrunedStorage:
        if not session_id:
            logger.warning(
                "PrunedManager._get_storage called with empty session_id — "
                "pruned content from sessions without IDs will share a single directory"
            )
        if session_id not in self._storages:
            from modex_agent.memory.pruned.storage import FilePrunedStorage
            from modex_agent.memory.stores.utils import sanitize_scope_key

            safe = sanitize_scope_key(session_id)
            self._storages[session_id] = FilePrunedStorage(self._base_dir / safe)
        return self._storages[session_id]

    def _generate_filename(
        self,
        start: datetime | None,
        end: datetime | None,
        cleanup_time: datetime,
        next_id: int,
    ) -> str:
        fmt = "%Y-%m-%d_%H.%M.%S"
        if start is not None and end is not None:
            return f"pruned_{start.strftime(fmt)}-{end.strftime(fmt)}_{next_id}.md"
        return f"pruned_{cleanup_time.strftime(fmt)}_{next_id}.md"

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
            result = (
                f"{start.strftime(display_fmt)} ~ {end.strftime(display_fmt)} ({count} messages)"
            )
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
