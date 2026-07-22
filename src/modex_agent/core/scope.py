"""Scope abstractions for configurable isolation dimensions.

Moved from framework.memory.core.scope to core to break the core <-> memory
import cycle. These types (MemoryContext, Scope, concrete scopes) are shared
identity models used by both core.runtime_context and memory layers.

``RecordScope`` (frozen Pydantic model) carries every configurable isolation
dimension and produces a deterministic key (``canonical()``) or a filesystem
path segment (``to_path_segment(*dims)``).  The ``Scope`` ABC
(``extract(context) -> RecordScope`` + ``name``) is the single extraction
contract; the 8 concrete subclasses implement it.  :func:`scope_path_key`
derives a filesystem path segment from a ``Scope`` (using the dimensions
encoded in ``scope.name``); :func:`build_scope` constructs a ``Scope`` from
config dimension short-names.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.utils.canonical_json import canonical_json


class MemoryAgentRole(StrEnum):
    """Agent role for memory ownership and background processing."""

    MAIN = "main"
    SUBAGENT = "subagent"


class MemoryLayerName(StrEnum):
    """Canonical memory layer names used in metadata and config.

    ``CORE`` names the Core Memory layer (per ADR-0035; formerly "knowledge").
    The string value ``"core"`` is used as a dict key, a scope segment, and a
    filesystem path segment (``<root>/core/<scope_key>/`` on disk). For
    historical reasons the on-disk directory is ``core/`` rather than
    ``core_memory/`` — they refer to the same concept.
    """

    SESSION = "session"
    ARCHIVE = "archive"
    CORE = "core"
    PROVIDER = "provider"
    USER_RETENTION = "user_retention"


class MemoryContext(BaseModel):
    """统一上下文对象，包含所有可能用到的分组信息。

    Pydantic frozen model：构造时做类型校验，防止把与标注不符的值（例如把
    ``SessionInfo`` 对象塞进 ``session_id``）随意传入。``session_id`` 是会话
    标识**字符串**，不是 ``SessionInfo`` 对象。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    agent_role: str | MemoryAgentRole | None = None
    channel: str | None = None
    chat_id: str | None = None
    sender_agent: str | None = None
    receiver_agent: str | None = None

    def with_defaults(self, **defaults: Any) -> MemoryContext:
        """Return a new MemoryContext with default values for missing fields."""
        current = {key: getattr(self, key) for key in type(self).model_fields}
        for key, default_value in defaults.items():
            if key in current and current[key] is None and default_value is not None:
                current[key] = default_value
        return type(self)(**current)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MemoryContext:
        """Restore context from persisted scope metadata.

        ``session_id`` is persisted as a plain string; no SessionInfo parsing.
        """
        if not data:
            return cls()
        kwargs = {key: data.get(key) for key in cls.model_fields}
        return cls(**kwargs)


@dataclass(frozen=True)
class ScopeRecord:
    """Recoverable metadata for a persisted memory scope."""

    scope_key: str
    layer: str | MemoryLayerName
    context: MemoryContext
    storage_path: str
    agent_role: str | MemoryAgentRole = MemoryAgentRole.MAIN
    agent_id: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


def infer_agent_role(context: MemoryContext) -> MemoryAgentRole:
    """Infer role for persisted scope metadata.

    Explicit agent_id values are preferred. Unknown contexts default to main
    because ordinary single-agent use should keep full memory behavior.
    """
    candidates = [
        context.agent_role,
        context.agent_id,
        context.sender_agent,
        context.receiver_agent,
    ]
    normalized = {str(value).lower() for value in candidates if value}
    if MemoryAgentRole.SUBAGENT.value in normalized:
        return MemoryAgentRole.SUBAGENT
    return MemoryAgentRole.MAIN


# ---------------------------------------------------------------------------
# RecordScope — structured scope dimensions (T04)
# ---------------------------------------------------------------------------

