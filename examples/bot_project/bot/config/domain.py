"""Pure helpers shared by all ConfigDomains.

``mask``/``merge``/``describe`` operate on ``dict[str, Any]`` rather than a
typed model because they run over arbitrary per-domain schemas — each
config-domain (IM provider, model registry, ...) exposes a different Pydantic
shape. The returned map is therefore genuinely open/heterogeneous, which is the
single sanctioned rule-3 exception in this module (see ``rules/type-safety.md``
§12). Inside that map, a masked secret value is carried as a
:class:`SecretMask` instance; Pydantic serializes it nested on
``model_dump(mode="json")``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast, get_args, get_origin

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.fields import FieldInfo


class FieldType(str, Enum):
    """Closed set of field kinds a config domain may expose."""

    STRING = "string"
    BOOLEAN = "boolean"
    LIST = "list"
    SECRET = "secret"
    OBJECT = "object"


class DomainFlavor(str, Enum):
    """How a config domain is exposed (single instance vs named registry)."""

    SINGLETON = "singleton"
    REGISTRY = "registry"


class SecretMask(BaseModel):
    """Masked view of a secret field — reveals presence + last-4 hint only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    has_value: bool
    hint: str = ""


class FieldDescriptor(BaseModel):
    """Declarative description of a single config-domain field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    label: str
    type: FieldType
    required: bool


class _SecretMarker:
    """Field-metadata marker placed by :func:`Secret`.

    Detected via ``isinstance`` in :func:`_is_secret`; presence on a field's
    metadata is the canonical signal that the field is a secret.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "Secret()"


def Secret() -> _SecretMarker:  # noqa: N802 - declarative marker, reads as `Annotated[str, Secret()]`
    """Marker factory for secret fields.

    Usage::

        token: Annotated[str, Secret()]
    """

    return _SecretMarker()


def _hint(value: str) -> str:
    """Last-4 hint for a non-empty secret; empty string for falsy input."""

    if not value:
        return ""
    return "••••••••" + value[-4:]


def _is_secret(field_info: FieldInfo) -> bool:
    """True iff the field carries a :class:`_SecretMarker` in its metadata."""

    return any(isinstance(m, _SecretMarker) for m in field_info.metadata)


def _is_basemodel(tp: object) -> bool:
    """True iff ``tp`` is a concrete :class:`BaseModel` subclass.

    This ``isinstance``/``issubclass`` is type-introspection of Pydantic
    models (an external-SDK boundary), not duck-typing of domain objects.
    """

    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _list_item_type(tp: object) -> type[BaseModel] | None:
    """If ``tp`` is ``list[T]`` (or tuple/set/frozenset) over a BaseModel, return T.

    Returns ``None`` for non-sequences or scalar element types.
    """

    if get_origin(tp) in (list, tuple, set, frozenset):
        args = get_args(tp)
        if args and _is_basemodel(args[0]):
            # ``args`` comes from typing.get_args as Any; the _is_basemodel guard
            # narrows it at runtime, cast expresses that for the type checker.
            return cast(type[BaseModel], args[0])
    return None


def _field_type(field_info: FieldInfo) -> FieldType:
    """Resolve the :class:`FieldType` for a Pydantic field."""

    if _is_secret(field_info):
        return FieldType.SECRET
    annotation = field_info.annotation
    if annotation is bool:
        return FieldType.BOOLEAN
    if _is_basemodel(annotation):
        return FieldType.OBJECT
    origin = get_origin(annotation)
    if origin in (list, tuple, set, frozenset) or annotation in (list, tuple, set, frozenset):
        return FieldType.LIST
    return FieldType.STRING


# Open/heterogeneous payload map: arbitrary per-domain schemas (rule-3 §12
# sanctioned exception). A masked secret value inside this dict is a
# ``SecretMask`` instance; everything else is a plain JSON-ish value.
MaskResult = dict[str, Any]


