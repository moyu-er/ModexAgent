"""Channel checkpoint round-trip tests: LastValue + ReducerChannel."""
from __future__ import annotations

from typing import Annotated, Any

from modex_graph import (
    BaseChannel,
    Codec,
    GraphState,
    LastValue,
    ReducerChannel,
    register_codec,
)


class SimpleState(GraphState):
    """State with LastValue + ReducerChannel fields."""

    count: Annotated[int, LastValue] = 0
    name: Annotated[str, LastValue] = "default"
    items: Annotated[list[str], ReducerChannel(reducer=lambda a, b: a + b)] = []


class TestLastValueCheckpoint:
    """LastValue channel checkpoint round-trip."""

    def test_lastvalue_round_trip_primitives(self) -> None:
        state = SimpleState(count=42, name="test", items=["a"])
        checkpoint = state.checkpoint()
        assert checkpoint["count"] == 42
        assert checkpoint["name"] == "test"
        assert checkpoint["items"] == ["a"]

        restored = SimpleState.from_checkpoint(checkpoint)
        assert restored.count == 42
        assert restored.name == "test"
        assert restored.items == ["a"]

    def test_lastvalue_round_trip_after_imperative_mutation(self) -> None:
        state = SimpleState(count=0, name="", items=[])
        state.count += 10
        state.name = "mutated"
        state.items = state.items + ["x"]
        checkpoint = state.checkpoint()
        assert checkpoint["count"] == 10
        assert checkpoint["name"] == "mutated"
        assert checkpoint["items"] == ["x"]

        restored = SimpleState.from_checkpoint(checkpoint)
        assert restored.count == 10
        assert restored.name == "mutated"
        assert restored.items == ["x"]

    def test_lastvalue_default_values_round_trip(self) -> None:
        state = SimpleState()
        checkpoint = state.checkpoint()
        assert checkpoint["count"] == 0
        assert checkpoint["name"] == "default"
        assert checkpoint["items"] == []

        restored = SimpleState.from_checkpoint(checkpoint)
        assert restored.count == 0
        assert restored.name == "default"
        assert restored.items == []


class TestReducerChannelCheckpoint:
    """ReducerChannel channel checkpoint round-trip."""

    def test_reducer_round_trip_after_multiple_updates(self) -> None:
        state = SimpleState(items=[])
        # Apply multiple state_updates to exercise the reducer.
        state.apply_state_update({"items": ["a"]})
        state.apply_state_update({"items": ["b", "c"]})
        state.apply_state_update({"items": ["d"]})
        assert state.items == ["a", "b", "c", "d"]

        checkpoint = state.checkpoint()
        assert checkpoint["items"] == ["a", "b", "c", "d"]

        restored = SimpleState.from_checkpoint(checkpoint)
        assert restored.items == ["a", "b", "c", "d"]

    def test_reducer_round_trip_empty_list(self) -> None:
        state = SimpleState(items=[])
        checkpoint = state.checkpoint()
        assert checkpoint["items"] == []
        restored = SimpleState.from_checkpoint(checkpoint)
        assert restored.items == []

    def test_reducer_imperative_mutation_round_trip(self) -> None:
        """Imperative mutation syncs to channel at checkpoint time."""
        state = SimpleState(items=["initial"])
        state.items = state.items + ["appended"]
        checkpoint = state.checkpoint()
        assert checkpoint["items"] == ["initial", "appended"]
        restored = SimpleState.from_checkpoint(checkpoint)
        assert restored.items == ["initial", "appended"]


class TestPydanticModelCheckpoint:
    """Pydantic BaseModel fields use model_dump/model_validate as universal codec."""

    def test_pydantic_model_field_round_trip(self) -> None:
        from pydantic import BaseModel

        class Inner(BaseModel):
            value: int = 0
            label: str = ""

        class StateWithModel(GraphState):
            inner: Annotated[Inner, LastValue] = Inner(value=1, label="hello")

        state = StateWithModel(inner=Inner(value=99, label="world"))
        checkpoint = state.checkpoint()
        # Inner is serialized as a dict via model_dump(mode="json")
        assert checkpoint["inner"] == {"value": 99, "label": "world"}

        restored = StateWithModel.from_checkpoint(checkpoint)
        assert restored.inner.value == 99
        assert restored.inner.label == "world"


class TestCustomCodec:
    """register_codec for non-Pydantic types."""

    def test_register_codec_for_custom_type(self) -> None:
        class CustomContainer:
            def __init__(self, data: list[int]) -> None:
                self.data = data

        register_codec(
            CustomContainer,
            Codec(
                encode=lambda v: {"d": v.data},
                decode=lambda d: CustomContainer(d["d"]),  # type: ignore[index]
            ),
        )

        # Use a typed field so decode_value knows to use the CustomContainer codec.
        class StateWithCustom(GraphState):
            custom: Annotated[CustomContainer, LastValue] = CustomContainer(data=[])

        state = StateWithCustom()
        state.custom = CustomContainer([1, 2, 3])
        checkpoint = state.checkpoint()
        assert checkpoint["custom"] == {"d": [1, 2, 3]}

        restored = StateWithCustom.from_checkpoint(checkpoint)
        assert isinstance(restored.custom, CustomContainer)
        assert restored.custom.data == [1, 2, 3]


class TestBaseChannelABC:
    """BaseChannel is the public extension seam."""

    def test_base_channel_is_abstract(self) -> None:
        import pytest

        with pytest.raises(TypeError):
            BaseChannel()  # type: ignore[abstract]

    def test_custom_channel_subclass(self) -> None:
        """A custom channel can be created by subclassing BaseChannel."""

        class AppendChannel(BaseChannel[Any]):
            """Appends values to a list (like ReducerChannel but always list)."""

            _field_type: Any = Any

            def __init__(self) -> None:
                self._value: list[Any] = []

            def update(self, values: list[Any]) -> None:
                for v in values:
                    self._value = self._value + v

            def set(self, value: Any) -> None:
                self._value = list(value) if isinstance(value, list) else [value]

            def get(self) -> Any:
                return self._value

            def checkpoint(self) -> Any:
                return self._value

            def restore(self, data: Any) -> None:
                self._value = list(data)

            def _fresh(self, field_type: Any) -> BaseChannel[Any]:
                ch = AppendChannel()
                ch._field_type = field_type
                return ch

        class StateWithAppend(GraphState):
            items: Annotated[list[str], AppendChannel()] = []

        state = StateWithAppend()
        state.apply_state_update({"items": ["a"]})
        state.apply_state_update({"items": ["b"]})
        assert state.items == ["a", "b"]

        checkpoint = state.checkpoint()
        assert checkpoint["items"] == ["a", "b"]
        restored = StateWithAppend.from_checkpoint(checkpoint)
        assert restored.items == ["a", "b"]
