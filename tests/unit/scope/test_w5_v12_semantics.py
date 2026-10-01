"""W5 red anchor T-R2 — V12 (EXTERNAL_CAPABILITIES) semantics preserved.

The W5 completion re-derives V12's trigger from ``RuntimeOwnership``
(``owns_context``, resolved through the EXECUTION_STRATEGY slot probe —
``scope.runtime_ownership``) instead of the ``provider_kind``
discriminator. This anchor pins the EXACT rule id and message text so
the re-keying cannot silently change validator output; the derivation
itself is covered by ``test_w5_ownership_derivation.py`` (probe-resolved
third strategy firing the same bytes).
"""

from __future__ import annotations

from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.scope.validator import RuleId, validate_declaration

_EXPECTED_MESSAGE = (
    "pool 'p': external agent 'external' declares capabilities — explicit "
    "capability declarations are invalid for external agents because "
    "external agents take no native component face; remove the "
    "capabilities block (V12)"
)


def _pool_scope(agent: AgentSpec) -> ScopeSpec:
    return ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="p", agents=[agent]),
    )


def test_v12_rule_id_and_message_bytes_unchanged() -> None:
    agent = AgentSpec(
        name="external",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
        capabilities={"todo": {}},
    )

    issues = validate_declaration(_pool_scope(agent))

    assert len(issues) == 1
    assert issues[0].rule is RuleId.EXTERNAL_CAPABILITIES
    assert issues[0].node == "external"
    assert issues[0].message == _EXPECTED_MESSAGE


def test_v12_still_passes_native_capability_declarations() -> None:
    agent = AgentSpec(name="native", capabilities={"subagents": False})

    issues = validate_declaration(_pool_scope(agent))

    assert all(issue.rule is not RuleId.EXTERNAL_CAPABILITIES for issue in issues)
