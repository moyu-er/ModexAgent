"""Tests for the generic ToolStream accumulator (providers/http).

Covers the three module contracts: stream key != call_id accumulation
(including interleaved parallel fragments), caller-owned LENGTH pending-drop
(not testable here by design), and zero-argument calls finishing with
``arguments={}``.
"""

import logging

import pytest

from modex_agent.providers.http.tool_stream import (
    PendingTool,
    ToolStreamError,
    append_existing,
    append_or_start,
    finish,
    finish_all,
    finish_with_input,
    start,
)

MODULE_LOGGER = "modex_agent.providers.http.tool_stream"


def accumulate(state, key, call_id, name, fragments):
    """Build pending state via start + append_existing (anthropic block shape)."""
    state = start(state, key, call_id, name)
    for fragment in fragments:
        state, _ = append_existing(state, key, fragment)
    return state


class TestStart:
    @pytest.mark.parametrize("key", [0, 3, "item_A", "resp_9"])
    def test_registers_tool_with_empty_input(self, key):
        state = start({}, key, "call_1", "get_weather")

        assert state == {key: PendingTool(id="call_1", name="get_weather", input="")}


class TestAppendOrStart:
    @pytest.mark.parametrize(
        ("key", "fragments", "expected"),
        [
            (0, ['{"city":', ' "Par', 'is"}'], {"city": "Paris"}),
            ("item_0", ['{"q": 1', ', "r": [1,', " 2]}"], {"q": 1, "r": [1, 2]}),
            (7, ['{"single": true}'], {"single": True}),
        ],
    )
    def test_sequential_fragments_accumulate(self, key, fragments, expected):
        state, first = append_or_start({}, key, "call_1", "get_weather", fragments[0])

        assert first == PendingTool(id="call_1", name="get_weather", input=fragments[0])
        for fragment in fragments[1:]:
            state, tool = append_or_start(state, key, None, None, fragment)
            assert tool.id == "call_1"
            assert tool.name == "get_weather"
        assert state[key].input == "".join(fragments)
        _, call = finish(state, key)
        assert call.arguments == expected

    @pytest.mark.parametrize(("k0", "k1"), [(0, 1), ("item_a", "item_b"), (0, "item_b")])
    def test_interleaved_fragments_accumulate_independently(self, k0, k1):
        state, _ = append_or_start({}, k0, "call_0", "tool_a", '{"a":')
        state, _ = append_or_start(state, k1, "call_1", "tool_b", '{"b":')
        state, _ = append_or_start(state, k0, None, None, " 1}")
        state, _ = append_or_start(state, k1, None, None, " 2}")

        state, call_b = finish(state, k1)
        state, call_a = finish(state, k0)

        assert call_a.call_id == "call_0"
        assert call_a.tool_name == "tool_a"
        assert call_a.arguments == {"a": 1}
        assert call_b.call_id == "call_1"
        assert call_b.tool_name == "tool_b"
        assert call_b.arguments == {"b": 2}
        assert state == {}

    @pytest.mark.parametrize(
        ("delta_id", "delta_name"),
        [(None, None), ("call_1", None), (None, "tool_a"), ("", "")],
    )
    def test_missing_identity_raises(self, delta_id, delta_name):
        with pytest.raises(ToolStreamError):
            append_or_start({}, 0, delta_id, delta_name, '{"a": 1}')


class TestAppendExisting:
    @pytest.mark.parametrize("key", [4, "item_B"])
    def test_unknown_key_raises(self, key):
        with pytest.raises(ToolStreamError):
            append_existing({}, key, '{"a": 1}')

    @pytest.mark.parametrize("key", [1, "item_C"])
    def test_empty_fragment_is_noop(self, key):
        state = accumulate({}, key, "call_1", "tool_a", ['{"a": 1'])

        new_state, tool = append_existing(state, key, "")

        assert new_state == state
        assert tool.input == '{"a": 1'

    def test_appends_after_start(self):
        state = start({}, 0, "call_1", "tool_a")

        state, tool = append_existing(state, 0, '{"x":')
        state, tool = append_existing(state, 0, " 42}")

        assert tool.input == '{"x": 42}'
        assert state == {0: PendingTool(id="call_1", name="tool_a", input='{"x": 42}')}


