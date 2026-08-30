from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.core.constants import ExecutionStrategyKind, ProviderKind
from modex_agent.scope import (
    AgentSpec,
    PoolSpec,
    RuleId,
    ScopeKind,
    ScopeSpec,
    load_scope_declaration,
    validate_declaration,
)
from modex_agent.scope.spec import CapabilityOverride


def _pool_scope(agent: AgentSpec) -> ScopeSpec:
    return ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(name="pool", agents=[agent]),
    )


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ({"todo": {}}, {"todo": {}}),
        ({"subagents": False}, {"subagents": False}),
        (
            {"experience": {"min_messages": 20}},
            {"experience": {"min_messages": 20}},
        ),
    ],
)
def test_agent_spec_parses_capability_override_maps(
    capabilities: dict[str, CapabilityOverride],
    expected: dict[str, CapabilityOverride],
) -> None:
    spec = AgentSpec.model_validate(
        {"name": "agent", "capabilities": capabilities}
    )

    assert spec.capabilities == expected


def test_agent_spec_rejects_true_capability_override() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {"name": "agent", "capabilities": {"todo": True}}
        )


def test_agent_spec_rejects_integer_zero_as_false_override() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {"name": "agent", "capabilities": {"todo": 0}}
        )


@pytest.mark.parametrize("name", ["", "a.b", "with space", "1abc"])
def test_agent_spec_rejects_invalid_capability_name(name: str) -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(
            {"name": "agent", "capabilities": {name: {}}}
        )


def test_empty_capabilities_map_is_no_override() -> None:
    absent = AgentSpec(name="absent")
    empty = AgentSpec(name="empty", capabilities={})

    assert absent.capabilities is None
    assert empty.capabilities == {}


def test_retired_supplement_field_is_an_unknown_field_rejection(
    tmp_path: Path,
) -> None:
    """The retired additive-tool field died with the capability migration
    (todo 18): ``AgentSpec`` is frozen ``extra="forbid"``, so ANY
    ``tool_supplements`` key — whatever its value — fails the loader as
    an unknown field (a loud boot failure, not a silent ignore)."""
    yml = tmp_path / "retired-face.yml"
    yml.write_text(
        "pool:\n  name: p\n  agents:\n    root:\n      tool_supplements: [todo]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="tool_supplements"):
        load_scope_declaration(yml)


def test_v12_rejects_external_agent_with_explicit_capabilities() -> None:
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
    assert "external" in issues[0].message
    assert "explicit capability declarations" in issues[0].message


def test_v12_accepts_external_agent_without_capabilities_block() -> None:
    agent = AgentSpec(
        name="external",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
    )

    issues = validate_declaration(_pool_scope(agent))

    assert all(issue.rule is not RuleId.EXTERNAL_CAPABILITIES for issue in issues)


def test_v12_accepts_external_agent_with_empty_capabilities_block() -> None:
    agent = AgentSpec(
        name="external",
        execution_strategy=ExecutionStrategyKind.EXTERNAL,
        provider_kind=ProviderKind.OPENCODE,
        capabilities={},
    )

    issues = validate_declaration(_pool_scope(agent))

    assert all(issue.rule is not RuleId.EXTERNAL_CAPABILITIES for issue in issues)


def test_v12_accepts_native_agent_with_capability_overrides() -> None:
    agent = AgentSpec(name="native", capabilities={"subagents": False})

    issues = validate_declaration(_pool_scope(agent))

    assert all(issue.rule is not RuleId.EXTERNAL_CAPABILITIES for issue in issues)


def test_capabilities_round_trip_preserves_frozen_agent_spec() -> None:
    parsed = AgentSpec.model_validate(
        {"name": "agent", "capabilities": {"todo": {}}}
    )
    round_tripped = AgentSpec.model_validate(parsed.model_dump())

    assert round_tripped.capabilities == {"todo": {}}
    with pytest.raises(ValidationError):
        AgentSpec.__setattr__(round_tripped, "capabilities", None)
