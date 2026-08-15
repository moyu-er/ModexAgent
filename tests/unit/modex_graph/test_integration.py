"""Tests for IntegratedPayload, IntegratedInput, InputIntegrator (ticket 07).

Covers:

- `IntegratedPayload` frozen Pydantic model (rules 10-16): construction,
  frozen immutability, `extra="forbid"`, field defaults.
- `IntegratedInput` frozen Pydantic model: construction, default
  `payloads=[]`, `integrated_content=None`.
- `InputIntegrator` ABC (rule 7: ABC, not Protocol): abstract, cannot
  instantiate, one abstract method `integrate`.
- `DefaultInputIntegrator`: concatenates payloads,
  `integrated_content = [p.content for p in payloads]`.
"""

from __future__ import annotations

from abc import ABC
from typing import Any

import pytest
from pydantic import ValidationError

from modex_graph import (
    DefaultInputIntegrator,
    DeliverConsumptionStatus,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
)

# ── IntegratedPayload ─────────────────────────────────────────────────────


class TestIntegratedPayload:
    def test_construction_with_required_fields(self) -> None:
        p = IntegratedPayload(source_node="upstream_a", content={"key": "value"})
        assert p.source_node == "upstream_a"
        assert p.content == {"key": "value"}

    def test_metadata_defaults_to_empty_dict(self) -> None:
        p = IntegratedPayload(source_node="a", content="data")
        assert p.metadata == {}

    def test_metadata_can_be_set(self) -> None:
        p = IntegratedPayload(
            source_node="a",
            content="data",
            metadata={"priority": 1, "tag": "urgent"},
        )
        assert p.metadata == {"priority": 1, "tag": "urgent"}

    def test_frozen_cannot_set_fields(self) -> None:
        p = IntegratedPayload(source_node="a", content="data")
        with pytest.raises(ValidationError):
            p.content = "new_data"  # type: ignore[misc]

    def test_frozen_cannot_delete_fields(self) -> None:
        p = IntegratedPayload(source_node="a", content="data")
        with pytest.raises(ValidationError):
            del p.content  # type: ignore[misc]

    def test_extra_forbid_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            IntegratedPayload.model_validate(
                {"source_node": "a", "content": "data", "unknown_field": "bad"}
            )

    def test_content_can_be_any_json_serializable(self) -> None:
        for content in ["string", 42, [1, 2, 3], {"k": "v"}, None, True]:
            p = IntegratedPayload(source_node="a", content=content)
            assert p.content == content

    def test_source_node_required(self) -> None:
        with pytest.raises(ValidationError):
            IntegratedPayload.model_validate({"content": "data"})

    def test_content_required(self) -> None:
        with pytest.raises(ValidationError):
            IntegratedPayload.model_validate({"source_node": "a"})

    def test_status_defaults_to_pending(self) -> None:
        p = IntegratedPayload(source_node="a", content="data")
        assert p.status is DeliverConsumptionStatus.PENDING

    def test_consumed_by_invocation_id_defaults_to_none(self) -> None:
        p = IntegratedPayload(source_node="a", content="data")
        assert p.consumed_by_invocation_id is None

    def test_status_can_be_consumed_pending(self) -> None:
        p = IntegratedPayload(
            source_node="a",
            content="data",
            status=DeliverConsumptionStatus.CONSUMED_PENDING,
        )
        assert p.status is DeliverConsumptionStatus.CONSUMED_PENDING

    def test_consumed_by_invocation_id_can_be_set(self) -> None:
        p = IntegratedPayload(
            source_node="a",
            content="data",
            status=DeliverConsumptionStatus.CONSUMED_PENDING,
            consumed_by_invocation_id=42,
        )
        assert p.consumed_by_invocation_id == 42

    def test_status_frozen_cannot_set(self) -> None:
        p = IntegratedPayload(source_node="a", content="data")
        with pytest.raises(ValidationError):
            p.status = DeliverConsumptionStatus.CONSUMED_PENDING  # type: ignore[misc]

    def test_round_trip_preserves_new_fields(self) -> None:
        p = IntegratedPayload(
            source_node="a",
            content={"k": "v"},
            status=DeliverConsumptionStatus.CONSUMED_PENDING,
            consumed_by_invocation_id=7,
        )
        dumped = p.model_dump()
        assert dumped["status"] == DeliverConsumptionStatus.CONSUMED_PENDING
        assert dumped["consumed_by_invocation_id"] == 7
        restored = IntegratedPayload.model_validate(dumped)
        assert restored.status is DeliverConsumptionStatus.CONSUMED_PENDING
        assert restored.consumed_by_invocation_id == 7
        assert restored == p

    def test_round_trip_json_preserves_new_fields(self) -> None:
        p = IntegratedPayload(
            source_node="a",
            content="data",
            status=DeliverConsumptionStatus.CONSUMED_PENDING,
            consumed_by_invocation_id=99,
        )
        json_str = p.model_dump_json()
        restored = IntegratedPayload.model_validate_json(json_str)
        assert restored.status is DeliverConsumptionStatus.CONSUMED_PENDING
        assert restored.consumed_by_invocation_id == 99
        assert restored == p


# ── IntegratedInput ───────────────────────────────────────────────────────


