"""Unit tests for ``modex_agent.acp.events_map`` — the pure event mappers.

Every mapper is a pure function (no I/O), so these tests cover the full
mapping table: text/reasoning chunks, tool-call start/update, the tool-kind
inference table, the ``{turn_id}:{call_id}`` id format, and the full
framework ``StopReason`` -> ACP stop-reason table.
"""

import pytest
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)

from modex_agent.acp import events_map
from modex_agent.core.emitter import StopReason
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.turn_events import (
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

TURN_ID = "turn-abc"


# ---------------------------------------------------------------------------
# tool_call_id format
# ---------------------------------------------------------------------------


def test_tool_call_id_joins_turn_and_call() -> None:
    assert events_map.tool_call_id_for("t1", "c9") == "t1:c9"


# ---------------------------------------------------------------------------
# tool kind inference table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("Read", "read"),
        ("read_file", "read"),
        ("Glob", "read"),
        ("grep", "read"),
        ("Write", "edit"),
        ("Edit", "edit"),
        ("apply_patch", "edit"),
        ("Bash", "execute"),
        ("shell_exec", "execute"),
        ("WebFetch", "fetch"),
        ("fetch_url", "fetch"),
        ("Think", "think"),
        ("think_tool", "think"),
        ("TodoWrite", "edit"),
        ("SomethingElse", "other"),
    ],
)
def test_infer_tool_kind(tool_name: str, expected: str) -> None:
    assert events_map.infer_tool_kind(tool_name) == expected


# ---------------------------------------------------------------------------
# text / reasoning chunks
# ---------------------------------------------------------------------------


def test_text_event_maps_to_agent_message_chunk() -> None:
    update = events_map.map_text_event(TurnTextEvent(text="hello"))
    assert isinstance(update, AgentMessageChunk)
    assert update.session_update == "agent_message_chunk"
    assert update.content.type == "text"
    assert update.content.text == "hello"


def test_reasoning_event_maps_to_agent_thought_chunk() -> None:
    update = events_map.map_reasoning_event(TurnReasoningEvent(text="hmm"))
    assert isinstance(update, AgentThoughtChunk)
    assert update.session_update == "agent_thought_chunk"
    assert update.content.type == "text"
    assert update.content.text == "hmm"


# ---------------------------------------------------------------------------
# tool call start / progress
# ---------------------------------------------------------------------------


def test_tool_call_event_maps_to_tool_call_start() -> None:
    event = TurnToolCallEvent(tool_name="Bash", call_id="call-1", arguments={"command": "ls"})
    update = events_map.map_tool_call_event(event, turn_id=TURN_ID)
    assert isinstance(update, ToolCallStart)
    assert update.session_update == "tool_call"
    assert update.tool_call_id == f"{TURN_ID}:call-1"
    assert update.title == "Bash"
    assert update.kind == "execute"
    assert update.status == "in_progress"
    assert update.raw_input == {"command": "ls"}


def test_tool_result_event_maps_to_completed_progress() -> None:
    event = TurnToolResultEvent(tool_name="Read", call_id="call-2", output="contents")
    update = events_map.map_tool_result_event(event, turn_id=TURN_ID)
    assert isinstance(update, ToolCallProgress)
    assert update.session_update == "tool_call_update"
    assert update.tool_call_id == f"{TURN_ID}:call-2"
    assert update.status == "completed"
    assert update.raw_output == "contents"


def test_tool_result_event_failed_maps_to_failed_progress() -> None:
    event = TurnToolResultEvent(tool_name="Read", call_id="call-3", output="boom")
    update = events_map.map_tool_result_event(event, turn_id=TURN_ID, failed=True)
    assert update.status == "failed"
    assert update.raw_output == "boom"


# ---------------------------------------------------------------------------
# dispatcher on the discriminated union
# ---------------------------------------------------------------------------


def test_map_turn_event_dispatches_on_kind() -> None:
    cases = [
        (TurnTextEvent(text="t"), AgentMessageChunk),
        (TurnReasoningEvent(text="r"), AgentThoughtChunk),
        (TurnToolCallEvent(tool_name="Read", call_id="c", arguments={}), ToolCallStart),
        (TurnToolResultEvent(tool_name="Read", call_id="c", output="o"), ToolCallProgress),
    ]
    for event, expected_type in cases:
        update = events_map.map_turn_event(event, turn_id=TURN_ID)
        assert type(update) is expected_type


