"""Shared context interface for input-pipeline stages.

The framework defines the interface; concrete dependencies (transcript
stores, workspace-scoped media wiring, model-choice registries, ...) are
provided by the business-layer context (:class:`InputContext` subclass).
The members below are the ones the framework-owned stages consume (W4b);
business members stay on the business context.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from modex_agent.core.media import MediaConfig, MediaStore
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.stores import PoolRoutingStore


class InputContext(ABC):
    """Interface implemented by the business layer.

    The framework's generic stages (resolve-pool, set-channel, command
    dispatch, attachment ingest, persist-user-message, approval,
    unsupported-command) read every routing/media dependency through this
    contract; channel-specific behavior is injected as callbacks by the
    business layer.
    """

    @property
    @abstractmethod
    def default_pool(self) -> str | None:
        ...

    @property
    @abstractmethod
    def pool_session_store(self) -> PoolRoutingStore:
        """The session-prefix → pool routing store (S5 persistence owner)."""

    @property
    @abstractmethod
    def session_factory(self) -> SessionIdFactory:
        """Factory creating SessionInfo from external conversation ids."""

    @abstractmethod
    def agent_for_pool(self, pool: str) -> str:
        """The root agent name serving *pool*."""

    @abstractmethod
    def available_pools(self) -> set[str]:
        """The current set of routable pool names (re-read at call time)."""

    @abstractmethod
    def current_ws(self) -> Path:
        """The pipeline's current workspace root (fallback for routing)."""

    @abstractmethod
    def media_config_for(self, pool: str) -> MediaConfig:
        """The perception-gate config to apply for *pool*'s attachment ingest."""

    @abstractmethod
    def media_store_for(self, pool: str) -> MediaStore | None:
        """The pool-routed inbound media store, or ``None`` when the
        deployment wires no media backend (attachment ingest no-ops)."""
