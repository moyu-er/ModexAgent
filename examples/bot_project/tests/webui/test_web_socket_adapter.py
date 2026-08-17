"""Tests for WebSocket input/output adapters."""

from __future__ import annotations

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.events import DeltaEnvelope, WebUIEventType

from modex_agent.adapters.platform import StreamingMode


@pytest.mark.asyncio
async def test_input_adapter_name() -> None:
    adapter = WebSocketInputAdapter()
    assert adapter.name == "websocket"


@pytest.mark.asyncio
async def test_output_adapter_streaming_mode() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    assert output_adapter.streaming_mode == StreamingMode.NATIVE


@pytest.mark.asyncio
async def test_send_delta_routes_to_session() -> None:
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)
    await output_adapter.send_delta("hello", "sess1")
    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    envelope = q.get_nowait()
    assert envelope.event_type == "content"
    assert envelope.payload == {"text": "hello"}


@pytest.mark.asyncio
async def test_enqueue_user_message_and_receive() -> None:
    """Verify enqueue_user_message → receive yields the InputMessage."""
    adapter = WebSocketInputAdapter()
    adapter.enqueue_user_message("sess1", "hello world")
    gen = adapter.receive()
    msg = await gen.__anext__()
    assert msg.content == "hello world"
    assert msg.session.agent_name == "main"
    assert msg.channel == "websocket"


@pytest.mark.asyncio
async def test_unregister_connection_cleanup() -> None:
    """Verify unregister removes both connection and delta queue."""
    adapter = WebSocketInputAdapter()
    adapter.register_connection("sess1", None)
    assert "sess1" in adapter._delta_queues
    adapter.unregister_connection("sess1", None)
    assert "sess1" not in adapter._delta_queues


@pytest.mark.asyncio
async def test_send_delta_to_unregistered_session_is_noop() -> None:
    """Sending a delta to an unregistered session should silently no-op."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    # Should not raise — unregistered session, no queue
    await output_adapter.send_delta("ghost", "nonexistent")


@pytest.mark.asyncio
async def test_delta_queue_drops_when_full() -> None:
    """A full delta queue must drop new deltas instead of growing unbounded.

    Deltas are transient UI refresh; dropping them under backpressure protects
    memory when a client disconnects or lags, without crashing the agent.
    """
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("sess1", None)
    q = input_adapter.get_delta_queue("sess1", None)
    assert q is not None
    capacity = q.maxsize

    # Fill the queue exactly to capacity.
    for i in range(capacity):
        await output_adapter.send_delta(f"chunk-{i}", "sess1")
    assert q.qsize() == capacity

    # One more beyond capacity: must not raise and must not grow the queue.
    await output_adapter.send_delta("overflow", "sess1")
    assert q.qsize() == capacity


@pytest.mark.asyncio
async def test_duplicate_tabs_same_session_each_receive_stream() -> None:
    """Multicast: two connections attached to the SAME session (duplicate
    workspace tabs) each own a queue and each receive every delta."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    tab_a, tab_b = object(), object()
    qa = input_adapter.register_connection("sess1", tab_a)
    qb = input_adapter.register_connection("sess1", tab_b)
    assert qa is not qb

    await output_adapter.send_delta("chunk", "sess1")

    assert qa.get_nowait().payload == {"text": "chunk"}
    assert qb.get_nowait().payload == {"text": "chunk"}


@pytest.mark.asyncio
async def test_surviving_tab_keeps_stream_after_duplicate_closes() -> None:
    """Closing one duplicate tab must not kill the survivor's stream:
    unregister removes only that connection's queue; the session entry lives
    on until the last connection detaches."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    tab_a, tab_b = object(), object()
    input_adapter.register_connection("sess1", tab_a)
    qb = input_adapter.register_connection("sess1", tab_b)

    input_adapter.unregister_connection("sess1", tab_a)

    assert input_adapter.get_delta_queue("sess1", tab_a) is None
    assert input_adapter.get_delta_queue("sess1", tab_b) is qb
    assert "sess1" in input_adapter._delta_queues
    await output_adapter.send_delta("still-live", "sess1")
    assert qb.get_nowait().payload == {"text": "still-live"}

    # Last detach drops the session entry entirely.
    input_adapter.unregister_connection("sess1", tab_b)
    assert "sess1" not in input_adapter._delta_queues


@pytest.mark.asyncio
async def test_ensure_queue_buffer_adopted_by_first_registrant() -> None:
    """A pre-dispatch buffer (ensure_queue) keeps early subagent deltas and is
    adopted by the first connection to register the session; a second
    connection gets a fresh queue (no backlog replay)."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    pending = input_adapter.ensure_queue("sub.sess.main")
    await output_adapter.send_delta("early", "sub.sess.main")
    assert pending.qsize() == 1

    tab_a, tab_b = object(), object()
    qa = input_adapter.register_connection("sub.sess.main", tab_a)
    assert qa is pending  # adopted, backlog preserved
    assert qa.get_nowait().payload == {"text": "early"}

    qb = input_adapter.register_connection("sub.sess.main", tab_b)
    assert qb is not pending
    assert qb.empty()

    # From now on both receive via fan-out.
    await output_adapter.send_delta("live", "sub.sess.main")
    assert qa.get_nowait().payload == {"text": "live"}
    assert qb.get_nowait().payload == {"text": "live"}


