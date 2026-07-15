"""Persistence backend configuration (T26).

Defines the :class:`PersistenceBackend` enum and :class:`PersistenceConfig`
Pydantic model that drive IOC factory selection between the legacy file-based
stores and the new SQLite-backed adapters (T16-T25).

When ``backend == PersistenceBackend.SQLITE`` the bot IOC factories construct
SQLite adapters bound to the workspace's
:class:`~modex_agent.persistence.managers.WorkspacePersistenceManager`
(per-workspace DB) and the service-level
:class:`~modex_agent.persistence.managers.RegistryPersistenceManager`
(global registry DB). When ``backend == PersistenceBackend.FILE`` the
existing file-based implementations are used unchanged.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class PersistenceBackend(StrEnum):
    """Storage backend selector for runtime state and memory stores."""

    FILE = "file"
    SQLITE = "sqlite"


class PersistenceConfig(BaseModel):
    """Persistence layer configuration.

    The default backend is :attr:`PersistenceBackend.SQLITE` so that new
    deployments get the SQLite persistence layer out of the box. Existing
    deployments can opt out by setting ``persistence.backend: file`` in
    ``bot_config.yml``.
    """

    model_config = ConfigDict(frozen=True)

    backend: PersistenceBackend = PersistenceBackend.SQLITE
