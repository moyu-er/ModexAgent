"""Generic read/write/restart collaborator over the registered ConfigDomains.

Injected into the webui server (next task); handlers call
:meth:`ConfigController.read`, :meth:`ConfigController.write`, and
:meth:`ConfigController.restart` and serialize the returned Pydantic payload to
JSON. Cross-boundary response objects are frozen Pydantic models; the controller
itself is a runtime collaborator (rule-12 exception), so it is a plain class.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from bot.config.domain import (
    DomainFlavor,
    FieldDescriptor,
    SectionRead,
    get_domain,
)


class ConfigReadPayload(BaseModel):
    """Masked, serializable read view of one config domain (cross-boundary)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: str
    label: str
    flavor: DomainFlavor
    restart_required: bool
    # Singleton-only (None for registry). Open/heterogeneous masked value map
    # (rule-§12 sanctioned exception); values may carry SecretMask instances,
    # which ``model_dump(mode="json")`` serializes nested.
    values: dict[str, Any] | None = None
    fields: list[FieldDescriptor] | None = None
    # Registry-only (None for singleton).
    sections: dict[str, SectionRead] | None = None


class FieldValidationError(Exception):
    """A write payload failed schema validation."""

    def __init__(self, errors: dict[str, list[str]]) -> None:
        super().__init__("config validation failed")
        self.errors = errors


class ConfigController:
    """Runtime collaborator exposing generic read/write/restart over domains.

    Rule-12 exception: holds no structured state of its own (only an injected
    restarter callback), so it is a plain class rather than a frozen model.
    """

    def __init__(self, *, restarter: Callable[[], None] | None = None) -> None:
        self._restarter = restarter

    def read(self, domain_name: str) -> ConfigReadPayload:
        dom = get_domain(domain_name)
        if dom is None:
            raise KeyError(domain_name)
        if dom.flavor is DomainFlavor.REGISTRY:
            sections, restart_required = dom.read_registry()
            return ConfigReadPayload(
                domain=dom.name,
                label=dom.label,
                flavor=dom.flavor,
                restart_required=restart_required,
                sections=sections,
            )
        values, fields, restart_required = dom.read()
        return ConfigReadPayload(
            domain=dom.name,
            label=dom.label,
            flavor=dom.flavor,
            restart_required=restart_required,
            values=values,
            fields=fields,
        )

    def write(self, domain_name: str, payload: dict[str, Any]) -> ConfigReadPayload:
        dom = get_domain(domain_name)
        if dom is None:
            raise KeyError(domain_name)
        try:
            if dom.flavor is DomainFlavor.REGISTRY:
                dom.write_registry(payload or {})
            else:
                dom.write(payload or {})
        except ValidationError as ve:
            raise FieldValidationError(_flatten_errors(ve)) from ve
        return self.read(domain_name)

    def restart(self) -> None:
        if self._restarter is None:
            raise RuntimeError("no restarter configured")
        self._restarter()


def _flatten_errors(ve: ValidationError) -> dict[str, list[str]]:
    """Convert a Pydantic ValidationError into {loc_joined: [messages]}."""

    out: dict[str, list[str]] = {}
    for err in ve.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        out.setdefault(loc, []).append(err.get("msg", "invalid"))
    return out
