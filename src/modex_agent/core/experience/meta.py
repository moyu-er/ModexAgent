"""Per-experience metadata storage — no memory cache, direct disk I/O."""

from __future__ import annotations

import contextlib
import json
import logging
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from modex_agent.utils.file_io import safe_atomic_replace

logger = logging.getLogger(__name__)

_META_FILENAME = ".exp.meta.json"


@dataclass
class ExperienceMetaRecord:
    """Typed metadata for a single experience."""

    use_count: int = 0
    view_count: int = 0
    created_at: str | None = None
    last_used_at: str | None = None
    pinned: bool = False


class ExperienceMetaStore(ABC):
    """Abstract interface for experience metadata persistence."""

    @abstractmethod
    def get(self, name: str) -> ExperienceMetaRecord | None:
        """Load metadata for a single experience. Returns None if not found."""

    @abstractmethod
    def set(self, name: str, record: ExperienceMetaRecord) -> None:
        """Persist metadata for a single experience."""

    @abstractmethod
    def remove(self, name: str) -> None:
        """Delete metadata for a single experience."""

    @abstractmethod
    def migrate(self, old_name: str, new_name: str) -> None:
        """Move metadata from one experience name to another."""

    @abstractmethod
    def list_all(self) -> dict[str, ExperienceMetaRecord]:
        """Load metadata for all experiences."""

    def bump_use(self, name: str) -> ExperienceMetaRecord:
        """Increment use_count and update last_used_at. Returns the updated record."""
        record = self.get(name) or ExperienceMetaRecord()
        record.use_count += 1
        self._touch_timestamps(record)
        self.set(name, record)
        return record

    def bump_view(self, name: str) -> ExperienceMetaRecord:
        """Increment view_count and update last_used_at. Returns the updated record."""
        record = self.get(name) or ExperienceMetaRecord()
        record.view_count += 1
        self._touch_timestamps(record)
        self.set(name, record)
        return record

    def touch(self, name: str) -> ExperienceMetaRecord:
        """Update last_used_at without incrementing counters. Returns the record."""
        record = self.get(name) or ExperienceMetaRecord()
        self._touch_timestamps(record)
        self.set(name, record)
        return record

    # -- internal helper --------------------------------------------------

    @staticmethod
    def _touch_timestamps(record: ExperienceMetaRecord) -> None:
        now = datetime.now(UTC).isoformat()
        record.last_used_at = now
        if record.created_at is None:
            record.created_at = now


class PerFileExperienceMetaStore(ExperienceMetaStore):
    """Store metadata as ``{exp_root}/{name}/.exp.meta.json`` per experience.

    No memory cache — every operation reads/writes the file directly.
    Accepts ``Path | Callable[[], Path]`` for workspace-safe path resolution.
    """

    def __init__(self, exp_root: Path | Callable[[], Path]) -> None:
        self._get_root = exp_root if callable(exp_root) else lambda: exp_root

    @property
    def _root(self) -> Path:
        return self._get_root().resolve()

    # -- ExperienceMetaStore interface ------------------------------------

    def get(self, name: str) -> ExperienceMetaRecord | None:
        path = self._meta_path(name)
        if not path.exists():
            return None
        return self._read_file(path)

    def set(self, name: str, record: ExperienceMetaRecord) -> None:
        path = self._meta_path(name)
        self._write_file(path, record)

    def remove(self, name: str) -> None:
        path = self._meta_path(name)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                logger.debug("Failed to remove meta file: %s", path, exc_info=True)

    def migrate(self, old_name: str, new_name: str) -> None:
        record = self.get(old_name)
        if record is not None:
            self.set(new_name, record)
            self.remove(old_name)

    def list_all(self) -> dict[str, ExperienceMetaRecord]:
        root = self._root
        if not root.exists():
            return {}
        result: dict[str, ExperienceMetaRecord] = {}
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            meta_path = entry / _META_FILENAME
            if not meta_path.exists():
                continue
            record = self._read_file(meta_path)
            if record is not None:
                result[entry.name] = record
        return result

    # -- internal ---------------------------------------------------------

    def _meta_path(self, name: str) -> Path:
        return self._root / name / _META_FILENAME

    @staticmethod
    def _read_file(path: Path) -> ExperienceMetaRecord | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            return ExperienceMetaRecord(
                use_count=int(raw.get("use_count", 0)),
                view_count=int(raw.get("view_count", 0)),
                created_at=raw.get("created_at"),
                last_used_at=raw.get("last_used_at"),
                pinned=bool(raw.get("pinned", False)),
            )
        except Exception:
            logger.debug("Failed to read meta file: %s", path, exc_info=True)
            return None

    @staticmethod
    def _write_file(path: Path, record: ExperienceMetaRecord) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(record)
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent),
                prefix=".meta_",
                suffix=".tmp",
            )
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                    f.flush()
                safe_atomic_replace(Path(tmp), path)
            except BaseException:
                with contextlib.suppress(OSError):
                    Path(tmp).unlink()
                raise
        except Exception:
            logger.debug("Failed to write meta file: %s", path, exc_info=True)
