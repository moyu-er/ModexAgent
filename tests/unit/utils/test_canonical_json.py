"""Tests for deterministic recursive JSON serialization."""

from __future__ import annotations

import pytest

from modex_agent.utils.canonical_json import canonical_json


def test_canonical_json_recursively_sorts_mapping_keys() -> None:
    data = {"z": {"b": 2, "a": 1}, "a": "first"}

    result = canonical_json(data)

    assert result == '{"a":"first","z":{"a":1,"b":2}}'


def test_canonical_json_preserves_list_and_tuple_order() -> None:
    data = {"items": [3, {"b": 2, "a": 1}], "tuple": ("second", "first")}

    result = canonical_json(data)

    assert result == '{"items":[3,{"a":1,"b":2}],"tuple":["second","first"]}'


def test_canonical_json_sorts_mixed_set_values_by_defined_type_order() -> None:
    data = {"values": {"text", 2.5, 2, True, None, ("other",)}}

    result = canonical_json(data)

    assert result == '{"values":[null,true,2,2.5,"text",["other"]]}'


def test_canonical_json_sorts_nested_sets_deterministically() -> None:
    data = {"values": {("b", 2), ("a", 3), ("a", 1)}}

    result = canonical_json(data)

    assert result == '{"values":[["a",1],["a",3],["b",2]]}'


def test_canonical_json_emits_utf8_friendly_compact_json() -> None:
    data = {"message": "你好", "enabled": True}

    result = canonical_json(data)

    assert result == '{"enabled":true,"message":"你好"}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
        canonical_json({"value": value})
