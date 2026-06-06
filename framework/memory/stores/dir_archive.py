"""Directory-based archive storage with per-archive-id MD files."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_ARCHIVE_FILES: frozenset[str] = frozenset(
    {"context.md", "knowledge.md", "index.md"}
)


class DirArchiveStorage:
    """Archive storage backed by a directory tree of markdown files.

    Layout::

        {base_dir}/
          state.json
          1/
            context.md
            knowledge.md
            index.md
          2/
            ...
    """

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir

    # -- properties ----------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def directory(self) -> Path:
        """Alias for compatibility with callers that expect ``.directory``."""
        return self._base

    # -- ArchiveChannelStorage protocol --------------------------------------

    async def read_archive_state(self) -> dict[str, Any] | None:
        """Return the persisted archive state, or ``None`` if absent."""
        state_path = self._base / "state.json"
        if not state_path.exists():
            return None
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        """Persist the archive state atomically."""
        self._base.mkdir(parents=True, exist_ok=True)
        state_path = self._base / "state.json"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def append_channel_log(
        self, channel: str, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """Append *entry* to the log for *channel* and return the stored record.

        Reads ``next_archive_id`` from state (defaulting to 1), creates the
        archive subdirectory, and writes the entry's ``summary`` field as
        ``{channel}.md`` if non-empty.  Does **not** increment
        ``next_archive_id`` — that happens in the cleanup step.
        """
        state = await self.read_archive_state()
        next_id: int = (state or {}).get("next_archive_id", 1)

        archive_dir = self._base / str(next_id)
        archive_dir.mkdir(parents=True, exist_ok=True)

        summary: str | None = entry.get("summary")
        if summary:
            (archive_dir / f"{channel}.md").write_text(summary, encoding="utf-8")

        return {**entry, "archive_id": next_id, "channel": channel}

    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        """Read logs for *channel* with ``archive_id`` > *since_archive_id*.

        Scans numeric subdirectories of ``base_dir`` and reads
        ``{channel}.md`` from each.
        """
        if not self._base.exists():
            return []

        results: list[dict[str, Any]] = []
        for child in sorted(self._base.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or not child.name.isdigit():
                continue
            aid = int(child.name)
            if aid <= since_archive_id:
                continue
            md_path = child / f"{channel}.md"
            if not md_path.exists():
                continue
            results.append(
                {
                    "archive_id": aid,
                    "channel": channel,
                    "summary": md_path.read_text(encoding="utf-8"),
                    "metadata": {},
                }
            )
            if len(results) >= limit:
                break
        return results

    async def save_channel_logs(
        self, channel: str, entries: list[dict[str, Any]]
    ) -> None:
        """Remove archive directories not present in *entries*."""
        if not self._base.exists():
            return
        kept_ids: set[int] = {
            int(e.get("archive_id", 0))
            for e in entries
            if e.get("archive_id")
        }
        for child in list(self._base.iterdir()):
            if child.is_dir() and child.name.isdigit():
                aid = int(child.name)
                if aid not in kept_ids and aid > 0:
                    shutil.rmtree(child, ignore_errors=True)

    async def prune_to_max(self, max_total: int, min_safe_id: int = 0) -> int:
        """Delete oldest archive dirs exceeding max_total, but never below min_safe_id.

        min_safe_id is typically knowledge_consumed_archive_id — archives at or below
        this ID are already consumed and safe to delete. Archives above it are preserved
        for pending knowledge digestion.
        """
        ids = await self.list_archives(limit=10_000)
        deletable = [aid for aid in ids if aid <= min_safe_id] if min_safe_id > 0 else []
        if len(deletable) <= max_total:
            return 0
        ascending = sorted(deletable)
        to_delete = ascending[:-max_total]
        for aid in to_delete:
            shutil.rmtree(self._base / str(aid), ignore_errors=True)
        return len(to_delete)

    async def cleanup_empty_dirs(self) -> int:
        """Remove archive directories with no non-empty required files."""
        count = 0
        for child in list(self._base.iterdir()):
            if child.is_dir() and child.name.isdigit():
                has_content = any(
                    (child / f).exists() and (child / f).stat().st_size > 0
                    for f in _REQUIRED_ARCHIVE_FILES
                )
                if not has_content:
                    shutil.rmtree(child, ignore_errors=True)
                    count += 1
        return count

    # -- MD-file-specific methods --------------------------------------------

    async def write_archive_file(
        self, archive_id: int, filename: str, content: str
    ) -> int:
        """Write *content* to ``{base_dir}/{archive_id}/{filename}``.

        Creates the archive directory if needed.  Returns the byte count
        written.
        """
        archive_dir = self._base / str(archive_id)
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / filename
        target.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))

    async def read_archive_file(
        self, archive_id: int, filename: str
    ) -> str | None:
        """Read a file from an archive directory.  Returns ``None`` if missing."""
        target = self._base / str(archive_id) / filename
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    async def list_archives(
        self, since_id: int = 0, limit: int = 100
    ) -> list[int]:
        """List archive IDs > *since_id*, in descending order."""
        if not self._base.exists():
            return []
        ids: list[int] = sorted(
            (
                int(child.name)
                for child in self._base.iterdir()
                if child.is_dir() and child.name.isdigit()
            ),
            reverse=True,
        )
        return [aid for aid in ids if aid > since_id][:limit]

    async def is_archive_complete(self, archive_id: int) -> bool:
        """Check if the archive dir has all 3 required MD files, non-empty."""
        archive_dir = self._base / str(archive_id)
        if not archive_dir.is_dir():
            return False
        for name in _REQUIRED_ARCHIVE_FILES:
            target = archive_dir / name
            if not target.exists() or target.stat().st_size == 0:
                return False
        return True


__all__ = ["DirArchiveStorage"]
