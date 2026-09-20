"""Personal-assistant preference config domain (PA-06).

A SINGLETON config domain over ``<config_dir>/personal_assistant.yml`` holding
the user's entry preferences: ``default_workspace`` (absolute path or null)
and ``default_pool`` (non-empty pool name, default ``"default"``).

Two layers share one owner (:class:`PersonalAssistantPreferences`):

- **ConfigDomain surface** — registered as ``personal_assistant`` so the
  existing generic ``GET/PUT /api/config/personal_assistant`` routes serve it
  with zero route changes. The domain's Pydantic root schema
  (:class:`PersonalAssistantConfig`) enforces strict types (frozen,
  ``extra="forbid"``); a ``model_validator`` normalizes/validates
  ``default_workspace`` at schema level so every write path (REST PUT,
  ``Preferences.save``, direct YAML) gets identical validation.
- **Runtime preference object** — one instance per assembly; the effective
  preference for *new choices* is read through :meth:`preferred_pool` /
  :meth:`preferred_workspace` at call time (immediate for new choices after a
  successful save; the object updates only after the atomic write succeeded,
  so a failed save leaves both memory and disk unchanged).

Workspace validation follows the ticket contract: the path is validated via
normalization (absolute + existing directory) against the server filesystem,
and ``ScopeRegistry.home`` / runtime roots are NOT modified — the preference
is an entry hint, not a data migration.

Rule-12 exception: :class:`PersonalAssistantPreferences` is a runtime object
holding a live :class:`ConfigDomain` (itself a runtime collaborator), not a
frozen value object.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from bot.config.domain import (
    ConfigDomain,
    DomainFlavor,
    RestartMarker,
    atomic_write,
)

DEFAULT_PREFERRED_POOL: str = "default"


def normalize_workspace_path(raw: str) -> str:
    """Validate and normalize a user-supplied workspace path.

    The path belongs to the server: it must already exist as a directory and
    be expressed absolutely (or be resolvable against the process CWD to an
    absolute form). Returns the canonical absolute string. Raises
    ``ValueError`` with a specific reason for relative/nonexistent/file
    inputs — the caller maps that to a field validation error.
    """
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        raise ValueError(
            f"default_workspace must be an absolute path, got {raw!r}"
        )
    resolved = candidate.resolve()
    if not resolved.exists():
        raise ValueError(f"default_workspace path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"default_workspace path is not a directory: {resolved}")
    return str(resolved)


class PersonalAssistantConfig(BaseModel):
    """Strict typed schema of the personal_assistant.yml domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_workspace: str | None = None
    default_pool: str = DEFAULT_PREFERRED_POOL

    @field_validator("default_pool")
    @classmethod
    def _pool_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("default_pool must be a non-empty pool name")
        return v

    @field_validator("default_workspace")
    @classmethod
    def _normalize_workspace(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return normalize_workspace_path(v)


def _load_domain(path: Path) -> dict[str, Any]:
    """YAML mapping loader; schema defaults for missing/empty file.

    Returning the defaults (not ``{}``) means a fresh install reads back the
    full effective values (``default_workspace: null``, ``default_pool:
    default``) through the generic config surface, and a partial write
    merges onto those defaults.
    """
    import yaml

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PersonalAssistantConfig().model_dump()
    if not raw.strip():
        return PersonalAssistantConfig().model_dump()
    loaded = yaml.safe_load(raw)
    if loaded is None:
        return PersonalAssistantConfig().model_dump()
    if not isinstance(loaded, dict):
        raise ValueError(
            f"expected a YAML mapping at {path}, got {type(loaded).__name__}"
        )
    return dict(loaded)


def _dump_domain(path: Path, data: dict[str, Any]) -> None:
    """Atomic YAML dump (sorted-key order preserved, unicode)."""
    import yaml

    atomic_write(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


class PersonalAssistantPreferences:
    """The single runtime owner of the personal-assistant preferences.

    Wraps one :class:`ConfigDomain` (name ``personal_assistant``) whose YAML
    path is the assembly's ``config_dir / "personal_assistant.yml"`` — one
    instance per assembly, constructed by the service from
    ``BotAssemblyRoots.config_dir`` and injected everywhere (REST config
    surface + runtime default resolution). There is deliberately NO
    module-level singleton: the domain registry entry this instance registers
    is what ``GET/PUT /api/config/personal_assistant`` serves through the
    existing :class:`~bot.service.config_controller.ConfigController`, so the
    SAME object backs both the config API and runtime defaults — a PUT is
    visible to new selections immediately.

    The domain is an *immediate-effect* domain: unlike model/im config,
    preference writes apply to new choices without a process restart, so
    ``restart_required`` always reports ``False`` (the mtime-based
    :class:`RestartMarker` is disabled by never capturing the path).

    Reads go through the domain's loader (fresh from disk each call);
    ``save`` validates → persists atomically → the next read reflects the
    new value (failed validation leaves disk and runtime unchanged).
    """

    def __init__(self, yaml_path: Path) -> None:
        self.domain = ConfigDomain(
            name="personal_assistant",
            label="Personal Assistant",
            yaml_path=yaml_path,
            flavor=DomainFlavor.SINGLETON,
            root_schema=PersonalAssistantConfig,
            loader=_load_domain,
            dumper=_dump_domain,
            # Immediate-effect domain: never report restart_required. The
            # marker extension point injects an always-unmodified indicator.
            marker=_NeverRestartMarker(),
        )

    # ── Runtime preference surface ────────────────────────────────────────

    def read(self) -> PersonalAssistantConfig:
        """Read the current preference from disk (missing file → defaults).

        Raises ``pydantic.ValidationError`` on malformed content — a broken
        file must surface loudly on the config surface, not silently reset.
        """
        values, _fields, _restart = self.domain.read()
        return PersonalAssistantConfig.model_validate(values)

    def preferred_pool(self) -> str | None:
        """The preferred default pool for NEW choices.

        Returns the configured pool name, or ``None`` when the preference is
        unreadable (malformed YAML) — never a silent fallback to another
        pool; callers must ask the user to pick in that case.
        """
        try:
            return self.read().default_pool
        except Exception:  # noqa: BLE001 - routing must not crash on bad config
            return None

    def preferred_workspace(self) -> Path | None:
        """The preferred default workspace for NEW entries (None = unset)."""
        try:
            raw = self.read().default_workspace
        except Exception:  # noqa: BLE001
            return None
        if raw is None:
            return None
        try:
            return Path(normalize_workspace_path(raw))
        except ValueError:
            return None

    # ── Persistence ───────────────────────────────────────────────────────

    def save(
        self, *, default_workspace: str | None, default_pool: str
    ) -> PersonalAssistantConfig:
        """Validate and persist a full preference write, atomically.

        Semantics: full replacement of both fields (the two-field schema has
        no partial-write ambiguity worth a merge path). ``default_workspace``
        accepts ``None`` to unset. Raises ``pydantic.ValidationError`` (disk
        and the runtime view untouched) on invalid input.
        """
        target = PersonalAssistantConfig(
            default_workspace=default_workspace, default_pool=default_pool
        )
        self.domain.write(target.model_dump())
        return target


class _NeverRestartMarker(RestartMarker):
    """RestartMarker override for immediate-effect domains.

    ``is_modified`` always returns ``False``: preference saves apply to new
    selections without a process restart, so ``restart_required`` must never
    flip to true (unlike model/im config, which genuinely needs a restart).
    """

    def capture(self, path: Path) -> None:  # noqa: ARG002 - marker API
        return None

    def is_modified(self, path: Path) -> bool:  # noqa: ARG002 - marker API
        return False