#: Maps short dimension names (used by config, ``Scope.name``, ``build_scope``)
#: to the corresponding ``RecordScope`` field name.  Used by
#: ``RecordScope.to_path_segment``.
_DIMENSION_FIELDS: dict[str, str] = {
    "pool": "pool",
    "workspace": "workspace_id",
    "session": "session_id",
    "session_prefix": "session_prefix",
    "agent": "agent_id",
    "agent_role": "agent_role",
    "user": "user_id",
    "tenant": "tenant_id",
    "channel": "channel",
    "chat": "chat_id",
    "invocation": "invocation_id",
    "parent_session": "parent_session_id",
}


_SCOPE_TYPE_KEY: str = "__scope_type__"


class RecordScope(BaseModel):
    """Structured scope dimensions for a memory record.

    Frozen Pydantic model carrying every configurable isolation dimension.
    A structured object that can produce:

    - ``canonical()`` — deterministic JSON for DB scope_key uniqueness.
    - ``to_path_segment(*dimensions)`` — file-system path segment for the
      selected dimensions (``None`` values become ``"default"``).
    - ``merge(other)`` — combine two records (``other``'s non-``None`` fields
      override ``self``'s), used by ``CompositeScope.extract``.

    Subclasses (e.g. ``BotRecordScope``) auto-register via
    :meth:`__init_subclass__` and stamp their extra non-``None`` field names
    (sorted, comma-joined) into ``canonical()`` output under
    ``__scope_type__``. :meth:`from_canonical` reads that stamp and dispatches
    to the registered subclass whose extra field set matches, so subclass
    instances round-trip with full fidelity — framework code never imports
    business subclasses yet can restore them. The stamp is content-based, not
    class-name-based: two structurally identical subclasses (same extra field
    names) in different modules produce identical canonical JSON, enabling
    cross-process scope_key matching (ADR-0028 §3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    _SCOPE_TYPE_REGISTRY: ClassVar[dict[frozenset[str], type[RecordScope]]] = {}

    workspace_id: str | None = None
    session_id: str | None = None
    session_prefix: str | None = None
    agent_id: str | None = None
    agent_role: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    channel: str | None = None
    chat_id: str | None = None
    invocation_id: str | None = None
    parent_session_id: str | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Index subclasses by their extra-field signature (frozenset of field
        # names beyond the base RecordScope) so from_canonical can dispatch in
        # O(1) by stamp. Uses __annotations__ (available at __init_subclass__
        # time) rather than model_fields (not yet populated by Pydantic).
        # Multiple subclasses with the same signature share a slot —
        # last-registered wins, which is fine because content-based stamping
        # treats them as interchangeable (ADR-0028 §3).
        base_annotations = set(RecordScope.__annotations__)
        extra = frozenset(cls.__annotations__) - base_annotations
        RecordScope._SCOPE_TYPE_REGISTRY[extra] = cls

    def canonical(self) -> str:
        """Deterministic JSON representation of non-``None`` dimensions.

        Uses :func:`canonical_json` so the output is byte-stable regardless of
        field construction order — suitable for DB scope_key uniqueness
        constraints. Subclass instances with extra non-``None`` fields are
        stamped with ``__scope_type__`` whose value is the sorted, comma-joined
        names of those extra fields (content-based, not class-name-based) so
        :meth:`from_canonical` can restore a matching subclass on read — and so
        structurally identical subclasses in different processes (e.g. the
        bot's ``BotRecordScope`` and modexctl's ``_PoolScopedRecordScope``)
        produce identical scope_keys. A subclass instance whose extra fields
        are all ``None`` produces identical canonical JSON to a base
        :class:`RecordScope` with the same framework fields — no stamp, no
        divergence (per ADR-0028 §3).
        """
        payload: dict[str, Any] = self.model_dump(exclude_none=True)
        extra_non_none = {
            k: v for k, v in payload.items() if k not in RecordScope.model_fields
        }
        if extra_non_none:
            payload[_SCOPE_TYPE_KEY] = ",".join(sorted(extra_non_none))
        return canonical_json(payload)

    @classmethod
    def from_canonical(cls, canonical_json_str: str) -> RecordScope:
        """Parse a ``scope_key`` produced by :meth:`canonical`.

        If the JSON carries a ``__scope_type__`` stamp (sorted, comma-joined
        extra field names), the registered subclass whose extra-field signature
        matches is used to validate the payload, preserving subclass-specific
        fields. Otherwise the payload is validated against the base class with
        unknown fields dropped — preserving framework-owned fields for legacy
        scope_keys written before this stamp existed, and for stamps naming
        field sets no registered subclass declares.
        """
        try:
            data = json.loads(canonical_json_str)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"Invalid scope_key JSON: {canonical_json_str!r}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"scope_key JSON must be an object: {canonical_json_str!r}")
        stamp = data.pop(_SCOPE_TYPE_KEY, None)
        if isinstance(stamp, str) and stamp:
            signature = frozenset(name for name in stamp.split(",") if name)
            target = RecordScope._SCOPE_TYPE_REGISTRY.get(signature)
            if target is not None:
                return target.model_validate(data)
        known = {k: v for k, v in data.items() if k in cls.model_fields}
        return cls.model_validate(known)

    def to_path_segment(self, *dimensions: str) -> str:
        """Build a path segment string from the selected dimensions.

        Each dimension is a short name (e.g. ``"user"``, ``"session"``) mapped
        to a ``RecordScope`` field via ``_DIMENSION_FIELDS``.  ``None`` values
        (including fields absent on a subclass-less base instance, e.g.
        ``"pool"`` after ADR-0028) are rendered as ``"default"``.  Dimensions
        are joined with ``":"``.

        With zero dimensions the result is ``""`` — mirroring
        :class:`GlobalScope`'s empty-key behavior.
        """
        if not dimensions:
            return ""
        parts: list[str] = []
        for dim in dimensions:
            field_name = _DIMENSION_FIELDS.get(dim)
            if field_name is None:
                raise ValueError(f"Unknown scope dimension: {dim!r}")
            value = getattr(self, field_name, None)
            parts.append(value if value is not None else "default")
        return ":".join(parts)

    def merge(self, other: RecordScope) -> RecordScope:
        """Return a new ``RecordScope`` merging ``self`` with ``other``.

        For each field: ``other``'s non-``None`` value overrides ``self``'s;
        when ``other``'s field is ``None`` ``self``'s value is preserved.
        Used by :class:`CompositeScope.extract` to combine sub-scope records.

        Fields present on ``self`` but absent on ``other`` (e.g. when ``self``
        is a subclass with extra dimensions such as ``BotRecordScope.pool``)
        are treated as ``None`` on ``other`` — preserving ``self``'s value.
        """
        merged: dict[str, str | None] = {}
        for field_name in type(self).model_fields:
            other_val = getattr(other, field_name, None)
            self_val = getattr(self, field_name)
            merged[field_name] = other_val if other_val is not None else self_val
        return type(self)(**merged)


# ---------------------------------------------------------------------------
# Scope — extraction ABC
# ---------------------------------------------------------------------------


class Scope(ABC):
    """Structured scope extraction contract.

    Each concrete subclass extracts a :class:`RecordScope` from a
    :class:`MemoryContext`.  ``CompositeScope.extract`` merges sub-scope
    records via :meth:`RecordScope.merge`.

    Consumers derive either a deterministic DB key via
    ``scope.extract(context).canonical()`` or a filesystem path segment via
    :func:`scope_path_key` (which calls ``to_path_segment`` with the
    dimensions encoded in ``scope.name``).
    """

    @abstractmethod
    def extract(self, context: MemoryContext) -> RecordScope:
        """Extract a :class:`RecordScope` from *context*."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Short dimension name, used for debugging and metadata."""
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


