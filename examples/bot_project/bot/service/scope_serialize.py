"""Canonical scope-declaration serializer (structured WebUI save path).

The pools config panel edits a JSON tree; the write road converts it to a
``ScopeSpec`` through the SAME loader the boot uses, then this module
renders the spec back to canonical YAML:

- fixed field order (reading order of the shipped declaration);
- deviations only — fields equal to spec field defaults or position-derived
  defaults are omitted (``model_dump(exclude_defaults=True)``, applied
  recursively to nested blocks);
- exception (owner decision): ``use_terminal`` / ``terminal_visibility``
  stay explicit on NATIVE pool roots — the permission-relevant terminal
  face must be visible in the file, not implicit. External agents carry no
  terminal face, so the keys stay off their entries.

The serializer trusts its input is already validated (the PUT gate chain
runs load → validate → compile → validate-effective before serializing).
"""

from __future__ import annotations

from typing import Any, Final

import yaml

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec

# Canonical agent field order — the shipped declaration's reading order.
# ``name``/``parent`` never appear: names are mapping keys and parentage is
# the nesting structure (loader sugar).
_AGENT_FIELD_ORDER: Final = (
    "description",
    "max_steps",
    "use_terminal",
    "terminal_visibility",
    "toolset",
    "tools",
    "tool_configs",
    "capabilities",
    "hooks",
    "hook_configs",
    "interceptors",
    "interceptor_configs",
    "commands",
    "approval",
    "mcp",
    "execution_strategy",
    "provider_kind",
    "context_mode",
    "fork_max_messages",
    "eager",
    "roles",
    "prompt_name",
    "system_prompt",
    "system_prompt_provider",
    "system_prompt_provider_config",
    "memory",
    "memory_system",
    "memory_system_config",
    "llm_provider",
    "llm_provider_config",
    "sandbox",
)

# Map fields whose empty mapping is equivalent to absence (spec: "an empty
# outer block is equivalent to absence") — never emitted.
_DROP_WHEN_EMPTY: Final = frozenset(
    {
        "capabilities",
        "tool_configs",
        "hook_configs",
        "interceptor_configs",
        "system_prompt_provider_config",
        "memory_system_config",
        "llm_provider_config",
    }
)


def serialize_scope_declaration(spec: ScopeSpec) -> str:
    """Render a validated ``ScopeSpec`` as canonical declaration YAML."""
    if spec.kind is ScopeKind.WORKSPACE and spec.workspace is not None:
        ws = spec.workspace
        # dict[str, Any] — a YAML tree node (genuinely open payload, the
        # declaration's nested-mapping sugar form).
        body: dict[str, Any] = {"name": ws.name}
        if ws.persistence is not None:
            body["persistence"] = ws.persistence.model_dump(
                mode="json", exclude_defaults=True
            )
        if ws.paths is not None:
            paths = ws.paths.model_dump(mode="json", exclude_defaults=True)
            if paths:
                body["paths"] = paths
        body["pools"] = {pool.name: _pool_body(pool) for pool in ws.pools}
        tree: dict[str, Any] = {"workspace": body}
    elif spec.kind is ScopeKind.POOL and spec.pool is not None:
        tree = {"pool": {"name": spec.pool.name, **_pool_body(spec.pool)}}
    else:  # pragma: no cover — the spec's form validator forbids this
        raise ValueError(f"malformed scope spec: kind={spec.kind}")
    return yaml.safe_dump(tree, sort_keys=False, allow_unicode=True)


def _pool_body(pool: PoolSpec) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if pool.peers:
        body["peers"] = list(pool.peers)
    body["agents"] = _agents_body(pool.agents)
    return body


def _agents_body(agents: list[AgentSpec]) -> dict[str, Any]:
    """The nested agents mapping — parentage as nesting (loader sugar)."""
    by_parent: dict[str | None, list[AgentSpec]] = {}
    for agent in agents:
        by_parent.setdefault(agent.parent, []).append(agent)

    def build(agent: AgentSpec) -> dict[str, Any]:
        body = _agent_body(agent)
        children = by_parent.get(agent.name)
        if children:
            body["agents"] = {child.name: build(child) for child in children}
        return body

    return {root.name: build(root) for root in by_parent.get(None, [])}


def _agent_body(agent: AgentSpec) -> dict[str, Any]:
    dumped = agent.model_dump(mode="json", exclude_defaults=True)
    if agent.parent is None and str(agent.execution_strategy) == ExecutionStrategyKind.REACT.value:
        # Owner decision: the terminal face stays explicit on native roots.
        dumped["use_terminal"] = agent.use_terminal
        dumped["terminal_visibility"] = agent.terminal_visibility
    return {
        key: dumped[key]
        for key in _AGENT_FIELD_ORDER
        if key in dumped and not (key in _DROP_WHEN_EMPTY and dumped[key] == {})
    }
