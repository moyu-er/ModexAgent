"""Declaration-backed pool listing + reference scans (ticket 11).

Replaces the deleted ``PoolStore`` disk scan as the WebUI's pool-facing
read surface: pool summaries and per-agent prompt references are read from
the scope declaration (``config/scopes/bot.yml``) — the single config
source since the legacy ``config/pools/`` format was deleted. Writes go
through the scope declaration editor (``PUT /api/scope/declaration``,
ticket 16).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from modex_agent.core import ExecutionStrategyKind
from modex_agent.scope import AgentSpec, PoolSpec, ScopeKind

_POOL_LISTING_ERROR = (
    "pool listing requires a readable scope declaration at %s — the legacy "
    "config/pools/ format is no longer read"
)


class PoolSummary(BaseModel):
    """A one-line summary of a declared pool for the listing endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    root_agent_name: str
    subagent_count: int


def _declared_pools(
    declaration_path: Path, *, missing_ok: bool = False
) -> list[PoolSpec]:
    from modex_agent.scope.loader import load_scope_declaration

    if not declaration_path.exists():
        if missing_ok:
            return []
        raise FileNotFoundError(_POOL_LISTING_ERROR % declaration_path)
    spec = load_scope_declaration(declaration_path)
    if spec.kind is ScopeKind.WORKSPACE and spec.workspace is not None:
        return list(spec.workspace.pools)
    if spec.kind is ScopeKind.POOL and spec.pool is not None:
        return [spec.pool]
    return []


def list_pool_summaries(declaration_path: Path) -> list[PoolSummary]:
    """Summarize every declared pool, in declaration order."""
    return [
        PoolSummary(
            name=pool.name,
            root_agent_name=pool.root_agent.name,
            subagent_count=len(pool.agents) - 1,
        )
        for pool in _declared_pools(declaration_path)
    ]


def declared_pool_names(declaration_path: Path) -> set[str]:
    """The set of declared pool names (the input-pipeline pool guard)."""
    return {pool.name for pool in _declared_pools(declaration_path)}


def skill_assignment_eligible(agent: AgentSpec) -> bool:
    """Whether a declared agent has the bundled Skills runtime."""
    if agent.execution_strategy is ExecutionStrategyKind.EXTERNAL:
        return False
    return (agent.capabilities or {}).get("skills") is not False


def validate_skill_assignment_target(
    pool_name: str,
    agent_name: str,
    declaration_path: Path,
) -> None:
    """Reject unknown, external, or explicitly Skills-vetoed targets."""
    for pool in _declared_pools(declaration_path):
        if pool.name != pool_name:
            continue
        for agent in pool.agents:
            if agent.name != agent_name:
                continue
            if skill_assignment_eligible(agent):
                return
            raise ValueError(
                f"agent {agent_name!r} in pool {pool_name!r} does not have Skills enabled"
            )
        break
    raise ValueError(f"unknown skill assignment target {pool_name!r}/{agent_name!r}")


def prompt_usages_of(prompt_name: str, declaration_path: Path) -> list[tuple[str, str, str]]:
    """Declared agents that reference *prompt_name* — ``(pool, kind, agent)``.

    Two reference cases per agent (mirroring the legacy scan):

    1. **Explicit**: ``agent.prompt_name`` is non-empty and equals the name.
    2. **Fallback**: ``agent.prompt_name`` is empty AND the agent's own
       name equals it (the ``agents/<agent_name>.md`` convention).
    """
    usages: list[tuple[str, str, str]] = []
    for pool in _declared_pools(declaration_path, missing_ok=True):
        root_name = pool.root_agent.name
        for agent in pool.agents:
            referenced = (agent.prompt_name and agent.prompt_name == prompt_name) or (
                not agent.prompt_name and agent.name == prompt_name
            )
            if referenced:
                kind = "main" if agent.name == root_name else "subagent"
                usages.append((pool.name, kind, agent.name))
    return usages
