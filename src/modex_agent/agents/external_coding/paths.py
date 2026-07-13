"""Workdir-relative path accessor for the external coding agent integration.

`ExternalPaths` is the single source of truth for everything under a workdir
(``.modex/`` layout, provider session files, outbox / inbox / result /
env-snapshot files, AGENTS.md marker). It is **not** a Pydantic model — it is
a process-local path accessor receiving an already-validated workdir from
`WorkspacePathResolver.external_workdir()`.

`ProviderKind` lives here too because it is a domain-level enum tightly
coupled to the path accessor (each value maps to a session-file suffix).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class ProviderKind(StrEnum):
    """Coding-agent provider families supported by `ExternalCodingAgent`.

    Day-one values are ``PI`` and ``OPENCODE``. Add a new value when a new
    CLI family (Claude Code, Codex, Cursor) is integrated — paired with a
    new ``ProviderBackend`` subclass and a new ``ProviderEventParser``.
    """

    PI = "pi"
    OPENCODE = "opencode"


class ExternalPaths:
    """Workdir-relative path accessor for the ``.modex/`` layout.

    Constructor argument is an **already-validated** absolute workdir
    produced by ``WorkspacePathResolver.external_workdir()``. The workdir
    is ``Path.resolve()``-ed once in ``__init__`` so every accessor returns
    a path anchored to the same canonical root.

    All accessors are read-only ``@property`` methods except the
    ``provider_session(kind)`` method that consumes a ``ProviderKind``
    argument. Every derived path is canonical and constrained to lie
    inside the workdir.
    """

    def __init__(self, workdir: Path) -> None:
        """Initialise the path accessor.

        Args:
            workdir: Already-validated per-modex_session cwd. Resolved
                once here so all accessors share one canonical root.
        """
        self._workdir = Path(workdir).resolve()

    @property
    def workdir(self) -> Path:
        """The canonical workdir root."""
        return self._workdir

    @property
    def modex_root(self) -> Path:
        """``<workdir>/.modex`` — the harness's private data root."""
        return self._workdir / ".modex"

    @property
    def external_root(self) -> Path:
        """``<workdir>/.modex/external`` — provider-session + session-map data."""
        return self.modex_root / "external"

    @property
    def outbox(self) -> Path:
        """``<workdir>/.modex/external/outbox.jsonl`` — outbound message log."""
        return self.external_root / "outbox.jsonl"

    @property
    def inbox_snapshot(self) -> Path:
        """``<workdir>/.modex/external/inbox-snapshot.jsonl`` — last inbox read."""
        return self.external_root / "inbox-snapshot.jsonl"

    @property
    def result(self) -> Path:
        """``<workdir>/.modex/external/result.json`` — last backend result."""
        return self.external_root / "result.json"

    @property
    def env_snapshot(self) -> Path:
        """``<workdir>/.modex/external/env-snapshot.json`` — last spawned env."""
        return self.external_root / "env-snapshot.json"

    @property
    def agents_md(self) -> Path:
        """``<workdir>/AGENTS.md`` — provider-visible static runtime notes."""
        return self._workdir / "AGENTS.md"

    def provider_session(self, kind: ProviderKind) -> Path:
        """Provider session file path.

        Returns ``<workdir>/.modex/external/<kind>-session.jsonl`` — the
        single source of truth for the provider's own session file. Pi
        stores its session there as a JSONL transcript; OpenCode uses the
        same path but with its own on-disk shape.
        """
        return self.external_root / f"{kind.value}-session.jsonl"

    def session_map(self) -> Path:
        """``<workdir>/.modex/external/session-map.json``.

        Persisted map of ``modex_session_id`` ↔ ``provider_session_id``,
        owned by `ExternalSessionStore`.
        """
        return self.external_root / "session-map.json"


__all__ = ["ProviderKind", "ExternalPaths"]
