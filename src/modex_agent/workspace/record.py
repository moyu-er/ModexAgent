"""WorkspaceRecord — persisted metadata for a known workspace (T14).

Frozen Pydantic model carrying the identity + metadata the
:class:`modex_agent.workspace.registry.WorkspaceRegistryStore` persists for each
known workspace.  Replaces the bare ``list[Path]`` of the legacy
``RegistryStore`` interface with a structured record that carries
``workspace_id``, ``display_name``, timestamps, ``is_home``, and arbitrary
``metadata_json``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceRecord(BaseModel):
    """Persisted metadata for one known workspace.

    Frozen Pydantic model.  ``target_path`` is the natural key (the resolved
    absolute path as a string).  Stores carry at most one record per
    ``target_path``; :meth:`upsert_workspace` replaces the existing record on
    key collision.

    Fields:
        workspace_id: Stable identifier (UUID) for the workspace.
        target_path: Resolved absolute path (string) — the natural key.
        display_name: Optional human-readable name for UI display.
        created_at: Unix-epoch millisecond timestamp of first registration.
        last_active: Unix-epoch millisecond timestamp of most recent activity.
        is_home: ``True`` for the implicit home workspace.
        metadata_json: Arbitrary extension metadata (origin, tags, …).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: str
    target_path: str
    display_name: str | None = None
    created_at: int
    last_active: int
    is_home: bool = False
    metadata_json: dict[str, Any] = Field(default_factory=dict)
