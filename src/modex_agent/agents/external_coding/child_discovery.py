"""Provider-neutral sink for runtime subagent session discovery.

External coding providers (opencode, Pi, future Claude Code/Codex) discover
internal subagents at runtime — different from native subagents registered at
pool materialization. :class:`ChildSessionDiscoverySink` isolates that
discovery mechanism so the ``ExternalCodingAgent`` harness and the persistence
layer (``ExternalSessionMapStore``) stay provider-neutral.

The contract is intentionally minimal: two methods, one async
(:meth:`~ChildSessionDiscoverySink.on_child_discovered`) for the registration
side-effect, one sync
(:meth:`~ChildSessionDiscoverySink.resolve_child_modex_session_id`) for the
deterministic modex session_id derivation. The sync method is callable inside
the discovery callback to populate the routing mapping and create the child
emitter *before* the first child emission is handled — no await race window.

Determinism: ``encode_snowflake`` (see ``modex_agent.core.session_id``)
guarantees the same ``provider_child_session_id`` always maps to the same
modex session_id. The sink is the contract; the harness wires a concrete
implementation that calls ``encode_snowflake`` and persists the mapping
through ``ExternalSessionMapStore``.

Provider neutrality: this module imports no provider-specific types. A new
provider family (Claude Code, Codex, Cursor) implements this ABC; the harness
and persistence layer are unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ...core.session_id import SessionIdFactory
from ...core.session_registry import SessionRegistry
from .paths import ProviderKind
from .session_store import ExternalSessionMapStore

__all__ = ["ChildSessionDiscoverySink", "ExternalChildSessionDiscoverySink"]


class ChildSessionDiscoverySink(ABC):
    """Provider-neutral sink for runtime subagent session discovery.

    External coding providers (opencode, Pi, future Claude Code/Codex)
    discover internal subagents at runtime — different from native
    subagents registered at pool materialization. This ABC isolates the
    discovery mechanism so the Agent harness and persistence layer stay
    provider-neutral.
    """

    @abstractmethod
    async def on_child_discovered(
        self,
        provider_child_session_id: str,
        parent_modex_session_id: str,
        provider_agent_type: str | None = None,
    ) -> str:
        """Called when a new child session is first seen.

        Returns the ModexAgent-side session_id for the child
        (deterministic: same provider_child_session_id always maps
        to the same modex session_id via encode_snowflake).
        """
        ...

    @abstractmethod
    def resolve_child_modex_session_id(self, provider_child_session_id: str) -> str:
        """Deterministically derive the modex session_id for a provider
        child session. Always returns a non-None value — the mapping is
        deterministic via encode_snowflake, so the modex session_id can
        be computed before async registration completes. This is used
        synchronously in the discovery callback to populate the routing
        mapping and create the child emitter BEFORE the first child
        emission is handled."""
        ...


class ExternalChildSessionDiscoverySink(ChildSessionDiscoverySink):
    """Concrete sink wired to ``SessionIdFactory`` + ``SessionRegistry`` +
    ``ExternalSessionMapStore``.

    The two ABC methods are split by side-effect, not by identity:
    ``resolve_child_modex_session_id`` is sync and side-effect-free so
    the harness can route the first child emission BEFORE the async
    ``register``/``commit`` side-effects land — no await race window.
    Both feed the same ``provider_child_session_id`` + ``fixed_agent_name``
    through :meth:`SessionIdFactory.create`, so they observe the same
    deterministic modex session_id (encode_snowflake).
    """

    def __init__(
        self,
        *,
        session_factory: SessionIdFactory,
        session_registry: SessionRegistry,
        session_map_store: ExternalSessionMapStore,
        provider_kind: ProviderKind,
        fixed_agent_name: str = "external-subagent",
    ) -> None:
        self._session_factory = session_factory
        self._session_registry = session_registry
        self._session_map_store = session_map_store
        self._provider_kind = provider_kind
        self._fixed_agent_name = fixed_agent_name

    async def on_child_discovered(
        self,
        provider_child_session_id: str,
        parent_modex_session_id: str,
        provider_agent_type: str | None = None,
    ) -> str:
        metadata: dict[str, Any] = (
            {"provider_agent": provider_agent_type} if provider_agent_type else {}
        )
        child_info = self._session_factory.create(
            agent_name=self._fixed_agent_name,
            external_id=provider_child_session_id,
            parent_session_id=parent_modex_session_id,
            metadata=metadata,
        )
        await self._session_registry.register(child_info)
        await self._session_map_store.commit(
            child_info.session_id,
            provider_child_session_id,
            self._provider_kind,
        )
        return child_info.session_id

    def resolve_child_modex_session_id(self, provider_child_session_id: str) -> str:
        return self._session_factory.create(
            agent_name=self._fixed_agent_name,
            external_id=provider_child_session_id,
        ).session_id