class TestIntegratedInput:
    def test_default_construction_empty_payloads(self) -> None:
        inp = IntegratedInput()
        assert inp.payloads == []
        assert inp.integrated_content is None

    def test_construction_with_payloads(self) -> None:
        p1 = IntegratedPayload(source_node="a", content="data1")
        p2 = IntegratedPayload(source_node="b", content="data2")
        inp = IntegratedInput(
            payloads=[p1, p2],
            integrated_content=["data1", "data2"],
        )
        assert len(inp.payloads) == 2
        assert inp.payloads[0] == p1
        assert inp.payloads[1] == p2
        assert inp.integrated_content == ["data1", "data2"]

    def test_frozen_cannot_set_fields(self) -> None:
        inp = IntegratedInput()
        with pytest.raises(ValidationError):
            inp.integrated_content = "new"  # type: ignore[misc]

    def test_extra_forbid_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            IntegratedInput.model_validate({"unknown": "bad"})

    def test_integrated_content_can_be_any_type(self) -> None:
        for content in ["string", 42, [1, 2], {"k": "v"}, None]:
            inp = IntegratedInput(integrated_content=content)
            assert inp.integrated_content == content


# ── InputIntegrator ABC ───────────────────────────────────────────────────


class TestInputIntegratorABC:
    def test_is_abc(self) -> None:
        assert issubclass(InputIntegrator, ABC)

    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            InputIntegrator()  # type: ignore[abstract]

    def test_one_abstract_method(self) -> None:
        assert set(InputIntegrator.__abstractmethods__) == {"integrate"}

    def test_is_not_protocol(self) -> None:
        from typing import Protocol

        assert not issubclass(InputIntegrator, Protocol)


# ── DefaultInputIntegrator ────────────────────────────────────────────────


class TestDefaultInputIntegrator:
    def test_is_subclass_of_input_integrator(self) -> None:
        assert issubclass(DefaultInputIntegrator, InputIntegrator)

    def test_no_abstract_methods(self) -> None:
        assert len(DefaultInputIntegrator.__abstractmethods__) == 0

    def test_can_instantiate(self) -> None:
        integrator = DefaultInputIntegrator()
        assert isinstance(integrator, InputIntegrator)

    def test_integrate_empty_list(self) -> None:
        integrator = DefaultInputIntegrator()
        result = integrator.integrate([])
        assert result.payloads == []
        assert result.integrated_content == []

    def test_integrate_single_payload(self) -> None:
        integrator = DefaultInputIntegrator()
        p = IntegratedPayload(source_node="a", content="data")
        result = integrator.integrate([p])
        assert len(result.payloads) == 1
        assert result.payloads[0] == p
        assert result.integrated_content == ["data"]

    def test_integrate_multiple_payloads(self) -> None:
        integrator = DefaultInputIntegrator()
        p1 = IntegratedPayload(source_node="a", content="data1")
        p2 = IntegratedPayload(source_node="b", content="data2")
        p3 = IntegratedPayload(source_node="c", content="data3")
        result = integrator.integrate([p1, p2, p3])
        assert len(result.payloads) == 3
        assert result.integrated_content == ["data1", "data2", "data3"]

    def test_integrate_preserves_payload_order(self) -> None:
        integrator = DefaultInputIntegrator()
        p1 = IntegratedPayload(source_node="a", content="first")
        p2 = IntegratedPayload(source_node="b", content="second")
        result = integrator.integrate([p2, p1])
        assert result.payloads[0] == p2
        assert result.payloads[1] == p1
        assert result.integrated_content == ["second", "first"]

    def test_integrate_preserves_complex_content(self) -> None:
        integrator = DefaultInputIntegrator()
        p1 = IntegratedPayload(source_node="a", content={"nested": [1, 2]})
        p2 = IntegratedPayload(source_node="b", content=None)
        result = integrator.integrate([p1, p2])
        assert result.integrated_content == [{"nested": [1, 2]}, None]

    def test_integrate_result_is_frozen(self) -> None:
        integrator = DefaultInputIntegrator()
        result = integrator.integrate([])
        with pytest.raises(ValidationError):
            result.integrated_content = "modified"  # type: ignore[misc]


# ── Custom InputIntegrator (verifies ABC is subclassable) ─────────────────


class _ConcatTextIntegrator(InputIntegrator):
    """Custom integrator: joins string contents with a separator."""

    def integrate(self, payloads: list[IntegratedPayload]) -> IntegratedInput:
        texts: list[Any] = [p.content for p in payloads]
        joined = " | ".join(str(t) for t in texts) if texts else ""
        return IntegratedInput(
            payloads=payloads,
            integrated_content=joined,
        )


class TestCustomInputIntegrator:
    def test_custom_integrator_works(self) -> None:
        integrator = _ConcatTextIntegrator()
        p1 = IntegratedPayload(source_node="a", content="hello")
        p2 = IntegratedPayload(source_node="b", content="world")
        result = integrator.integrate([p1, p2])
        assert result.integrated_content == "hello | world"
        assert len(result.payloads) == 2

    def test_custom_integrator_empty(self) -> None:
        integrator = _ConcatTextIntegrator()
        result = integrator.integrate([])
        assert result.integrated_content == ""
        assert result.payloads == []
