"""Declarative field-mapping abstraction for SQLite adapters (ADR-0030).

Lets a SQLite adapter split a dict into typed table columns plus a residual
JSON column on write, and re-assemble the dict on read. Replaces hand-rolled
per-adapter projection logic with a single, tested codec pipeline.

Design invariants (enforced by tests):

- ``ColumnCodec.encode`` returns ``dict[str, Any]`` (not a scalar) so a single
  logical value may fan out to companion columns (e.g. ``is_content_json``).
- ``ColumnProjection.split`` removes ALL candidate keys listed on a field from
  the residual JSON — not just the one that matched. This keeps the residual
  free of any key the adapter owns, so ``assemble`` never collides with
  re-injected values.
- ``ColumnProjection.assemble`` re-injects the decoded value under the FIRST
  candidate key only. The other candidate keys are aliases for read-time
  backwards compatibility on the dict side; they do not survive a round-trip
  as separate keys.
- ``ContentCodec`` round-trips ``str``, ``list[dict]``, and ``None`` via the
  ``is_content_json`` companion column (0 = scalar/null, 1 = JSON list).
- ``decode`` operates on a *pre-sliced* row dict that contains only the
  columns this codec owns (primary + companion). :meth:`ColumnProjection.assemble`
  always pre-slices via :meth:`ColumnCodec.columns_for`; direct codec callers
  must do the same.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ColumnCodec",
    "ColumnField",
    "ColumnProjection",
    "ContentCodec",
    "IdentityCodec",
]

CONTENT_JSON_FLAG_COLUMN = "is_content_json"


class ColumnCodec(ABC):
    """Encode a dict value into one or more table columns and decode it back.

    Subclasses are stateless runtime objects (architecture rule 12) — plain
    classes, not dataclasses or BaseModels. ``encode`` returns a dict (not a
    scalar) so a codec may emit companion columns (e.g.
    ``is_content_json``) alongside the primary column. ``decode`` reads the
    relevant columns from the row dict and reconstructs the original value.
    ``columns_for`` declares which row columns this codec needs to read;
    :meth:`ColumnProjection.assemble` pre-slices the row accordingly.
    """

    @abstractmethod
    def encode(self, column: str, value: Any) -> dict[str, Any]:
        """Project *value* into one or more column→cell entries."""

    @abstractmethod
    def decode(self, columns: dict[str, Any]) -> Any:
        """Reconstruct the original value from a pre-sliced row dict."""

    @abstractmethod
    def columns_for(self, column: str) -> tuple[str, ...]:
        """Declare all row columns this codec reads (primary + companions)."""


class IdentityCodec(ColumnCodec):
    """Pass-through codec: stores the value verbatim under *column*."""

    def encode(self, column: str, value: Any) -> dict[str, Any]:
        return {column: value}

    def decode(self, columns: dict[str, Any]) -> Any:
        return next(iter(columns.values()))

    def columns_for(self, column: str) -> tuple[str, ...]:
        return (column,)


class ContentCodec(ColumnCodec):
    """Codec for the ``str``-vs-``list[dict]`` content duality.

    - ``str``        → ``{column: value, "is_content_json": 0}``
    - ``None``       → ``{column: None,  "is_content_json": 0}``
    - ``list[dict]`` → ``{column: json.dumps(value), "is_content_json": 1}``

    ``decode`` expects a slice ``{column: ..., is_content_json: ...}`` and
    parses the content via ``json.loads`` when the flag is 1.
    """

    def encode(self, column: str, value: Any) -> dict[str, Any]:
        if isinstance(value, list):
            return {column: json.dumps(value, ensure_ascii=False), CONTENT_JSON_FLAG_COLUMN: 1}
        return {column: value, CONTENT_JSON_FLAG_COLUMN: 0}

    def decode(self, columns: dict[str, Any]) -> Any:
        flag = columns.get(CONTENT_JSON_FLAG_COLUMN, 0)
        primary = _single_primary_column(columns)
        raw = columns.get(primary)
        if flag == 1:
            return json.loads(raw) if raw is not None else None
        return raw

    def columns_for(self, column: str) -> tuple[str, ...]:
        return (column, CONTENT_JSON_FLAG_COLUMN)


def _single_primary_column(columns: dict[str, Any]) -> str:
    """Return the single non-flag key in *columns*."""
    non_flag = [k for k in columns if k != CONTENT_JSON_FLAG_COLUMN]
    if len(non_flag) == 1:
        return non_flag[0]
    raise ValueError(
        "ContentCodec.decode requires a slice with exactly one primary column "
        f"besides {CONTENT_JSON_FLAG_COLUMN!r}; got {sorted(columns)!r}"
    )


class ColumnField(BaseModel):
    """Declarative mapping from one logical dict field to one table column.

    Cross-module value object (type-safety rules 10–16): frozen Pydantic
    BaseModel. Attributes:
        column: Target table column name.
        dict_keys: Candidate dict keys, in priority order. The first key
            present in the input dict supplies the value on ``split``; the
            first key is also the one used to re-inject on ``assemble``.
            All candidate keys are stripped from the residual JSON on
            ``split`` so the residual never re-introduces a mapped field.
        codec: Optional :class:`ColumnCodec`. When ``None``, identity
            pass-through is used (equivalent to :class:`IdentityCodec`).
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    column: str
    dict_keys: tuple[str, ...]
    codec: ColumnCodec | None = None