def mask(model_cls: type[BaseModel], data: dict[str, Any]) -> MaskResult:
    """Replace secret values with :class:`SecretMask`; recurse into nested models.

    Operates over arbitrary per-domain schemas, hence ``dict[str, Any]``.
    """

    result: MaskResult = {}
    fields = model_cls.model_fields
    for name, field_info in fields.items():
        if name not in data:
            continue
        val = data[name]
        if _is_secret(field_info):
            # Open/heterogeneous masked map — see module docstring.
            result[name] = SecretMask(
                has_value=bool(val),
                hint=_hint(val) if val else "",
            )
        elif _is_basemodel(field_info.annotation) and isinstance(val, dict):
            result[name] = mask(cast(type[BaseModel], field_info.annotation), val)
        elif isinstance(val, list):
            item_tp = _list_item_type(field_info.annotation)
            if item_tp is not None:
                result[name] = [mask(item_tp, item) for item in val if isinstance(item, dict)]
            else:
                result[name] = list(val)
        else:
            result[name] = val
    return result


def _merge_secret(current: str, payload: object) -> str:
    """Apply secret write semantics for a single secret field.

    - ``{"value": x}`` → overwrite with ``x``
    - ``{"set": False}`` → clear to ``""``
    - echoed ``{"has_value", "hint"}`` / empty dict / ``None`` → keep ``current``
    - plain ``str`` → that str (writes that bypass the mask layer)
    """

    if isinstance(payload, dict):
        if "value" in payload:
            return str(payload["value"])
        if payload.get("set") is False:
            return ""
        return current
    if payload is None:
        return current
    if isinstance(payload, str):
        return payload
    return current


def merge(
    model_cls: type[BaseModel],
    current: dict[str, Any],
    payload: dict[str, Any],
) -> MaskResult:
    """Apply a partial write payload onto ``current`` honoring secret semantics.

    Fields absent from ``payload`` are preserved verbatim. Operates over
    arbitrary per-domain schemas, hence ``dict[str, Any]``.
    """

    result: MaskResult = dict(current)
    fields = model_cls.model_fields
    for name, field_info in fields.items():
        if name not in payload:
            continue
        pv = payload[name]
        if _is_secret(field_info):
            result[name] = _merge_secret(current.get(name, ""), pv)
        elif _is_basemodel(field_info.annotation) and isinstance(pv, dict):
            cur = current.get(name)
            base = cur if isinstance(cur, dict) else {}
            result[name] = merge(cast(type[BaseModel], field_info.annotation), base, pv)
        elif isinstance(pv, list):
            item_tp = _list_item_type(field_info.annotation)
            if item_tp is not None:
                cur_list = current.get(name)
                cur_seq = cur_list if isinstance(cur_list, list) else []
                merged_list: list[Any] = []
                for idx, item in enumerate(pv):
                    cur_item = (
                        cur_seq[idx]
                        if idx < len(cur_seq) and isinstance(cur_seq[idx], dict)
                        else {}
                    )
                    merged_list.append(merge(item_tp, cur_item, item))
                result[name] = merged_list
            else:
                result[name] = list(pv)
        else:
            result[name] = pv
    return result


def describe(model_cls: type[BaseModel]) -> list[FieldDescriptor]:
    """Declarative description of every field on ``model_cls`` in declaration order."""

    out: list[FieldDescriptor] = []
    for name, field_info in model_cls.model_fields.items():
        label = field_info.description or name
        out.append(
            FieldDescriptor(
                name=name,
                label=label,
                type=_field_type(field_info),
                required=field_info.is_required(),
            )
        )
    return out


# --- Domain registry & persistence --------------------------------------


@dataclass(frozen=True)
class KindEntry:
    """Registry entry kind: a class reference plus a human label.

    Rule-11 frozen-``@dataclass`` escape hatch — purely internal record holding
    a class reference and a label, no nested validation, not serialized.
    """

    schema: type[BaseModel]
    label: str


