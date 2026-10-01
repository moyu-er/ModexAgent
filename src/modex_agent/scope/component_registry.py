"""ComponentRegistry — global singleton store for component factories.

The registry stores ``ComponentFactory`` instances keyed by
``(ComponentSlot, name)`` and provides typed resolution for namespace
models and ``TypedBundle`` accessors. Compile-time schema ownership
lives in scope (W3a): the scope compiler and seam consume the registry
type directly; the plugins package keeps the discovery/loader
machinery and registers into it downward.

Design constraints (SPEC §4.2, §10.1, §11):
- **Factories only, no instances** — the registry holds factories, not
  component instances. Instances are created per-assembly via
  ``factory.create(config, ctx)``.
- **No KVStore ownership** — ``resolve_bundle`` receives the kv_store
  from the caller (workspace-scoped). The registry itself holds no
  workspace state.
- **No hot-plug** — there is no ``remove``/``unregister`` API.
  Components are registered at startup and survive until process exit
  (SPEC §11 assumption 1: "restart-effective").
- **Scope-forwarded keys** — ``TypedBundle`` forwards the ``RecordScope``
  to the underlying ``KVStore`` by encoding ``scope.canonical()`` as a
  key prefix (the ``KVStore`` ABC methods do not accept a scope
  parameter; the prefix is the forwarding mechanism).

``resolve_namespace_model`` special case (SPEC §10.1):
  The ``DATA_NAMESPACE`` slot stores ``SimpleFactory`` instances whose
  wrapped ``instance`` is the model CLASS itself (``type[BaseModel]``),
  not a model instance. This lets the graph state_schema compiler (task
  26) read the model class without calling ``factory.create()`` (which
  requires an ``AssemblyContext``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum, nonmember
from types import MappingProxyType
from typing import cast

from pydantic import BaseModel

from modex_agent.core.scope import RecordScope
from modex_agent.core.stores import KVStore
from modex_agent.scope.capability import Capability
from modex_agent.scope.components import ComponentFactory, ComponentSlot

__all__ = [
    "ComponentNotFoundError",
    "ComponentRegistry",
    "PluginSource",
    "TypedBundle",
]

logger = logging.getLogger(__name__)


class PluginSource(StrEnum):
    """The four plugin discovery sources (SPEC §4.1/§4.5).

    Attribution carrier for ``ComponentRegistry.register(source=...)`` and
    ``ComponentRegistry.registration_source()``: a same-source duplicate
    ``(slot, name)`` raises ``ValueError`` (a packaging/config error); a
    cross-source duplicate is resolved by :attr:`SOURCE_PRIORITY` rank —
    user > project > entry_points > bundled, nearest-to-user wins (SPEC
    §3.5 O2, ADR-0042). A ``None`` source (not a member) attributes a
    direct, loader-less registration, which preempts any source.
    """

    BUNDLED = "bundled"
    PROJECT = "project"
    USER = "user"
    ENTRY_POINTS = "entry_points"

    #: Explicit cross-source priority table (SPEC §3.5 O2): the higher
    #: rank wins a ``(slot, name)`` collision registered from two
    #: different sources. Read-only; consumers compare ranks and never
    #: assume discovery order decides the winner. ``nonmember`` keeps
    #: this class attribute out of the enum member set.
    SOURCE_PRIORITY = nonmember(
        MappingProxyType(
            {
                BUNDLED: 0,
                ENTRY_POINTS: 1,
                PROJECT: 2,
                USER: 3,
            }
        )
    )

# Null-byte separator between scope canonical and user key.
# Safe in JSON string values (encoded as \u0000) and filesystem-safe
# (DefaultScopedStorage stores keys inside kv.json, not as filenames).
_SEP = "\x00"


def _scoped_key(key: str, scope: RecordScope) -> str:
    """Combine ``scope.canonical()`` and ``key`` into a single kv_store key.

    ``canonical()`` produces deterministic sorted JSON, so two
    ``RecordScope`` instances with the same non-``None`` fields produce
    the same prefix — enabling scope-keyed isolation on a shared
    ``KVStore`` instance.
    """
    return f"{scope.canonical()}{_SEP}{key}"


def _scope_prefix(scope: RecordScope) -> str:
    """Prefix for ``list_keys`` — all keys belonging to *scope*."""
    return f"{scope.canonical()}{_SEP}"


# ---------------------------------------------------------------------------
# ComponentNotFoundError
# ---------------------------------------------------------------------------


class ComponentNotFoundError(Exception):
    """Raised by ``ComponentRegistry.resolve`` when a name is absent in a slot.

    Carries both the looked-up ``name`` and the ``ComponentSlot`` so callers
    can produce actionable diagnostics (e.g. "component 'foo' missing from
    slot 'tool' — registered plugins: [...]").
    """

    def __init__(self, name: str, slot: ComponentSlot) -> None:
        self.name = name
        self.slot = slot
        super().__init__(f"Component {name!r} not found in slot {slot.value!r}")


# ---------------------------------------------------------------------------
# ComponentRegistry
# ---------------------------------------------------------------------------


class ComponentRegistry:
    """Global factory store for the 10-slot component system.

    Internal storage: ``dict[ComponentSlot, dict[str, ComponentFactory]]``.
    The outer dict maps each ``ComponentSlot`` to a name→factory map. Slots
    that have never been registered are simply absent (``resolve`` raises
    ``ComponentNotFoundError`` for both unknown slots and unknown names).

    The registry stores ONLY factories — never component instances and
    never ``KVStore`` instances. Workspace-scoped state (kv_stores,
    assembled components) lives in the caller; the registry is a
    process-wide singleton that survives workspace eviction.
    """

    def __init__(self) -> None:
        self._factories: dict[ComponentSlot, dict[str, ComponentFactory]] = {}
        self._sources: dict[ComponentSlot, dict[str, PluginSource | None]] = {}

    def register(
        self,
        slot: ComponentSlot,
        name: str,
        factory: ComponentFactory,
        *,
        source: PluginSource | None = None,
        overwrite: bool = False,
    ) -> None:
        """Register *factory* under ``(slot, name)``.

        Same slot+name with ``overwrite=False`` raises ``ValueError`` —
        the duplicate-name guard for direct registrations (SPEC §4.1).
        The loader's flush path never relies on that guard for
        cross-source duplicates: it checks :meth:`registration_source`
        first, resolves by source priority (SPEC §3.5 O2 — a
        higher-priority source re-registers through ``overwrite=True``,
        a lower-priority source is skipped), and raises for a
        same-source duplicate before calling this method.

        ``source`` attributes the registration to a discovery source
        (a :class:`PluginSource` value; ``None`` for direct
        registrations) — read back via :meth:`registration_source`.

        ``overwrite=True`` replaces any existing factory AND its source
        attribution. The loader's source-priority override at startup is
        the one production use — there is still no hot-plug (components
        are registered at boot and survive until process exit).
        """
        slot_map = self._factories.setdefault(slot, {})
        if name in slot_map and not overwrite:
            raise ValueError(f"Component {name!r} already registered in slot {slot.value!r}")
        slot_map[name] = factory
        self._sources.setdefault(slot, {})[name] = source

    def registration_source(self, slot: ComponentSlot, name: str) -> PluginSource | None:
        """Return the discovery source that registered ``(slot, name)``.

        ``None`` when the name is not registered in *slot*, or when it was
        registered directly without source attribution.
        """
        if name not in self._factories.get(slot, {}):
            return None
        return self._sources.get(slot, {}).get(name)

    def resolve(self, slot: ComponentSlot, name: str) -> ComponentFactory:
        """Return the factory registered under ``(slot, name)``.

        Raises ``ComponentNotFoundError`` if the slot has no factories or
        *name* is not in the slot.
        """
        slot_map = self._factories.get(slot)
        if slot_map is None or name not in slot_map:
            raise ComponentNotFoundError(name, slot)
        return slot_map[name]

    def names(self, slot: ComponentSlot) -> tuple[str, ...]:
        """Return registered component names for *slot* in deterministic order."""
        return tuple(sorted(self._factories.get(slot, {})))

    def factories(self, slot: ComponentSlot) -> Mapping[str, ComponentFactory]:
        """Return the registered factories for *slot* as an immutable view.

        The whole-slot read face for cross-module consumers that iterate a
        slot's factories (e.g. deriving the runtime strategy registry from
        the EXECUTION_STRATEGY slot) — they read this view instead of the
        private storage. Never registered slots yield an empty mapping.
        """
        return MappingProxyType(self._factories.get(slot, {}))

    def resolve_capability(self, name: str) -> Capability:
        """Return the capability INSTANCE registered under ``CAPABILITY``/``name``.

        The CAPABILITY slot stores capability instances, not factories
        (SPEC §4) — the registry's ``ComponentFactory``-typed store face
        predates the 11th slot. This accessor is the ONE read-side
        convergence point for that representation mismatch (the loader's
        flush site is the write-side counterpart): compile-time consumers
        get a typed capability instead of scattering their own casts.

        Raises ``ComponentNotFoundError`` when *name* is not registered.
        """
        return cast("Capability", self.resolve(ComponentSlot.CAPABILITY, name))

# ---------------------------------------------------------------------------
# TypedBundle — typed KVStore accessor
# ---------------------------------------------------------------------------


class TypedBundle[T: BaseModel]:
    """Typed accessor over a ``KVStore`` for one plugin namespace.

    Serializes values via ``model_dump_json()`` and deserializes via
    ``model_validate_json()`` (rule 13: serialization boundaries go
    through Pydantic). The ``scope`` parameter is forwarded to the
    ``KVStore`` by encoding ``scope.canonical()`` as a key prefix — the
    ``KVStore`` ABC methods do not accept a scope parameter, so the
    prefix is the forwarding mechanism. This enables a single
    ``KVStore`` instance to hold data for multiple scopes in isolated
    keyspaces.

    The bundle does NOT own the kv_store — it is passed in by the
    caller and may be shared across bundles for different namespaces.
    """

    def __init__(
        self,
        namespace: str,
        model: type[T],
        kv_store: KVStore,
    ) -> None:
        self._namespace = namespace
        self._model = model
        self._kv_store = kv_store

    async def get(self, key: str, scope: RecordScope) -> T | None:
        """Read and deserialize *key* under *scope*.

        Returns ``None`` if the key does not exist in this scope.
        """
        data = await self._kv_store.get(_scoped_key(key, scope))
        if data is None:
            return None
        return self._model.model_validate_json(data)

    async def set(self, key: str, value: T, scope: RecordScope) -> None:
        """Serialize *value* and write it under ``(key, scope)``."""
        await self._kv_store.set(_scoped_key(key, scope), value.model_dump_json())

    async def list_keys(self, scope: RecordScope) -> list[str]:
        """Return user-level keys for *scope* (scope prefix stripped)."""
        prefix = _scope_prefix(scope)
        raw_keys = await self._kv_store.list_keys(prefix)
        return [k.removeprefix(prefix) for k in raw_keys]

    async def delete(self, key: str, scope: RecordScope) -> None:
        """Delete *key* under *scope*."""
        await self._kv_store.delete(_scoped_key(key, scope))
