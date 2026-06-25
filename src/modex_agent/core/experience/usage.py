from __future__ import annotations

import json
import logging
import tempfile
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modex_agent.core.utils import safe_atomic_replace

logger = logging.getLogger(__name__)


class ExperienceUsageTracker:
    """Sidecar JSON file tracking per-experience usage statistics.

    Mutations are accumulated in memory and flushed to disk on explicit
    :meth:`flush` or at next load-all. All counter bumps are best-effort —
    a broken sidecar never breaks the caller.
    """

    def __init__(self, usage_path: Path) -> None:
        warnings.warn(
            "ExperienceUsageTracker is deprecated — use PerFileExperienceMetaStore instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self._path = usage_path
        self._cache: dict[str, dict[str, Any]] | None = None

    # -- public API -------------------------------------------------------

    def bump_use(self, name: str) -> None:
        self._mutate(name, lambda r: r.update(use_count=r.get("use_count", 0) + 1))

    def bump_view(self, name: str) -> None:
        self._mutate(name, lambda r: r.update(view_count=r.get("view_count", 0) + 1))

    def update_timestamp(self, name: str) -> None:
        now = datetime.now(UTC).isoformat()

        def _apply(r: dict[str, Any]) -> None:
            r["last_used_at"] = now
            if "created_at" not in r:
                r["created_at"] = now

        self._mutate(name, _apply)

    def remove_record(self, name: str) -> None:
        """Remove a record from the usage sidecar."""
        try:
            data = self.load_all()
            data.pop(name, None)
        except Exception:
            logger.debug("Failed to remove usage record for %s", name, exc_info=True)

    def migrate_record(self, old_name: str, new_name: str) -> None:
        """Move a usage record from *old_name* to *new_name*.

        Used by rename_experience_dir and hook cleanup after directory renames.
        Preserves all fields unchanged.
        """
        try:
            data = self.load_all()
            record = data.pop(old_name, None)
            if record is not None:
                data[new_name] = record
        except Exception:
            logger.debug(
                "Failed to migrate usage record %s → %s",
                old_name,
                new_name,
                exc_info=True,
            )

    def update_timestamp_to(self, name: str, mtime: float) -> None:
        """Update last_used_at to a specific timestamp (from file mtime).

        Preserves existing created_at.
        """
        dt = datetime.fromtimestamp(mtime, tz=UTC)

        def _apply(r: dict[str, Any]) -> None:
            r["last_used_at"] = dt.isoformat()
            if "created_at" not in r:
                r["created_at"] = dt.isoformat()

        self._mutate(name, _apply)

    def get_record(self, name: str) -> dict[str, Any] | None:
        return self.load_all().get(name)

    def get_all_records(self) -> dict[str, dict[str, Any]]:
        return dict(self.load_all())

    def load_all(self) -> dict[str, dict[str, Any]]:
        """Return all records, loading from disk if not cached."""
        if self._cache is not None:
            return self._cache
        self._cache = self._load_all()
        return self._cache

    def flush(self) -> None:
        """Persist in-memory state to disk."""
        if self._cache is not None:
            self._save_all(self._cache)

    # -- internal ---------------------------------------------------------

    def _mutate(self, name: str, apply_fn) -> None:
        try:
            data = self.load_all()
            record = data.setdefault(name, {})
            apply_fn(record)
        except Exception:
            logger.debug("Failed to mutate usage record for %s", name, exc_info=True)

    def _load_all(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_all(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".usage_",
            suffix=".tmp",
        )
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
            safe_atomic_replace(Path(tmp), self._path)
        except BaseException:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
            raise
