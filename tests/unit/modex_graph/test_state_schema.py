"""Tests for `StateSchema` + `StateFieldSpec` — serializable state description."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modex_graph import StateFieldSpec, StateSchema


class TestStateFieldSpec:
    """`StateFieldSpec` — frozen Pydantic value object."""

    def test_defaults(self) -> None:
        spec = StateFieldSpec(name="count", field_type="int")
        assert spec.name == "count"
        assert spec.field_type == "int"
        assert spec.channel == "last_value"
        assert spec.default is None
        assert spec.description is None

    def test_full_construction(self) -> None:
        spec = StateFieldSpec(
            name="items",
            field_type="list[str]",
            channel="reducer",
            default=[],
            description="Accumulated items",
        )
        assert spec.name == "items"
        assert spec.field_type == "list[str]"
        assert spec.channel == "reducer"
        assert spec.default == []
        assert spec.description == "Accumulated items"

    def test_frozen_immutability(self) -> None:
        spec = StateFieldSpec(name="x", field_type="int")
        with pytest.raises(ValidationError):
            spec.name = "y"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            StateFieldSpec(name="x", field_type="int", bogus=True)  # type: ignore[call-arg]

    def test_serialization_round_trip(self) -> None:
        spec = StateFieldSpec(
            name="count",
            field_type="int",
            channel="last_value",
            default=0,
            description="A counter",
        )
        data = spec.model_dump()
        restored = StateFieldSpec.model_validate(data)
        assert restored == spec


class TestStateSchema:
    """`StateSchema` — collection of `StateFieldSpec`."""

    def test_empty_fields_default(self) -> None:
        schema = StateSchema(name="empty")
        assert schema.name == "empty"
        assert schema.fields == []
        assert schema.description is None

    def test_with_fields(self) -> None:
        schema = StateSchema(
            name="react_state",
            fields=[
                StateFieldSpec(name="count", field_type="int", default=0),
                StateFieldSpec(name="items", field_type="list[str]", channel="reducer", default=[]),
            ],
            description="ReAct state",
        )
        assert schema.name == "react_state"
        assert len(schema.fields) == 2
        assert schema.fields[0].name == "count"
        assert schema.fields[1].name == "items"
        assert schema.description == "ReAct state"

    def test_frozen_immutability(self) -> None:
        schema = StateSchema(
            name="s",
            fields=[StateFieldSpec(name="x", field_type="int")],
        )
        with pytest.raises(ValidationError):
            schema.name = "other"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            StateSchema(name="s", bogus=True)  # type: ignore[call-arg]

    def test_serialization_round_trip(self) -> None:
        schema = StateSchema(
            name="react_state",
            fields=[
                StateFieldSpec(name="count", field_type="int", default=0),
                StateFieldSpec(name="name", field_type="str", default=""),
            ],
            description="Test state",
        )
        data = schema.model_dump()
        restored = StateSchema.model_validate(data)
        assert restored == schema

    def test_json_serialization_round_trip(self) -> None:
        """StateSchema must be JSON-serializable for persistence."""
        schema = StateSchema(
            name="react_state",
            fields=[
                StateFieldSpec(name="count", field_type="int", default=0),
                StateFieldSpec(name="flags", field_type="dict[str, Any]", default={}),
            ],
        )
        json_str = schema.model_dump_json()
        restored = StateSchema.model_validate_json(json_str)
        assert restored == schema