# ---------------------------------------------------------------------------
# stop reason table (every framework member must be covered)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (StopReason.COMPLETED, "end_turn"),
        (StopReason.CANCELLED, "cancelled"),
        (StopReason.TURN_CANCELLED, "cancelled"),
        (StopReason.MAX_ITERATIONS, "max_turn_requests"),
        (StopReason.LOOP_DETECTED, "max_turn_requests"),
        (StopReason.ERROR, "refusal"),
        (StopReason.TIMEOUT, "refusal"),
        (StopReason.MISSED_COMMUNICATION, "refusal"),
        (StopReason.COMMAND_INTERCEPTED, "refusal"),
        (StopReason.DUPLICATE, "refusal"),
    ],
)
def test_map_stop_reason(reason: StopReason, expected: str) -> None:
    assert events_map.map_stop_reason(reason) == expected


def test_map_stop_reason_covers_every_member() -> None:
    for reason in StopReason:
        assert events_map.map_stop_reason(reason) in {
            "end_turn",
            "cancelled",
            "max_turn_requests",
            "refusal",
        }


# ---------------------------------------------------------------------------
# history replay projection (typed ChatMessage facts -> wire updates)
# ---------------------------------------------------------------------------


def test_replay_user_message_becomes_user_chunk() -> None:
    updates = events_map.map_history_replay([ChatMessage(role=MessageRole.USER, content="hi")])
    assert len(updates) == 1
    update = updates[0]
    assert isinstance(update, UserMessageChunk)
    assert update.content.type == "text"
    assert update.content.text == "hi"


def test_replay_assistant_text_becomes_agent_chunk() -> None:
    updates = events_map.map_history_replay(
        [ChatMessage(role=MessageRole.ASSISTANT, content="hello")]
    )
    assert len(updates) == 1
    update = updates[0]
    assert isinstance(update, AgentMessageChunk)
    assert update.content.text == "hello"


def test_replay_assistant_tool_call_becomes_in_progress_card() -> None:
    message = ChatMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=[ToolCall(tool_name="Bash", arguments={"command": "ls"}, call_id="call-1")],
    )
    updates = events_map.map_history_replay([message])
    (update,) = updates
    assert isinstance(update, ToolCallStart)
    assert update.tool_call_id == "replay:0:call-1"
    assert update.title == "Bash"
    assert update.kind == "execute"
    assert update.status == "in_progress"
    assert update.raw_input == {"command": "ls"}


def test_replay_tool_result_completes_the_matching_card() -> None:
    messages = [
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[ToolCall(tool_name="Read", arguments={}, call_id="c9")],
        ),
        ChatMessage(role=MessageRole.TOOL, content="contents", tool_call_id="c9", name="Read"),
    ]
    updates = events_map.map_history_replay(messages)
    assert [type(u) for u in updates] == [ToolCallStart, ToolCallProgress]
    progress = updates[1]
    assert isinstance(progress, ToolCallProgress)
    assert progress.tool_call_id == "replay:0:c9"
    assert progress.status == "completed"
    assert progress.raw_output == "contents"


def test_replay_skips_unmatched_tool_result_and_non_client_roles() -> None:
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content="hidden"),
        ChatMessage(role=MessageRole.TOOL, content="orphan", tool_call_id="ghost", name="X"),
        ChatMessage(role=MessageRole.USER, content="kept"),
    ]
    updates = events_map.map_history_replay(messages)
    assert len(updates) == 1
    assert isinstance(updates[0], UserMessageChunk)


def test_replay_preserves_true_order_and_namespaces_by_message_index() -> None:
    messages = [
        ChatMessage(role=MessageRole.USER, content="q"),
        ChatMessage(role=MessageRole.ASSISTANT, content="a"),
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[ToolCall(tool_name="Bash", arguments={}, call_id="c1")],
        ),
        ChatMessage(role=MessageRole.USER, content="q2"),
    ]
    updates = events_map.map_history_replay(messages)
    assert [type(u) for u in updates] == [
        UserMessageChunk,
        AgentMessageChunk,
        ToolCallStart,
        UserMessageChunk,
    ]
    assert isinstance(updates[2], ToolCallStart)
    assert updates[2].tool_call_id == "replay:2:c1"


def test_replay_empty_history_produces_no_updates() -> None:
    assert events_map.map_history_replay([]) == []
