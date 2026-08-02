"""Workdir-relative path accessor for the external coding agent integration.

``ExternalPaths`` is the single source of truth for everything under a workdir
(``.modex/`` layout, provider session files, outbox / inbox / result /
env-snapshot files, AGENTS.md marker). It is **not** a Pydantic model — it is
a process-local path accessor receiving an already-validated workdir from
``WorkspacePathResolver.external_workdir()``.

``ProviderKind`` is re-exported from ``modex_agent.core.constants`` for
backward compatibility — it was moved there to break the
``multi_agent.descriptor → agents.external.paths`` eager import cycle (the
canonical home is now next to ``ExecutionStrategyKind``, which it is paired
with in pool-config validation).
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.constants import ProviderKind  # noqa: F401 — re-export


def sanitize_session_id(session_id: str) -> str:
    """Sanitize a provider session ID for use as a filename.

    Replaces path separators and traversal sequences with underscores.
    The same logic is duplicated in ``modexctl/main.py:_read_env_snapshot``
    because modexctl is a standalone CLI that does not import the framework.
    """
    return session_id.replace("/", "_").replace("\\", "_").replace("..", "_")


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
    def inbox_snapshot(self) -> Path:
        """``<workdir>/.modex/external/inbox-snapshot.jsonl`` — last inbox read."""
        return self.external_root / "inbox-snapshot.jsonl"

    @property
    def env_snapshots_dir(self) -> Path:
        """``<workdir>/.modex/external/env-snapshots`` — per-provider-session env snapshot directory.

        Each file is ``<provider_session_id>.json`` containing the MODEX_* vars
        for that specific opencode session. modexctl reads the file matching
        the OPENCODE_SESSION_ID injected by the shell.env plugin.
        """
        return self.external_root / "env-snapshots"

    def env_snapshot_for_session(self, provider_session_id: str) -> Path:
        """``<workdir>/.modex/external/env-snapshots/<provider_session_id>.json``.

        Per-provider-session env snapshot file. Written by ExternalAgent
        on each turn (main session) and on child discovery (subagent session).
        Read by modexctl when OPENCODE_SESSION_ID is set in the env.
        """
        safe_sid = sanitize_session_id(provider_session_id)
        return self.env_snapshots_dir / f"{safe_sid}.json"

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
        owned by an `ExternalSessionMapStore` adapter.
        """
        return self.external_root / "session-map.json"


__all__ = ["ProviderKind", "ExternalPaths"]
