"""Canonical persistence identity for scoped records."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.utils.canonical_json import canonical_json

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
