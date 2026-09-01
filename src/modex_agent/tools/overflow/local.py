from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pathvalidate import sanitize_filename
from pydantic import ValidationError

from modex_agent.memory.core.lock import AioRWLock
from modex_agent.tools.overflow.models import OverflowMetadata, OverflowRef
from modex_agent.tools.overflow.store import ToolOverflowStore


class LocalFileToolOverflowStore(ToolOverflowStore):
    """Filesystem-based implementation of ToolOverflowStore.

    Directory layout::

        {workspace}/tool_overflow/{session_id}/{tool_call_id}/
        ├── .meta.json
        └── full.txt       ← raw content, no header
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._lock = AioRWLock()

    @staticmethod
    def _sanitize_id(value: str) -> str:
        safe = Path(value).name
        if not safe or ".." in safe:
            raise ValueError(f"Invalid identifier: {value!r}")
        return sanitize_filename(safe, replacement_text="_")

    def _session_dir(self, session_id: str) -> Path:
        return self._workspace / "tool_overflow" / self._sanitize_id(session_id)

    def _entry_dir(self, session_id: str, tool_call_id: str) -> Path:
        return self._session_dir(session_id) / self._sanitize_id(tool_call_id)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._workspace.mkdir, parents=True, exist_ok=True)

    async def store(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> OverflowRef:
        entry_dir = self._entry_dir(session_id, tool_call_id)
        absolute_dir = entry_dir.resolve()

        created_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        total_chars = len(content)

        meta = OverflowMetadata(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            session_id=session_id,
            created_at=created_at,
            total_chars=total_chars,
        )

        async with self._lock.write():
            await asyncio.to_thread(entry_dir.mkdir, parents=True, exist_ok=True)

            # full.txt first, .meta.json LAST: the metadata file is the
            # entry's commit marker (list_tool_call_ids counts only
            # directories that carry it), so a crash mid-write leaves a
            # half-entry that is never listed instead of a listed entry
            # whose full.txt is missing.
            full_path = entry_dir / "full.txt"
            await asyncio.to_thread(full_path.write_text, content, encoding="utf-8")

            meta_path = entry_dir / ".meta.json"
            await asyncio.to_thread(
                meta_path.write_text, meta.model_dump_json(), encoding="utf-8"
            )

        return OverflowRef(
            dir_path=str(absolute_dir),
            total_chars=total_chars,
            metadata_path=str(meta_path.resolve()),
        )

    async def read_metadata(
        self,
        session_id: str,
        tool_call_id: str,
    ) -> OverflowMetadata | None:
        entry_dir = self._entry_dir(session_id, tool_call_id)
        meta_path = entry_dir / ".meta.json"

        async with self._lock.read():
            if not await asyncio.to_thread(meta_path.exists):
                return None
            text = await asyncio.to_thread(meta_path.read_text, encoding="utf-8")
            return OverflowMetadata.model_validate_json(text)

    async def delete(self, session_id: str, tool_call_id: str) -> bool:
        entry_dir = self._entry_dir(session_id, tool_call_id)

        async with self._lock.write():
            if not await asyncio.to_thread(entry_dir.exists):
                return False
            await asyncio.to_thread(shutil.rmtree, entry_dir)
            return True

    async def list_tool_call_ids(self, session_id: str) -> list[str]:
        session_dir = self._session_dir(session_id)

        async with self._lock.read():
            if not await asyncio.to_thread(session_dir.exists):
                return []

            entries = await asyncio.to_thread(
                lambda: [
                    (p.name, p)
                    for p in session_dir.iterdir()
                    if p.is_dir() and (p / ".meta.json").exists()
                ]
            )

        # Read created_at from .meta.json for cross-platform consistent sorting
        sorted_entries: list[tuple[str, str]] = []
        for name, p in entries:
            try:
                meta_text = await asyncio.to_thread((p / ".meta.json").read_text, encoding="utf-8")
                meta = OverflowMetadata.model_validate_json(meta_text)
                sorted_entries.append((name, meta.created_at))
            except (FileNotFoundError, OSError, ValidationError):
                # Entry may have been deleted between listing and reading
                continue
        sorted_entries.sort(key=lambda x: x[1])
        return [name for name, _ in sorted_entries]

    async def close(self) -> None:
        pass
