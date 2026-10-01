import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter

from modex_agent.messaging.models import OutputMessage


@pytest.mark.asyncio
async def test_approval_message_is_dropped_on_webui_channel() -> None:
    """The IM approval prompt OutputMessage no longer pushes a WebUI card.

    Approval cards stream from the turn sink (WebBotEmitter projects the
    presentation ApprovalRequested card onto the same WS envelope, one per
    suspension); the adapter-side push was its duplicate delivery. The IM
    text prompt is channel vocabulary — the WebUI drops it instead of
    rendering the raw prompt text as a content delta.
    """
    inp = WebSocketInputAdapter()
    inp.register_connection("s.main", None)
    out = WebSocketOutputAdapter(inp)
    await out.send(
        OutputMessage(
            content="Approval Required...\nTool: write_file",
            message_type="approval_request",
            metadata={"approval": {"tool_call_id": "c1", "tool_name": "write_file",
                                   "tier": "dangerous", "arguments": {"path": "a"}, "status": "pending"}},
        ),
        "s.main",
    )
    q = inp.get_delta_queue("s.main", None)
    assert q is not None
    assert q.empty()  # no card push, no prompt-text content delta


@pytest.mark.asyncio
async def test_normal_message_still_content_delta() -> None:
    inp = WebSocketInputAdapter()
    inp.register_connection("s.main", None)
    out = WebSocketOutputAdapter(inp)
    await out.send(OutputMessage(content="hello", message_type="text"), "s.main")
    q = inp.get_delta_queue("s.main", None)
    assert q is not None
    env = q.get_nowait()
    assert env.event_type == "content"  # unchanged path
    assert env.payload["text"] == "hello"