# ---------------------------------------------------------------------------
# Concrete Scope subclasses
# ---------------------------------------------------------------------------


class SessionScope(Scope):
    """按会话分组。"""

    @property
    def name(self) -> str:
        return "session"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(session_id=context.session_id)


class UserScope(Scope):
    """按用户分组。"""

    @property
    def name(self) -> str:
        return "user"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(user_id=context.user_id)


class TenantScope(Scope):
    """按租户分组。"""

    @property
    def name(self) -> str:
        return "tenant"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(tenant_id=context.tenant_id)


class AgentScope(Scope):
    """按 Agent 类型分组。"""

    @property
    def name(self) -> str:
        return "agent:agent_role"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(agent_id=context.agent_id, agent_role=context.agent_role)


class ChannelScope(Scope):
    """按频道分组（例如 IM 平台中的 channel）。"""

    @property
    def name(self) -> str:
        return "channel"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(channel=context.channel)


class ChatScope(Scope):
    """按聊天群组分组（例如 QQ 群、微信群）。"""

    @property
    def name(self) -> str:
        return "chat"

    def extract(self, context: MemoryContext) -> RecordScope:
        return RecordScope(chat_id=context.chat_id)


class GlobalScope(Scope):
    """全局共享，无视任何上下文字段。

    Returns an empty path segment so the storage path has no user-level
    subdirectory in single-user mode: ``archive/`` instead of
    ``archive/global/``.
    """

    @property
    def name(self) -> str:
        return "global"

    def extract(self, context: MemoryContext) -> RecordScope:
        _ = context
        return RecordScope()