class TestFinish:
    @pytest.mark.parametrize("key", [0, "item_A"])
    def test_zero_deltas_yield_empty_arguments(self, key):
        state = start({}, key, "call_1", "get_weather")

        new_state, call = finish(state, key)

        assert call.call_id == "call_1"
        assert call.tool_name == "get_weather"
        assert call.arguments == {}
        assert new_state == {}

    @pytest.mark.parametrize(
        ("fragments", "expected"),
        [
            (['{"a": 1}'], {"a": 1}),
            (['{"a":', " 2}"], {"a": 2}),
            (['{"nested": {"k": [1, 2]}}'], {"nested": {"k": [1, 2]}}),
            ([], {}),
        ],
    )
    def test_parses_accumulated_json(self, fragments, expected):
        state = accumulate({}, 0, "call_1", "tool_a", fragments)

        _, call = finish(state, 0)

        assert call.arguments == expected

    @pytest.mark.parametrize("broken", ['{"a": 1', '{"a": ', "not json", '{"a": "unclosed'])
    def test_broken_json_degrades_to_empty_and_logs(self, broken, caplog):
        state = accumulate({}, 0, "call_1", "tool_a", [broken])

        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            _, call = finish(state, 0)

        assert call.arguments == {}
        assert "tool_a" in caplog.text

    def test_broken_json_log_carries_200_char_prefix(self, caplog):
        raw = '{"a": "' + "x" * 300
        state = accumulate({}, 0, "call_1", "tool_a", [raw])

        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            finish(state, 0)

        assert raw[:200] in caplog.text
        assert raw[:201] not in caplog.text

    def test_removes_finished_key_and_keeps_others(self):
        state = start({}, 0, "call_0", "tool_a")
        state = start(state, 1, "call_1", "tool_b")

        new_state, call = finish(state, 0)

        assert call.call_id == "call_0"
        assert new_state == {1: PendingTool(id="call_1", name="tool_b", input="")}
        assert 0 in state

    @pytest.mark.parametrize("key", [5, "missing"])
    def test_unknown_key_raises(self, key):
        with pytest.raises(ToolStreamError):
            finish({}, key)


class TestFinishWithInput:
    @pytest.mark.parametrize(
        ("accumulated", "final_input", "expected"),
        [
            ('{"a": 1', '{"a": 42}', {"a": 42}),
            ('{"a": 1}', '{"b": 2}', {"b": 2}),
            ('{"a": 1', "", {}),
        ],
    )
    def test_final_input_overrides_accumulation(self, accumulated, final_input, expected):
        state = accumulate({}, 0, "call_1", "tool_a", [accumulated])

        new_state, call = finish_with_input(state, 0, final_input)

        assert call.call_id == "call_1"
        assert call.tool_name == "tool_a"
        assert call.arguments == expected
        assert new_state == {}

    @pytest.mark.parametrize("broken_final", ['{"x": ', "nope"])
    def test_broken_final_input_degrades_to_empty(self, broken_final, caplog):
        state = accumulate({}, 0, "call_1", "tool_a", ['{"ok": 1}'])

        with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
            _, call = finish_with_input(state, 0, broken_final)

        assert call.arguments == {}

    @pytest.mark.parametrize("key", [9, "missing"])
    def test_unknown_key_raises(self, key):
        with pytest.raises(ToolStreamError):
            finish_with_input({}, key, '{"a": 1}')


class TestFinishAll:
    @pytest.mark.parametrize("keys", [[2, 0, 1], ["c", "a", "b"], [4, "x", 0]])
    def test_insertion_order(self, keys):
        state = {}
        for key in keys:
            state = start(state, key, f"call_{key}", f"tool_{key}")

        new_state, calls = finish_all(state)

        assert [c.call_id for c in calls] == [f"call_{key}" for key in keys]
        assert new_state == {}

    def test_parses_each_and_empties_state(self):
        state = accumulate({}, 0, "call_1", "tool_a", ['{"x": 1}'])
        state = accumulate(state, 1, "call_2", "tool_b", [])
        state = accumulate(state, 2, "call_3", "tool_c", ['{"y": [1, 2]}'])

        new_state, calls = finish_all(state)

        assert [c.tool_name for c in calls] == ["tool_a", "tool_b", "tool_c"]
        assert [c.arguments for c in calls] == [{"x": 1}, {}, {"y": [1, 2]}]
        assert new_state == {}

    def test_empty_state_yields_no_calls(self):
        assert finish_all({}) == ({}, [])


class TestImmutability:
    def test_operations_never_mutate_input_state(self):
        state = start({}, 0, "call_1", "tool_a")
        state = start(state, 1, "call_2", "tool_b")
        snapshot = dict(state)

        state_after_append, _ = append_or_start(state, 0, None, None, '{"a":')
        state_after_existing, _ = append_existing(state_after_append, 0, " 1}")
        finish(state_after_existing, 0)
        finish_with_input(state, 1, '{"b": 2}')
        finish_all(state)

        assert state == snapshot
