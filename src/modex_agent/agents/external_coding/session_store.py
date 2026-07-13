"""`ExternalSessionStore` — persistence for the modex ↔ provider session map.

Stores a flat JSON map of ``modex_session_id`` → ``SessionMapEntry`` at
``<workdir>/.modex/external/session-map.json``. The file is rebuilt
atomically on every commit (write-tmp + ``os.replace``) so a torn-write
leaves the previous version intact.

Process-internal serialisation is provided by an `asyncio.Lock`. The
harness passes one in (or relies on the per-instance lock created in
`__init__`) so two concurrent turns of the same modex session cannot
race the map. Cross-process safety is guaranteed by atomic rename plus
the single-writer pattern: only the harness writes this file; the
provider CLI never does.

`resolve` is sync (read-only). `acommit` and `ainvalidate` are async
because they serialise against the lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .paths import ExternalPaths, ProviderKind
from .types import SessionMapEntry

__all__ = ["ExternalSessionStore"]


class ExternalSessionStore:
    """File-backed session map with atomic commits and async serialisation.

    The store is process-local state (the in-memory cache is rebuilt from
    the file on each `resolve` / `acommit` / `ainvalidate`) plus a single
    `asyncio.Lock` that guards the read-modify-write cycle. The harness
    is the only writer; the provider CLI never reads or writes this
    file.

    Args:
        paths: An `ExternalPaths` accessor that names the on-disk map.
        lock: Optional `asyncio.Lock` shared across stores for
            cross-store serialisation. Defaults to a fresh per-instance
            lock.
    """

    def __init__(self, paths: ExternalPaths, lock: asyncio.Lock | None = None) -> None:
        self._paths = paths
        self._lock = lock if lock is not None else asyncio.Lock()
        # Make sure the external root exists before we try to write to it.
        self._paths.external_root.mkdir(parents=True, exist_ok=True)

    @property
    def file_path(self) -> Path:
        """The on-disk map location (``session-map.json``)."""
        return self._paths.session_map()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, modex_sid: str) -> tuple[str | None, bool]:
        """Look up the provider session id for a given modex session.

        Sync because it only reads. Two concurrent `resolve` calls on the
        same key are safe — the file is rewritten atomically and readers
        always see either the old or the new version.

        Args:
            modex_sid: The modex-side session identifier.

        Returns:
            ``(provider_session_id, True)`` if a non-invalidated entry
            exists for ``modex_sid``; otherwise ``(None, False)`` — the
            caller should spawn the provider fresh rather than resume.
        """
        data = self._load()
        entry = data.get(modex_sid)
        if entry is None or entry.invalidated:
            return (None, False)
        return (entry.provider_session_id, True)

    async def acommit(
        self, modex_sid: str, provider_sid: str, provider_kind: ProviderKind
    ) -> None:
        """Persist (or replace) the mapping for one modex session.

        Serialised by the store's lock so two concurrent turns of the
        same modex session cannot corrupt the file.

        Args:
            modex_sid: The modex-side session identifier.
            provider_sid: The provider-side session id minted by the
                provider on its first turn.
            provider_kind: The provider family (``pi``, ``opencode``).
        """
        async with self._lock:
            data = self._load()
            data[modex_sid] = SessionMapEntry(
                modex_session_id=modex_sid,
                provider_session_id=provider_sid,
                provider_kind=provider_kind,
                last_committed_at=datetime.now(UTC),
                invalidated=False,
            )
            self._save(data)

    async def ainvalidate(self, modex_sid: str) -> None:
        """Mark the entry as invalidated so the next resolve is fresh.

        Serialised by the store's lock.

        Args:
            modex_sid: The modex-side session identifier. A no-op if no
                entry exists yet for this session.
        """
        async with self._lock:
            data = self._load()
            entry = data.get(modex_sid)
            if entry is None:
                return
            data[modex_sid] = entry.model_copy(update={"invalidated": True})
            self._save(data)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, SessionMapEntry]:
        """Load and parse the map file; empty dict if absent or corrupt."""
        path = self._paths.session_map()
        if not path.exists():
            return {}
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt file is treated as empty — the next commit will
            # rebuild it atomically. The old (invalid) file is gone after
            # ``_save`` runs.
            return {}
        return {
            modex_sid: SessionMapEntry.model_validate(entry) for modex_sid, entry in raw.items()
        }

    def _save(self, data: dict[str, SessionMapEntry]) -> None:
        """Atomically write the map to disk via tmp-file + rename.

        Writes ``<path>.<rand>.tmp`` in the same directory as the
        target, then ``os.replace``s it onto the final path.
        ``os.replace`` is atomic on POSIX and same-volume on Windows,
        so a torn-write leaves either the old or the new file intact.
        """
        path = self._paths.session_map()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Pydantic's mode="json" serialises datetimes as ISO strings so
        # ``json.dumps`` does not need a custom encoder.
        serialisable = {sid: entry.model_dump(mode="json") for sid, entry in data.items()}
        # NamedTemporaryFile with delete=False + manual unlink is the
        # portable way to get a deterministic tmp path next to the target
        # (atomic rename across filesystems is supported on Windows only
        # when both files live on the same volume — same dir guarantees
        # that).
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(serialisable, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            # Clean up the tmp file on any failure path.
            tmp_path.unlink(missing_ok=True)
            raise