class SectionRead(BaseModel):
    """Cross-boundary read view of one registry section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    # Open/heterogeneous per-IM masked value map (rule-§12 sanctioned exception).
    values: dict[str, Any]
    # Field descriptors for this section. Named ``fields`` (not ``schema``) to
    # avoid shadowing a pydantic BaseModel attribute (which emits a UserWarning).
    fields: list[FieldDescriptor] = Field(
        default_factory=list, description="Field descriptors for this section"
    )


class RestartMarker:
    """Tracks whether the backing YAML file changed since capture.

    Used by :class:`ConfigDomain` to report ``restart_required``: editing
    settings on disk usually requires a process restart to take effect, so
    each domain captures the file mtime on construction and re-checks it on
    every read. Rule-12 exception: holds mutable runtime state, not Pydantic.
    """

    def __init__(self) -> None:
        self._mtimes: dict[Path, float] = {}

    def capture(self, path: Path) -> None:
        """Record the current mtime of ``path`` (missing file → 0.0)."""

        try:
            self._mtimes[path] = path.stat().st_mtime
        except OSError:
            self._mtimes[path] = 0.0

    def is_modified(self, path: Path) -> bool:
        """True iff ``path``'s mtime differs from the captured one."""

        try:
            current = path.stat().st_mtime
        except OSError:
            current = 0.0
        return current != self._mtimes.get(path, 0.0)


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + atomic ``replace``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    # On some platforms (Windows) ``replace`` may preserve the destination's
    # mtime. Touch the new file so consumers that compare mtimes can detect
    # the write.
    path.touch(exist_ok=True)