class ColumnProjection(BaseModel):
    """Split a dict into typed table columns + a residual JSON string, and
    re-assemble on read.

    Cross-module value object (type-safety rules 10–16): frozen Pydantic
    BaseModel. Attributes:
        fields: Ordered projection rules. Fields do not compete for the same
            dict key (overlapping ``dict_keys`` across fields is a
            configuration error).
        json_column: Name of the residual JSON column. Default
            ``"message_json"``. The column name itself is not emitted by
            ``split`` (the caller knows which INSERT column receives the JSON
            string); it is stored here for adapter self-description.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    fields: tuple[ColumnField, ...]
    json_column: str = "message_json"

    def split(self, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Project *data* into ``(column_values, residual_json)``.

        For each field, the first candidate key present in *data* supplies
        the value; the value is encoded via the field's codec (or identity)
        and merged into the column dict. ALL candidate keys of every matched
        field are removed from the residual. Unmatched fields emit nothing.
        """
        columns: dict[str, Any] = {}
        residual: dict[str, Any] = dict(data)

        for fld in self.fields:
            hit_key = next((k for k in fld.dict_keys if k in residual), None)
            if hit_key is None:
                continue
            value = residual[hit_key]
            codec = fld.codec if fld.codec is not None else IdentityCodec()
            columns.update(codec.encode(fld.column, value))
            for k in fld.dict_keys:
                residual.pop(k, None)

        return columns, _dump_json(residual)

    def assemble(self, columns: dict[str, Any], json_str: str) -> dict[str, Any]:
        """Reconstruct the original dict from *columns* + *json_str**.

        For each field whose primary column is present in *columns*, the codec
        decodes a pre-sliced row (via :meth:`ColumnCodec.columns_for`) and the
        result is re-injected under the field's first candidate key.
        Companion columns (e.g. ``is_content_json``) are consumed by the codec
        and never appear in the output. The residual JSON is then merged in;
        because ``split`` stripped all candidate keys, there is no key
        collision.
        """
        out: dict[str, Any] = {}

        for fld in self.fields:
            if fld.column not in columns:
                continue
            codec = fld.codec if fld.codec is not None else IdentityCodec()
            slice_ = {col: columns[col] for col in codec.columns_for(fld.column) if col in columns}
            decoded = codec.decode(slice_)
            out[fld.dict_keys[0]] = decoded

        for k, v in _load_json(json_str).items():
            if k not in out:
                out[k] = v
        return out


def _dump_json(residual: dict[str, Any]) -> str:
    return json.dumps(residual, ensure_ascii=False, separators=(",", ":"))


def _load_json(json_str: str) -> dict[str, Any]:
    if not json_str:
        return {}
    loaded = json.loads(json_str)
    return loaded if isinstance(loaded, dict) else {}
