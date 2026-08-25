"""Scope declaration loader — explicit file path → frozen ``ScopeSpec`` tree.

The production source of truth is a SINGLE file
(``examples/bot_project/config/scopes/bot.yml``) read by explicit path —
never a directory scan (no duplicate declarations, no misread files).
Pool-as-root declarations (no workspace layer, SPEC §3.1) load through
the same explicit-path parameter.

One exception to "no directory scan" (ticket 17, the 02 write-back
contract): runtime-created workspaces persist as exactly one file per
workspace under ``config/scopes/workspaces/``, where the FILE STEM is the
workspace identity. :func:`load_dynamic_workspace_declarations` reads that
directory — the restart-time companion read to the primary declaration.

Nested ``agents:`` mappings are parse-level sugar (SPEC §3.6): the loader
flattens them into the flat model with ``parent`` references. An agent
entry may also carry an explicit ``parent:`` key (the raw flat form); when
both express the same parent that is redundant-but-valid, and when they
disagree the loader fails loudly.

Error contract:
- ``pydantic.ValidationError`` propagates for field-level violations
  (unknown fields via ``extra="forbid"``, bad types, dangling parent
  references caught by :class:`~modex_agent.scope.spec.PoolSpec`).
- :class:`ScopeDeclarationError` is raised for structural violations the
  models cannot see: unrecognized root form, conflicting root keys, an
  explicit ``parent`` disagreeing with nesting, non-mapping bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from modex_agent.scope.spec import (
    AgentSpec,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    WorkspaceSpec,
)

_ROOT_FORM_KEYS = frozenset({"workspace", "pool"})


class ScopeDeclarationError(ValueError):
    """Structural failure in a scope declaration file (with path context)."""


def load_scope_declaration(path: Path) -> ScopeSpec:
    """Load the declaration file at ``path`` into a frozen ``ScopeSpec``.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        ScopeDeclarationError: Unrecognized/ambiguous root form, an
            explicit ``parent`` conflicting with nesting, or a non-mapping
            declaration body.
        pydantic.ValidationError: Field-level violations, including
            dangling parent references.
    """
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ScopeDeclarationError(f"{path}: expected a YAML mapping at the top level")

    present = _ROOT_FORM_KEYS & set(raw)
    if len(present) == 2:
        raise ScopeDeclarationError(
            f"{path}: both 'workspace' and 'pool' root keys present — declare exactly one"
        )
    if not present:
        raise ScopeDeclarationError(
            f"{path}: unrecognized scope declaration form — "
            f"expected a top-level 'workspace' or 'pool' key"
        )
    unknown = set(raw) - present
    if unknown:
        raise ScopeDeclarationError(
            f"{path}: unexpected top-level key(s) {sorted(unknown)} "
            f"next to {sorted(present)[0]!r}"
        )

    if "workspace" in raw:
        workspace = _parse_workspace(raw["workspace"], path)
        return ScopeSpec(kind=ScopeKind.WORKSPACE, workspace=workspace)
    pool = _parse_pool_decl(raw["pool"], path, name=None)
    return ScopeSpec(kind=ScopeKind.POOL, pool=pool)


def load_dynamic_workspace_declarations(directory: Path) -> dict[str, ScopeSpec]:
    """Load every runtime-created workspace declaration under ``directory``.

    The 02 write-back contract (consumed by ticket 17): each
    runtime-created workspace owns exactly ONE ``<name>.yml`` file whose
    stem IS the workspace identity. Files load in sorted filename order so
    boot behavior is deterministic. A declaration whose workspace name
    disagrees with its file stem fails loudly — identity is the file name,
    never a free-floating body value. A non-workspace root form is equally
    loud: these files exist only to declare dynamically created workspaces.

    An absent directory is not an error — a deployment without
    runtime-created workspaces simply has no such files.
    """
    if not directory.is_dir():
        return {}
    declarations: dict[str, ScopeSpec] = {}
    for path in sorted(directory.glob("*.yml")):
        spec = load_scope_declaration(path)
        if spec.kind is not ScopeKind.WORKSPACE or spec.workspace is None:
            raise ScopeDeclarationError(
                f"{path}: a dynamic workspace declaration must use the "
                f"'workspace' root form (found {spec.kind.value!r})"
            )
        name = path.stem
        if spec.workspace.name != name:
            raise ScopeDeclarationError(
                f"{path}: workspace name {spec.workspace.name!r} does not match "
                f"the file name — a dynamic workspace's identity IS its file name"
            )
        declarations[name] = spec
    return declarations


def _parse_workspace(body: Any, path: Path) -> WorkspaceSpec:
    if not isinstance(body, dict):
        raise ScopeDeclarationError(f"{path}: 'workspace' body must be a mapping")
    pools_raw = body.get("pools")
    if pools_raw is None:
        pools_raw = {}
    if not isinstance(pools_raw, dict):
        raise ScopeDeclarationError(f"{path}: workspace 'pools' must be a mapping")
    without_pools = {k: v for k, v in body.items() if k != "pools"}
    workspace = WorkspaceSpec.model_validate(without_pools)
    pools = [
        _parse_pool_decl(pool_body, path, name=pool_name)
        for pool_name, pool_body in pools_raw.items()
    ]
    return workspace.model_copy(update={"pools": pools})


def _parse_pool_decl(body: Any, path: Path, *, name: str | None) -> PoolSpec:
    """Parse one pool declaration body.

    ``name`` comes from the workspace pools mapping key (mapping-key form);
    ``name=None`` means pool-as-root, where the body must carry ``name``.
    """
    if not isinstance(body, dict):
        raise ScopeDeclarationError(f"{path}: pool declaration body must be a mapping")
    if name is not None:
        if "name" in body:
            raise ScopeDeclarationError(
                f"{path}: pool {name!r}: pool name comes from the mapping key — "
                f"a 'name' key in the body is ambiguous"
            )
        pool_name: str = name
    else:
        body_name = body.get("name")
        if not isinstance(body_name, str):
            raise ScopeDeclarationError(
                f"{path}: pool-as-root declaration requires a 'name' key"
            )
        pool_name = body_name

    agents_raw = body.get("agents")
    if agents_raw is None:
        agents_raw = {}
    agents = _flatten_agents(agents_raw, parent=None, pool=pool_name, path=path)
    fields = {
        k: v for k, v in body.items() if k not in ("name", "agents")
    }
    return PoolSpec.model_validate(
        {**fields, "name": pool_name, "agents": agents}
    )


def _flatten_agents(
    raw: Any,
    *,
    parent: str | None,
    pool: str,
    path: Path,
) -> list[AgentSpec]:
    """Flatten a (possibly nested) ``agents`` mapping into flat AgentSpecs.

    Nesting derives ``parent``; an explicit ``parent`` key must agree with
    the nesting parent when both are present. Declaration order is
    preserved (children follow their parent, in mapping order).
    """
    if not isinstance(raw, dict):
        raise ScopeDeclarationError(
            f"{path}: pool {pool!r}: 'agents' must be a mapping of agent name "
            f"to declaration"
        )
    agents: list[AgentSpec] = []
    for agent_name, body in raw.items():
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ScopeDeclarationError(
                f"{path}: pool {pool!r}: agent {agent_name!r} declaration "
                f"must be a mapping"
            )
        children = body.get("agents")
        explicit_parent = body.get("parent")
        if (
            explicit_parent is not None
            and parent is not None
            and explicit_parent != parent
        ):
            raise ScopeDeclarationError(
                f"{path}: pool {pool!r}: agent {agent_name!r} is nested "
                f"under {parent!r} but declares parent {explicit_parent!r}"
            )
        effective_parent = explicit_parent if explicit_parent is not None else parent
        fields = {k: v for k, v in body.items() if k not in ("agents", "parent")}
        agents.append(
            AgentSpec.model_validate(
                {**fields, "name": agent_name, "parent": effective_parent}
            )
        )
        if children is not None:
            agents.extend(
                _flatten_agents(children, parent=agent_name, pool=pool, path=path)
            )
    return agents
