"""Persistence backend configuration (T26).

Defines the :class:`PersistenceConfig` Pydantic model that drives the
selection between the file-based stores and the SQLite-backed adapters
(T16-T25): ``backend`` names a persistence-backend bundle in the
plugin-populated registry
(:mod:`modex_agent.plugins.persistence_backends`), which owns the
closed set of valid names — the framework bundles ``"file"`` and
``"sqlite"``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PersistenceConfig(BaseModel):
    """Persistence layer configuration.

    ``backend`` is the NAME of a persistence-backend bundle registered in
    the persistence-backend registry
    (:func:`modex_agent.plugins.persistence_backends.persistence_backend_registry`)
    — the registry is the closed-set authority, so a third-party plugin
    adds a backend by registering a bundle, not by editing this config
    schema. The framework bundles ``"file"`` (the file-based stores) and
    ``"sqlite"`` (the hybrid SQLite+file layer).

    The default backend is ``"sqlite"`` so that new deployments get the
    SQLite persistence layer out of the box. Existing deployments can opt
    out by setting ``persistence.backend: file`` in ``bot_config.yml``.
    """

    model_config = ConfigDict(frozen=True)

    backend: str = "sqlite"
