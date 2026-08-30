"""Runtime-created workspaces (ticket 17): declaration write-back + boot routing.

A dynamically created workspace is identified by NAME — the identity is the
declaration file name (the 02 write-back contract):

- declaration: ``config/scopes/workspaces/<name>.yml`` — a FULL loadable
  declaration: the primary declaration's hosted pools copied verbatim, with
  the workspace layer reduced to the minimal face (name + optional
  persistence backend). The restart loader reads it directly.
- root: ``<project_dir>/subworkspace/<name>`` — deterministic, so a restart
  re-registers the workspace by the same name→path mapping.

Creation reuses the COMPLETE boot code path — write the declaration, then
``ScopeRegistry.get_or_open`` + ``materialize`` (the same
``_assemble_resources`` road every ``/cd``-materialized workspace takes).
There is no runtime hot assembly (N2): the new workspace's pools boot through
the declaration road exactly as they would after a restart.

/cd semantics are untouched: switching is a routing pointer over already
existing directories; creation makes a NEW workspace that is immediately
usable. Both feed the same lazy-materialization registry.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from modex_agent.scope.loader import load_dynamic_workspace_declarations
from modex_agent.scope.spec import ScopeKind
from modex_agent.workspace.context import WorkspaceContext

if TYPE_CHECKING:
    from bot.service.core import BotService

logger = logging.getLogger(__name__)

DYNAMIC_WORKSPACE_PARENT_DIR = "subworkspace"
"""Runtime-populated workspace targets live under ``<project>/subworkspace/``."""

_DYNAMIC_DECLARATIONS_SUBPATH = Path("config") / "scopes" / "workspaces"

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class WorkspaceCreationError(ValueError):
    """A workspace-creation request is invalid or the setup refuses it."""


class WorkspaceExistsError(WorkspaceCreationError):
    """The requested workspace name (or its target) already exists."""


@dataclass(frozen=True)
class WorkspaceCreationResult:
    """What ``create_workspace`` produced: identity, root, and booted pools."""

    name: str
    root: Path
    declaration_path: Path
    pools: tuple[str, ...]


def dynamic_workspace_root(project_dir: Path, name: str) -> Path:
    """The deterministic root of the dynamic workspace ``name``."""
    return project_dir / DYNAMIC_WORKSPACE_PARENT_DIR / name


def dynamic_declarations_dir(project_dir: Path) -> Path:
    """The directory holding per-dynamic-workspace declaration files."""
    return project_dir / _DYNAMIC_DECLARATIONS_SUBPATH


def dynamic_workspace_declaration_path(project_dir: Path, target: Path) -> Path | None:
    """The declaration file a workspace target boots from, or ``None``.

    ``None`` means the target boots the primary declaration
    (``config/scopes/bot.yml``) — the home workspace, arbitrary ``/cd``
    targets, and dynamic roots whose declaration file has been removed.
    A dynamic target is one that sits exactly at
    ``<project_dir>/subworkspace/<name>`` WITH a matching
    ``workspaces/<name>.yml`` on disk; the file's presence is what makes the
    name→declaration routing real (identity = file name).
    """
    try:
        resolved = Path(target).resolve()
        relative = resolved.relative_to(Path(project_dir).resolve())
    except (OSError, ValueError):
        return None
    if len(relative.parts) != 2 or relative.parts[0] != DYNAMIC_WORKSPACE_PARENT_DIR:
        return None
    candidate = dynamic_declarations_dir(project_dir) / f"{relative.parts[1]}.yml"
    return candidate if candidate.is_file() else None


def _primary_declaration(project_dir: Path) -> Path:
    return project_dir / "config" / "scopes" / "bot.yml"


async def register_dynamic_workspaces(service: BotService) -> list[str]:
    """Boot-time registration: read ``workspaces/*.yml``, register each root.

    The declarations are the persistent record of runtime-created
    workspaces — the registry store is merely a cache that also learns
    ``/cd`` targets. Registration makes each dynamic workspace's context
    known so the dispatcher lazily materializes it on the first turn that
    targets it (the same road a ``/cd``-switched workspace takes after a
    restart). Broken declaration files fail the boot loudly, matching the
    primary declaration's fatal-on-malformed contract.

    Returns the registered names in sorted (deterministic) order.
    """
    spec = service._scope_spec
    declarations = load_dynamic_workspace_declarations(
        dynamic_declarations_dir(service._project_dir)
    )
    if not declarations:
        return []
    if spec is None or spec.kind is not ScopeKind.WORKSPACE:
        raise WorkspaceCreationError(
            "dynamic workspace declarations require a workspace-layer primary "
            f"declaration ({_primary_declaration(service._project_dir)}); "
            f"found {len(declarations)} dynamic declaration(s) under a "
            "non-workspace deployment"
        )
    registry = service.workspace_stack.registry
    for name in sorted(declarations):
        root = dynamic_workspace_root(service._project_dir, name)
        await registry.get_or_open(root)
        logger.info(
            "[dynamic-workspace] registered %r at %s (declaration: %s)",
            name,
            root,
            dynamic_declarations_dir(service._project_dir) / f"{name}.yml",
        )
    return sorted(declarations)


def _write_dynamic_declaration(
    primary_path: Path, destination: Path, *, name: str, backend: str | None
) -> None:
    """Write ``destination``: the primary declaration's pools under a new
    workspace layer (name + optional persistence backend).

    The copy is a YAML-level transformation of the primary declaration —
    pools are copied verbatim (byte-faithful structure, no model round-trip
    that could drop fields), and the workspace layer keeps only the minimal
    face plus the optionally requested backend selection.
    """
    raw: object = yaml.safe_load(primary_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "workspace" not in raw:
        raise WorkspaceCreationError(
            f"{primary_path}: the primary declaration must use the workspace "
            "root form to host dynamically created workspaces"
        )
    workspace = raw["workspace"]
    if not isinstance(workspace, dict):
        raise WorkspaceCreationError(
            f"{primary_path}: the primary declaration's workspace body is not a mapping"
        )
    workspace["name"] = name
    if backend is not None:
        workspace["persistence"] = {"backend": backend}
    else:
        workspace.pop("persistence", None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _validate_creation_request(
    service: BotService, name: str, backend: str | None
) -> None:
    if not _NAME_PATTERN.match(name):
        raise WorkspaceCreationError(
            f"invalid workspace name {name!r}: use 1-64 characters of letters, "
            "digits, '_' or '-', starting with a letter or digit"
        )
    if service._scope_spec is None or service._scope_spec.kind is not ScopeKind.WORKSPACE:
        raise WorkspaceCreationError(
            "workspace creation requires a workspace-layer primary declaration "
            f"({_primary_declaration(service._project_dir)})"
        )
    if service.workspace_stack is None:
        raise WorkspaceCreationError("workspace stack is not assembled")
    declaration_path = dynamic_declarations_dir(service._project_dir) / f"{name}.yml"
    if declaration_path.exists():
        raise WorkspaceExistsError(
            f"a workspace named {name!r} already exists ({declaration_path})"
        )
    if (service._project_dir / DYNAMIC_WORKSPACE_PARENT_DIR / name).resolve() in {
        Path(t).resolve() for t in service.workspace_stack.registry.known_targets()
    }:
        raise WorkspaceExistsError(
            f"a workspace is already registered at "
            f"{dynamic_workspace_root(service._project_dir, name)}"
        )
    if backend is not None:
        from modex_agent.persistence.config import PersistenceBackend

        try:
            PersistenceBackend(backend)
        except ValueError as exc:
            raise WorkspaceCreationError(
                f"unknown persistence backend {backend!r} "
                f"(expected one of {[b.value for b in PersistenceBackend]})"
            ) from exc


async def create_workspace(
    service: BotService, *, name: str, backend: str | None = None
) -> WorkspaceCreationResult:
    """Create a workspace at runtime: write declaration → full boot path.

    The declaration is written FIRST (the persistent record — it survives
    restart), then the workspace materializes through the registry's normal
    lazy-materialization road, which boots its pools from the just-written
    declaration. Materialization runs BEFORE the registration upsert so a
    failed creation leaves nothing behind: the declaration is removed, the
    empty root is dropped, and no ghost context sits in the registry — a
    half-created workspace must not poison the next restart's boot read.

    Raises:
        WorkspaceCreationError: invalid name/backend, no workspace-layer
            declaration, or the stack is missing.
        WorkspaceExistsError: the name or its target is already taken.
    """
    _validate_creation_request(service, name, backend)
    declaration_path = dynamic_declarations_dir(service._project_dir) / f"{name}.yml"
    root = dynamic_workspace_root(service._project_dir, name)
    _write_dynamic_declaration(
        _primary_declaration(service._project_dir),
        declaration_path,
        name=name,
        backend=backend,
    )
    root.mkdir(parents=True, exist_ok=True)
    registry = service.workspace_stack.registry
    app_config = service._app_config
    assert app_config is not None, "AppConfig must be loaded before workspace creation"
    ctx = WorkspaceContext.from_target(
        root, data_dir_name=app_config.paths.data_dir_name, home=registry.home
    )
    try:
        resources = await registry.materialize(ctx)
        await registry.get_or_open(root)
    except BaseException:
        declaration_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            root.rmdir()  # only succeeds while empty — a pre-populated root stays
        raise
    logger.info(
        "[dynamic-workspace] created %r at %s — pools booted from %s",
        name,
        root,
        declaration_path,
    )
    return WorkspaceCreationResult(
        name=name,
        root=root,
        declaration_path=declaration_path,
        pools=tuple(resources.pools),
    )
