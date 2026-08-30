"""Typed golden facets and strict split-brain equality assertions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, Field, RootModel

from modex_agent.scope.compiler import ToolOrigin


class FacetField(StrEnum):
    """The five independently compared migration facets."""

    EFFECTIVE_SET = "effective_set"
    TOOL_ROSTER = "tool_roster"
    HOOK_ROSTER = "hook_roster"
    SECTIONS = "sections"
    SUPPLY_KEYS = "supply_keys"


class ToolFacet(BaseModel):
    """One final tool entry exactly as represented in the provenance bill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    origin: ToolOrigin
    replaces: str | None
    targets: tuple[str, ...]


class SectionFacet(BaseModel):
    """One declarable capability prompt section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    section_id: str
    order: int


class Facets(BaseModel):
    """The five observable surfaces captured for one compiled agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effective_set: tuple[str, ...]
    tool_roster: tuple[ToolFacet, ...]
    hook_roster: tuple[str, ...]
    sections: tuple[SectionFacet, ...]
    supply_keys: tuple[str, ...]


class GoldenFile(RootModel[dict[str, Facets]]):
    """Validated JSON shape for one package and pool, keyed by agent name."""

    model_config = ConfigDict(frozen=True)


class Exemption(BaseModel):
    """One explicit, reasoned exception to a single facet comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package: str = Field(min_length=1)
    facet_field: FacetField
    agent_pattern: str = Field(min_length=1)
    reason: str = Field(min_length=1)


FacetValue = tuple[str, ...] | tuple[ToolFacet, ...] | tuple[SectionFacet, ...]
_FACET_FIELDS: Final = tuple(FacetField)


def _facet_value(facets: Facets, field: FacetField) -> FacetValue:
    match field:
        case FacetField.EFFECTIVE_SET:
            return facets.effective_set
        case FacetField.TOOL_ROSTER:
            return facets.tool_roster
        case FacetField.HOOK_ROSTER:
            return facets.hook_roster
        case FacetField.SECTIONS:
            return facets.sections
        case FacetField.SUPPLY_KEYS:
            return facets.supply_keys
        case unreachable:
            assert_never(unreachable)


def assert_facets_equal(
    actual: Mapping[str, Facets],
    golden: Mapping[str, Facets],
    package: str,
    exemptions: Sequence[Exemption] = (),
) -> None:
    """Assert strict per-agent facet equality with explicit exceptions only.

    Each exemption must match a real difference for the requested package,
    facet, and full agent-name regex. An exemption left unused is stale and
    fails the assertion.
    """

    actual_agents = tuple(sorted(actual))
    expected_agents = tuple(sorted(golden))
    if actual_agents != expected_agents:
        missing = sorted(set(expected_agents) - set(actual_agents))
        unexpected = sorted(set(actual_agents) - set(expected_agents))
        agent_differences = [
            f"package={package!r} agent={agent!r} facet='agents' "
            "expected='present' actual='missing'"
            for agent in missing
        ] + [
            f"package={package!r} agent={agent!r} facet='agents' "
            "expected='missing' actual='present'"
            for agent in unexpected
        ]
        raise AssertionError("facet mismatch:\n" + "\n".join(agent_differences))

    used_exemptions: set[int] = set()
    differences: list[str] = []
    for agent in expected_agents:
        expected_facets = golden[agent]
        actual_facets = actual[agent]
        for field in _FACET_FIELDS:
            expected_value = _facet_value(expected_facets, field)
            actual_value = _facet_value(actual_facets, field)
            if actual_value == expected_value:
                continue
            matching = [
                index
                for index, exemption in enumerate(exemptions)
                if exemption.package == package
                and exemption.facet_field is field
                and re.fullmatch(exemption.agent_pattern, agent) is not None
            ]
            if matching:
                used_exemptions.update(matching)
                continue
            differences.append(
                f"package={package!r} agent={agent!r} facet={field.value!r} "
                f"expected={expected_value!r} actual={actual_value!r}"
            )

    for index, exemption in enumerate(exemptions):
        if index not in used_exemptions:
            differences.append(
                "unused exemption "
                f"package={exemption.package!r} "
                f"facet={exemption.facet_field.value!r} "
                f"agent_pattern={exemption.agent_pattern!r} "
                f"reason={exemption.reason!r}"
            )

    if differences:
        raise AssertionError("facet mismatch:\n" + "\n".join(differences))