def _default_loader(path: Path) -> dict[str, Any]:
    """Default YAML loader; ``{}`` for missing or empty files."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    loaded = yaml.safe_load(raw)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"expected a YAML mapping at {path}, got {type(loaded).__name__}")
    # Open/heterogeneous config payload (rule-§12 sanctioned exception).
    return dict(loaded)


def _default_dumper(path: Path, data: dict[str, Any]) -> None:
    """Default YAML dumper — atomic write, sorted keys preserved, unicode."""

    atomic_write(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _reject_unknown_keys(model_cls: type[BaseModel], payload: dict[str, Any]) -> None:
    """Raise :class:`ValidationError` for payload keys not on ``model_cls``.

    ``merge`` silently drops unknown payload keys (it iterates the model's own
    fields), so without this check a write with a typo'd field name would be
    accepted and silently no-op. We synthesize a Pydantic ``ValidationError``
    so callers see the same exception type they'd get from ``extra="forbid"``.
    """

    allowed = set(model_cls.model_fields)
    unknown = [k for k in payload if k not in allowed]
    if unknown:
        raise ValidationError.from_exception_data(
            model_cls.__name__,
            [
                {
                    "type": "extra_forbidden",
                    "loc": (key,),
                    "input": payload,
                }
                for key in unknown
            ],
        )


class ConfigDomain:
    """A single config domain (singleton or named registry) backed by one YAML file.

    Rule-12 exception: this is a runtime object holding mutable state (the kind
    registry and restart marker), not a frozen value object — so it is a plain
    class, not Pydantic. The cross-boundary *read results* it returns
    (``SectionRead``) ARE frozen Pydantic models.
    """

    def __init__(
        self,
        *,
        name: str,
        label: str,
        yaml_path: Path,
        flavor: DomainFlavor,
        root_schema: type[BaseModel] | None = None,
        loader: Callable[[Path], dict[str, Any]] | None = None,
        dumper: Callable[[Path, dict[str, Any]], None] | None = None,
    ) -> None:
        self.name = name
        self.label = label
        self.yaml_path = yaml_path
        self.flavor = flavor
        self.root_schema = root_schema
        self._loader = loader or _default_loader
        self._dumper = dumper or _default_dumper
        self._kinds: dict[str, KindEntry] = {}
        self._marker = RestartMarker()
        self._marker.capture(yaml_path)

    # --- registry management --------------------------------------------

    def register_kind(self, key: str, schema: type[BaseModel], *, label: str = "") -> None:
        """Register a named sub-section kind for a REGISTRY domain."""

        self._kinds[key] = KindEntry(schema=schema, label=label or key)

    def kinds(self) -> dict[str, KindEntry]:
        """Return a shallow copy of the registered kinds."""

        return dict(self._kinds)

    # --- singleton read/write -------------------------------------------

    def read(self) -> tuple[dict[str, Any], list[FieldDescriptor], bool]:
        """Read the singleton domain: (masked values, schema, restart_required)."""

        data = self._loader(self.yaml_path)
        schema = describe(self.root_schema) if self.root_schema is not None else []
        values = mask(self.root_schema, data) if self.root_schema is not None else data
        return values, schema, self._marker.is_modified(self.yaml_path)

    def write(self, payload: dict[str, Any]) -> None:
        """Merge ``payload`` onto current data, validate, then persist atomically.

        Raises :class:`pydantic.ValidationError` if the input violates
        ``root_schema`` (unknown keys under ``extra="forbid"``, wrong types,
        missing required fields) — in which case disk is left untouched.
        """

        assert self.root_schema is not None, "write() requires a root_schema"
        _reject_unknown_keys(self.root_schema, payload)
        current = self._loader(self.yaml_path)
        merged = merge(self.root_schema, current, payload)
        self.root_schema.model_validate(merged)
        self._dumper(self.yaml_path, merged)

    # --- registry read/write -------------------------------------------

    def read_registry(self) -> tuple[dict[str, SectionRead], bool]:
        """Read every registered kind as a masked :class:`SectionRead`."""

        data = self._loader(self.yaml_path)
        out: dict[str, SectionRead] = {}
        for key, entry in self._kinds.items():
            sec = data.get(key, {}) or {}
            if not isinstance(sec, dict):
                raise ValueError(f"section {key!r} at {self.yaml_path} is not a mapping")
            out[key] = SectionRead(
                label=entry.label,
                values=mask(entry.schema, sec),
                fields=describe(entry.schema),
            )
        return out, self._marker.is_modified(self.yaml_path)

    def write_registry(self, payload: dict[str, Any]) -> None:
        """Merge each kind section in ``payload`` onto current data and persist.

        Validates each section against its kind schema; raises
        :class:`pydantic.ValidationError` on bad input without touching disk.
        """

        current = self._loader(self.yaml_path)
        merged: dict[str, Any] = dict(current)
        for key, section_payload in payload.items():
            entry = self._kinds.get(key)
            if entry is None:
                continue
            base = current.get(key, {}) or {}
            if not isinstance(base, dict):
                base = {}
            _reject_unknown_keys(entry.schema, section_payload)
            merged_sec = merge(entry.schema, base, section_payload)
            entry.schema.model_validate(merged_sec)
            merged[key] = merged_sec
        self._dumper(self.yaml_path, merged)


# --- Module-level domain registry ---------------------------------------

# Rule-12 exception: runtime registry holding live ConfigDomain instances.
DOMAINS: dict[str, ConfigDomain] = {}


def register_domain(domain: ConfigDomain) -> ConfigDomain:
    """Register ``domain`` by its ``name`` and return it."""

    DOMAINS[domain.name] = domain
    return domain


def get_domain(name: str) -> ConfigDomain | None:
    """Look up a previously registered domain by name."""

    return DOMAINS.get(name)


__all__ = [
    "FieldType",
    "DomainFlavor",
    "SecretMask",
    "FieldDescriptor",
    "Secret",
    "mask",
    "merge",
    "describe",
    "ConfigDomain",
    "KindEntry",
    "SectionRead",
    "RestartMarker",
    "atomic_write",
    "register_domain",
    "get_domain",
]