@pytest.mark.asyncio
async def test_unclaimed_buffer_dropped_on_turn_end() -> None:
    """An anonymous pre-attach buffer no connection ever claimed is dropped
    when the session's turn ends — abandoned subagent sessions (e.g. IM-driven
    turns no browser opens) must not accumulate queue entries. The genealogy
    link is RETAINED (append-only): late envelopes still resolve parent ids."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_subagent("sub.sess.main", "parent.main")
    await output_adapter.send_delta("work", "sub.sess.main")
    assert "sub.sess.main" in input_adapter._delta_queues

    await output_adapter.send_envelope(
        DeltaEnvelope(
            session_id="sub.sess.main",
            agent_name="main",
            event_type=WebUIEventType.TURN_END.value,
            payload={},
        )
    )

    assert "sub.sess.main" not in input_adapter._delta_queues
    assert input_adapter.get_parent("sub.sess.main") == "parent.main"


@pytest.mark.asyncio
async def test_watched_parent_keeps_buffer_past_turn_end() -> None:
    """A live connection anywhere up the parent chain means a watcher will
    claim the buffer within one poll interval — turn_end must NOT reclaim it,
    or attached browsers lose subagent turns that complete in <1s."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    input_adapter.register_connection("parent.main", object())
    input_adapter.register_subagent("sub.sess.main", "parent.main")

    await output_adapter.send_envelope(
        DeltaEnvelope(
            session_id="sub.sess.main",
            agent_name="main",
            event_type=WebUIEventType.TURN_END.value,
            payload={},
        )
    )

    assert "sub.sess.main" in input_adapter._delta_queues


@pytest.mark.asyncio
async def test_genealogy_survives_detach_for_late_envelopes() -> None:
    """A browser detaching mid-conversation must not erase the parent link:
    IM-driven continuation envelopes emitted afterwards still carry correct
    genealogy (SessionTree / transcript metadata)."""
    input_adapter = WebSocketInputAdapter()
    ws = object()
    input_adapter.register_connection("sub.sess.main", ws)
    input_adapter.register_subagent("sub.sess.main", "parent.main")

    input_adapter.unregister_connection("sub.sess.main", ws)

    assert "sub.sess.main" not in input_adapter._delta_queues
    assert input_adapter.get_parent("sub.sess.main") == "parent.main"


def test_ancestors_walks_chain_and_stops_on_cycles() -> None:
    """ancestors() yields nearest-first chains and terminates on corrupted
    (cyclic) links instead of looping forever."""
    input_adapter = WebSocketInputAdapter()
    input_adapter.register_subagent("c", "b")
    input_adapter.register_subagent("b", "a")
    assert list(input_adapter.ancestors("c")) == ["b", "a"]
    assert list(input_adapter.ancestors("a")) == []

    broken = WebSocketInputAdapter()
    broken._parent_map["x"] = "y"
    broken._parent_map["y"] = "x"
    assert sorted(broken.ancestors("x")) == ["x", "y"]


@pytest.mark.asyncio
async def test_claimed_queue_survives_turn_end() -> None:
    """A queue owned by a real connection is NOT dropped on turn_end — the
    attached tab keeps its stream for the next turn, and the turn_end
    envelope itself is delivered first."""
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    ws = object()
    q = input_adapter.register_connection("sess.main", ws)

    await output_adapter.send_envelope(
        DeltaEnvelope(
            session_id="sess.main",
            agent_name="main",
            event_type=WebUIEventType.TURN_END.value,
            payload={},
        )
    )

    assert input_adapter.get_delta_queue("sess.main", ws) is q
    assert q.get_nowait().event_type == WebUIEventType.TURN_END.value

