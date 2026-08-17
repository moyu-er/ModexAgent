import pytest
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter

from modex_agent.core.types import OutputMessage


@pytest.mark.asyncio
async def test_approval_message_emits_structured_envelope() -> None:
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
    env = q.get_nowait()
    assert env.event_type == "approval_request"
    assert env.payload["tool_call_id"] == "c1"
    assert env.payload["tool_name"] == "write_file"
    assert env.payload["tier"] == "dangerous"


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
