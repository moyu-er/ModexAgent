from __future__ import annotations

import pytest

from modex_agent.scope.compiler import ToolOrigin
from tests.unit.scope.goldens.assertor import (
    Exemption,
    FacetField,
    Facets,
    GoldenFile,
    ToolFacet,
    assert_facets_equal,
)
from tests.unit.scope.goldens.capture import GoldenPackage, capture_package_bytes


def _facets(*, hooks: tuple[str, ...] = ("after_turn",)) -> Facets:
    return Facets(
        effective_set=("todo",),
        tool_roster=(
            ToolFacet(
                name="read",
                origin=ToolOrigin.PRESET,
                replaces=None,
                targets=(),
            ),
        ),
        hook_roster=hooks,
        sections=(),
        supply_keys=("todo",),
    )


def test_equal_facets_pass() -> None:
    facets = {"worker": _facets()}

    assert_facets_equal(facets, facets, package="todo")


def test_single_field_difference_names_exact_field_and_agent() -> None:
    actual = {"worker": _facets(hooks=("after_turn", "todo_continuation"))}
    golden = {"worker": _facets()}

    with pytest.raises(AssertionError) as error:
        assert_facets_equal(actual, golden, package="todo")

    message = str(error.value)
    assert "agent='worker'" in message
    assert "facet='hook_roster'" in message
    assert "expected=('after_turn',)" in message
    assert "actual=('after_turn', 'todo_continuation')" in message
    for unchanged_field in (
        "effective_set",
        "tool_roster",
        "sections",
        "supply_keys",
    ):
        assert f"facet='{unchanged_field}'" not in message


def test_exemption_masks_only_its_matching_difference() -> None:
    actual = {
        "worker": _facets(hooks=("after_turn", "todo_continuation")),
        "other": _facets(),
    }
    golden = {"worker": _facets(), "other": _facets()}
    exemption = Exemption(
        package="todo",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern="worker",
        reason="The migration makes the formerly hardwired hook declarable.",
    )

    assert_facets_equal(actual, golden, package="todo", exemptions=(exemption,))


def test_exemption_does_not_mask_another_agents_difference() -> None:
    actual = {
        "worker": _facets(hooks=("after_turn", "todo_continuation")),
        "other": _facets(hooks=("after_turn", "todo_continuation")),
    }
    golden = {"worker": _facets(), "other": _facets()}
    exemption = Exemption(
        package="todo",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern="worker",
        reason="Only the worker's transition is intentional.",
    )

    with pytest.raises(AssertionError) as error:
        assert_facets_equal(actual, golden, package="todo", exemptions=(exemption,))

    message = str(error.value)
    assert "agent='other'" in message
    assert "agent='worker'" not in message


def test_unmatched_exemption_fails_loudly() -> None:
    facets = {"worker": _facets()}
    exemption = Exemption(
        package="todo",
        facet_field=FacetField.HOOK_ROSTER,
        agent_pattern="missing-agent",
        reason="A stale exception must not survive its difference.",
    )

    with pytest.raises(AssertionError, match="unused exemption.*missing-agent"):
        assert_facets_equal(facets, facets, package="todo", exemptions=(exemption,))


async def test_real_capture_is_byte_deterministic() -> None:
    first = await capture_package_bytes(GoldenPackage.TODO)
    second = await capture_package_bytes(GoldenPackage.TODO)

    assert first
    assert first == second
    for payload in first.values():
        GoldenFile.model_validate_json(payload)
