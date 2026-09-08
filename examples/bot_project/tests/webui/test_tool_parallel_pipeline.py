from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import SessionMeta, WebUIEventType
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolCallEndPayload
from modex_agent.core.events import EmitterConfig
from modex_agent.core.message import ToolCall
from modex_agent.core.tool_manager import ToolResult
from modex_agent.workspace.runtime import bind_workspace_root


async def test_parallel_batches_round_trip_in_model_order(tmp_path: Path) -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    output = MagicMock()
    output.send_envelope = AsyncMock()
    emitter = WebBotEmitter(
        output_adapter=output,
        session_id="conv.main",
        config=EmitterConfig(),
        pool="main",
        transcript_store=store,
        session_meta_resolver=lambda: SessionMeta(parent_session_id=None),
    )
    calls = [
        ToolCall(
            tool_name="read_file",
            arguments={"path": label},
            call_id=f"call-{label}",
        )
        for label in ("A", "B", "C", "D")
    ]
    results = [
        ToolResult.from_text(
            "read_file", f"result-{label}", call_id=f"call-{label}"
        )
        for label in ("A", "B", "C", "D")
    ]

    with bind_workspace_root(tmp_path):
        for call in calls[:2]:
            await emitter.emit(ReActEvent.TOOL_CALL_START, call)
        for index in (1, 0):
            await emitter.emit(
                ReActEvent.TOOL_CALL_END,
                ToolCallEndPayload(
                    tool_call=calls[index],
                    result=results[index],
                    seq=index,
                ),
            )
        for call in calls[2:]:
            await emitter.emit(ReActEvent.TOOL_CALL_START, call)
        for index in (3, 2):
            await emitter.emit(
                ReActEvent.TOOL_CALL_END,
                ToolCallEndPayload(
                    tool_call=calls[index],
                    result=results[index],
                    seq=index,
                ),
            )

    transcript = JSONLTranscriptStore(tmp_path / ".modex" / "sessions" / "main")
    turns = await transcript.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    assert turns[0].blocks == [
        {
            "kind": "tool",
            "tool": "read_file",
            "args": {"path": "A"},
            "result": "result-A",
        },
        {
            "kind": "tool",
            "tool": "read_file",
            "args": {"path": "B"},
            "result": "result-B",
        },
        {
            "kind": "tool",
            "tool": "read_file",
            "args": {"path": "C"},
            "result": "result-C",
        },
        {
            "kind": "tool",
            "tool": "read_file",
            "args": {"path": "D"},
            "result": "result-D",
        },
    ]

    envelopes = [call.args[0] for call in output.send_envelope.await_args_list]
    starts = [
        envelope
        for envelope in envelopes
        if envelope.event_type == WebUIEventType.TOOL_CALL_START
    ]
    ends = [
        envelope
        for envelope in envelopes
        if envelope.event_type == WebUIEventType.TOOL_CALL_END
    ]
    assert [envelope.payload["call_id"] for envelope in starts] == [
        "call-A",
        "call-B",
        "call-C",
        "call-D",
    ]
    assert [envelope.payload["call_id"] for envelope in ends] == [
        "call-B",
        "call-A",
        "call-D",
        "call-C",
    ]
    assert [envelope.payload["seq"] for envelope in ends] == [1, 0, 3, 2]
