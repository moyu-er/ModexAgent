"""Core memory storage that stores .md files as actual files on disk.

Instead of kv.json, each core memory file (SOUL.md, USER.md, MEMORY.md) is stored
as a real file in the storage directory. Non-.md keys (metadata) still use kv.json.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.utils.file_io import safe_atomic_replace


class MarkdownCoreMemoryStorage(DefaultScopedStorage):
    """Core memory layer storage backed by individual .md files.

    ``get("SOUL.md")`` reads ``<dir>/SOUL.md``, ``set("SOUL.md", content)``
    writes ``<dir>/SOUL.md``.  Non-``.md`` keys (metadata starting with ``.``)
    fall through to the base kv.json implementation.
    """

    @staticmethod
    def _is_md_key(key: str) -> bool:
        return key.endswith(".md")

    def _md_path(self, key: str) -> Path:
        return self.directory / key

    async def get(self, key: str) -> Any | None:
        if self._is_md_key(key):
            path = self._md_path(key)
            if path.exists():
                return path.read_text(encoding="utf-8")
            return None
        return await super().get(key)

    async def set(self, key: str, value: Any) -> None:
        if self._is_md_key(key):
            async with self.get_lock().write():
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self._md_path(key)
                tmp_path = path.with_suffix(path.suffix + ".tmp")
                tmp_path.write_text(str(value), encoding="utf-8")
                safe_atomic_replace(tmp_path, path)
                self._touch()
            return
        await super().set(key, value)

    async def delete(self, key: str) -> bool:
        if self._is_md_key(key):
            async with self.get_lock().write():
                path = self._md_path(key)
                if path.exists():
                    path.unlink()
                    self._touch()
                    return True
                return False
        return await super().delete(key)

    async def list_keys(self, prefix: str = "") -> list[str]:
        md_files = [
            f.name for f in self.directory.glob("*.md") if f.is_file() and f.name.startswith(prefix)
        ]
        kv_keys = await super().list_keys(prefix)
        return sorted(set(md_files + kv_keys))
