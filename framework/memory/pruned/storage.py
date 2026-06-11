from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

from framework.memory.pruned.models import PrunedIndexEntry
from framework.utils.file_io import read_jsonl_robust

logger = logging.getLogger(__name__)


class PrunedStorage(ABC):
    """Abstract interface for persisting pruned message batches and their index."""

    @abstractmethod
    def write_pruned(self, filename: str, messages: list[dict]) -> None:
        """Write a batch of messages to a JSONL file."""
        ...

    @abstractmethod
    def append_index(self, entry: PrunedIndexEntry) -> None:
        """Append a single entry to the index file."""
        ...

    @abstractmethod
    def read_index(self) -> list[PrunedIndexEntry]:
        """Read all index entries. Returns an empty list if no index exists.

        Implementations should be resilient to individual malformed entries
        (invalid JSON, missing required fields): log and skip them rather than
        raising, so callers always receive the valid subset.
        """
        ...

    @abstractmethod
    def save_index(self, entries: list[PrunedIndexEntry]) -> None:
        """Atomically replace the entire index with *entries*."""
        ...

    @abstractmethod
    def has_content(self) -> bool:
        """Return True if any content file (not the index) exists."""
        ...

    @abstractmethod
    def prune_oldest(self, keep_count: int) -> None:
        """Remove the oldest entries beyond *keep_count*, deleting their files."""
        ...

    @abstractmethod
    def get_directory_path(self) -> str:
        """Return the absolute path of the storage directory."""
        ...


class FilePrunedStorage(PrunedStorage):
    """File-system backed storage using JSONL for both content and index."""

    def __init__(self, pruned_dir: Path, index_filename: str = "index.jsonl") -> None:
        self._dir = pruned_dir
        self._index_filename = index_filename

    # -- PrunedStorage implementation ----------------------------------------

    def write_pruned(self, filename: str, messages: list[dict]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / filename
        tmp = target.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for msg in messages:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        # os.replace is atomic on the same filesystem
        os.replace(str(tmp), str(target))

    def append_index(self, entry: PrunedIndexEntry) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        index_path = self._dir / self._index_filename
        with open(index_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def read_index(self) -> list[PrunedIndexEntry]:
        """Read all index entries, skipping malformed lines.

        index.jsonl is editable by the agent (as stated in the system prompt).
        A corrupted line (invalid JSON, missing required fields) is logged and
        skipped so the remaining valid entries are still available for injection.
        """
        index_path = self._dir / self._index_filename
        raw_entries = read_jsonl_robust(index_path)
        entries: list[PrunedIndexEntry] = []
        for parsed in raw_entries:
            try:
                entries.append(PrunedIndexEntry.from_dict(parsed))
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping invalid index entry in %s: %s",
                    index_path,
                    exc,
                )
        return entries

    def save_index(self, entries: list[PrunedIndexEntry]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._rewrite_index(entries)

    def has_content(self) -> bool:
        if not self._dir.exists():
            return False
        # Check for .jsonl content files (legacy path)
        if any(
            f.suffix == ".jsonl" and f.name != self._index_filename
            for f in self._dir.iterdir()
            if f.is_file()
        ):
            return True
        # Index file existence is sufficient — get_injection_xml will
        # handle the case where it exists but has no valid entries.
        return (self._dir / self._index_filename).exists()

    def prune_oldest(self, keep_count: int) -> None:
        entries = self.read_index()
        if len(entries) <= keep_count:
            return
        to_remove = entries[:-keep_count]
        for entry in to_remove:
            filepath = self._dir / entry.content_filename
            if filepath.exists():
                filepath.unlink()
        surviving = entries[-keep_count:]
        self._rewrite_index(surviving)

    def get_directory_path(self) -> str:
        return str(self._dir.resolve())

    # -- private helpers -----------------------------------------------------

    def _rewrite_index(self, entries: list[PrunedIndexEntry]) -> None:
        index_path = self._dir / self._index_filename
        tmp = index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        os.replace(str(tmp), str(index_path))
