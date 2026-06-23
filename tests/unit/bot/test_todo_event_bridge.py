import pytest

from bot.webui.events import TodoUpdatedEvent, WebUIEventType


def test_todo_updated_event_type_registered() -> None:
    assert WebUIEventType.TODO_UPDATED.value == "todo_updated"


def test_todo_updated_event_builds() -> None:
    evt = TodoUpdatedEvent(
        session_id="s1", agent_name="main", todos=[{"content": "a", "status": "pending"}]
    )
    assert evt.event == "todo_updated"
    assert evt.todos == [{"content": "a", "status": "pending"}]


class _RecordingOutput:
    def __init__(self) -> None:
        self.envelopes: list = []

    async def send_envelope(self, env) -> None:  # type: ignore[no-untyped-def]
        self.envelopes.append(env)


@pytest.mark.asyncio
async def test_emitter_forwards_todo_updated() -> None:
    """The framework todo tool emits a BARE STRING "todo.updated" (it stays
    decoupled from ReActEvent). The full emit() -> _on_event path must forward
    it without crashing on ``.value``. This is the real production shape —
    do not stub the event with a SimpleNamespace (that would mask the bug)."""
    from bot.webui.emitter import WebBotEmitter

    output = _RecordingOutput()
    emitter = WebBotEmitter(
        output_adapter=output,
        session_id="conv1.main",
    )
    await emitter.emit(
        "todo.updated",
        {"session_id": "conv1.main", "todos": [{"content": "a", "status": "pending"}]},
    )
    assert len(output.envelopes) == 1
    envelope = output.envelopes[0]
    # DeltaEnvelope moves the event type out of the payload into envelope.event_type;
    # the payload holds only event-specific fields (here: todos).
    assert envelope.event_type == "todo_updated"
    assert envelope.payload["todos"] == [{"content": "a", "status": "pending"}]