class CompositeScope(Scope):
    """组合多个 Scope，生成复合分组键。

    例如 CompositeScope(TenantScope(), UserScope()) 会生成 "tenant_id:user_id"。

    ``extract`` merges sub-scope records via :meth:`RecordScope.merge`.
    """

    def __init__(self, *scopes: Scope) -> None:
        self.scopes = scopes

    def extract(self, context: MemoryContext) -> RecordScope:
        record = RecordScope()
        for scope in self.scopes:
            record = record.merge(scope.extract(context))
        return record

    @property
    def name(self) -> str:
        return ":".join(s.name for s in self.scopes)

    def __repr__(self) -> str:
        return f"CompositeScope({', '.join(s.name for s in self.scopes)})"


# ---------------------------------------------------------------------------
# build_scope factory
# ---------------------------------------------------------------------------

#: Maps short dimension names to concrete ``Scope`` classes for
#: :func:`build_scope`.  Only the 7 leaf-scope dimensions that have a
#: corresponding ``Scope`` subclass are listed here; the remaining
#: ``RecordScope`` fields (pool, workspace_id, session_prefix, invocation_id,
#: parent_session_id) are populated by future Scope subclasses.
_DIMENSION_SCOPES: dict[str, type[Scope]] = {
    "session": SessionScope,
    "user": UserScope,
    "tenant": TenantScope,
    "agent": AgentScope,
    "channel": ChannelScope,
    "chat": ChatScope,
    "global": GlobalScope,
}


def build_scope(dims: list[str] | str) -> Scope:
    """Build a :class:`Scope` from dimension short-names.

    A single string is auto-wrapped into a one-element list (``build_scope("user")``
    is equivalent to ``build_scope(["user"])``).

    - Empty list → :class:`GlobalScope`.
    - Single dimension → the corresponding leaf ``Scope``.
    - Multiple dimensions → :class:`CompositeScope` preserving order.
    """
    if isinstance(dims, str):
        dims = [dims]
    if len(dims) == 0:
        return GlobalScope()

    resolved: list[Scope] = []
    for dim in dims:
        cls = _DIMENSION_SCOPES.get(dim)
        if cls is None:
            raise ValueError(f"Unknown scope dimension: {dim!r}")
        resolved.append(cls())

    if len(resolved) == 1:
        return resolved[0]
    return CompositeScope(*resolved)


# ---------------------------------------------------------------------------
# scope_path_key — filesystem path segment from a Scope
# ---------------------------------------------------------------------------


def scope_path_key(scope: Scope, context: MemoryContext) -> str:
    """Filesystem path segment for *scope* applied to *context*.

    Extracts a :class:`RecordScope` and renders the configured dimensions
    (those encoded in ``scope.name`` that are registered in
    ``_DIMENSION_FIELDS``) via :meth:`RecordScope.to_path_segment`.
    Dimensions not registered as fields — notably ``"global"`` — contribute
    nothing, so :class:`GlobalScope` yields ``""`` (no subdirectory).

    Use ``scope.extract(context).canonical()`` instead when the key is a dict
    / DB lookup key rather than a filesystem path.
    """
    record = scope.extract(context)
    dims = [d for d in scope.name.split(":") if d in _DIMENSION_FIELDS]
    return record.to_path_segment(*dims)
