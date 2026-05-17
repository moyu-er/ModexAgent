from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pathvalidate import sanitize_filename

from framework.memory.core.lock import AioRWLock
from framework.tools.overflow.models import OverflowMetadata, OverflowRef
from framework.tools.overflow.store import ToolOverflowStore


class LocalFileToolOverflowStore(ToolOverflowStore):
    """Filesystem-based implementation of ToolOverflowStore.

    Directory layout::

        {workspace}/tool_overflow/{session_id}/{tool_call_id}/
        ├── .meta.json
        ├── 1.full.txt       ← raw content, no header
        ├── 1.summary.txt    ← first 200 chars, no header
        └── ...
    """

    def __init__(
        self,
        workspace: Path,
        max_chunk_size: int = 9800,
        summary_chars: int = 200,
    ) -> None:
        self._workspace = workspace
        self._max_chunk_size = max_chunk_size
        self._summary_chars = summary_chars
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
        await asyncio.to_thread(
            self._workspace.mkdir, parents=True, exist_ok=True
        )

    async def store(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
    ) -> OverflowRef:
        entry_dir = self._entry_dir(session_id, tool_call_id)
        absolute_dir = entry_dir.resolve()

        # Chunk the content
        chunks: list[str] = []
        for i in range(0, len(content), self._max_chunk_size):
            chunks.append(content[i : i + self._max_chunk_size])
        total_chunks = len(chunks) if chunks else 1

        created_at = datetime.now(timezone.utc).isoformat()  # noqa: UP017
        total_chars = len(content)

        meta = OverflowMetadata(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            session_id=session_id,
            created_at=created_at,
            total_chars=total_chars,
            total_chunks=total_chunks,
            max_chunk_size=self._max_chunk_size,
        )

        async with self._lock.write():
            await asyncio.to_thread(entry_dir.mkdir, parents=True, exist_ok=True)

            # Write metadata
            meta_path = entry_dir / ".meta.json"
            meta_dict = {
                "tool_name": meta.tool_name,
                "tool_call_id": meta.tool_call_id,
                "session_id": meta.session_id,
                "created_at": meta.created_at,
                "total_chars": meta.total_chars,
                "total_chunks": meta.total_chunks,
                "max_chunk_size": meta.max_chunk_size,
            }
            await asyncio.to_thread(meta_path.write_text, json.dumps(meta_dict, ensure_ascii=False), encoding="utf-8")

            # Write chunks — raw content, no prefix header
            for idx, chunk in enumerate(chunks if chunks else [""], start=1):
                full_path = entry_dir / f"{idx}.full.txt"
                await asyncio.to_thread(full_path.write_text, chunk, encoding="utf-8")

                summary = chunk[: self._summary_chars]
                summary_path = entry_dir / f"{idx}.summary.txt"
                await asyncio.to_thread(summary_path.write_text, summary, encoding="utf-8")

        return OverflowRef(
            dir_path=str(absolute_dir),
            chunk_count=total_chunks,
            total_chars=total_chars,
            metadata_path=str(meta_path.resolve()),
        )

    async def read_chunk(
        self,
        session_id: str,
        tool_call_id: str,
        chunk_index: int,
        *,
        summary: bool = False,
    ) -> str | None:
        entry_dir = self._entry_dir(session_id, tool_call_id)
        suffix = "summary.txt" if summary else "full.txt"
        chunk_path = entry_dir / f"{chunk_index}.{suffix}"

        async with self._lock.read():
            if not await asyncio.to_thread(chunk_path.exists):
                return None
            return await asyncio.to_thread(chunk_path.read_text, encoding="utf-8")

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
            data = json.loads(text)
        return OverflowMetadata(
            tool_name=data["tool_name"],
            tool_call_id=data["tool_call_id"],
            session_id=data["session_id"],
            created_at=data["created_at"],
            total_chars=data["total_chars"],
            total_chunks=data["total_chunks"],
            max_chunk_size=data["max_chunk_size"],
        )

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
                meta_data = json.loads(meta_text)
                sorted_entries.append((name, meta_data.get("created_at", "")))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                # Entry may have been deleted between listing and reading
                continue
        sorted_entries.sort(key=lambda x: x[1])
        return [name for name, _ in sorted_entries]

    async def close(self) -> None:
        pass
