from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import SessionMeta, WebUIEventType
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ToolArgsDeltaPayload, ToolCallEndPayload
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.events import EmitterConfig
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.llm_struct import FinishReason
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ToolCallComplete,
    ToolCallDelta,
)
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.tool_manager import ToolResult
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager
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


async def test_tool_args_delta_seam_reaches_ws_and_skips_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A↔B 接缝贯通: ReactLlmClient 以 ``emit(TOOL_ARGS_DELTA,
    ToolArgsDeltaPayload)`` 的形态发出的参数增量, 真实 WebBotEmitter 必须
    投影为 ToolArgsDeltaEvent 信封且不污染 transcript; 随后的
    tool_call_start/end 配对不受预热信号影响。节流窗口置 0 保证逐条确定性。
    """
    monkeypatch.setattr(
        "bot.webui.emitter.web_bot._TOOL_ARGS_THROTTLE_SECONDS", 0.0
    )
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
    call = ToolCall(
        tool_name="write_file",
        arguments={"path": "a.txt", "content": "x" * 500},
        call_id="call-1",
    )
    result = ToolResult.from_text("write_file", "ok", call_id="call-1")

    with bind_workspace_root(tmp_path):
        for fragment in ('{"path": "a.t', 'xt", "content": "', "xxx"):
            await emitter.emit(
                ReActEvent.TOOL_ARGS_DELTA,
                ToolArgsDeltaPayload(
                    call_id="call-1",
                    tool_name="write_file",
                    args_fragment=fragment,
                ),
            )
        await emitter.emit(ReActEvent.TOOL_CALL_START, call)
        await emitter.emit(
            ReActEvent.TOOL_CALL_END,
            ToolCallEndPayload(tool_call=call, result=result, seq=0),
        )

    envelopes = [call_args.args[0] for call_args in output.send_envelope.await_args_list]
    args_deltas = [
        envelope
        for envelope in envelopes
        if envelope.event_type == WebUIEventType.TOOL_ARGS_DELTA
    ]
    assert len(args_deltas) == 3
    first = args_deltas[0]
    assert first.payload["tool"] == "write_file"
    assert first.payload["call_id"] == "call-1"
    assert first.payload["chars"] == len('{"path": "a.t')
    assert first.payload["preview"] == '{"path": "a.t'
    last = args_deltas[-1]
    assert last.payload["chars"] == len('{"path": "a.txt", "content": "xxx')
    assert last.payload["preview"].endswith("xxx")

    transcript = JSONLTranscriptStore(tmp_path / ".modex" / "sessions" / "main")
    turns = await transcript.load_materialized_by_prefix("conv")
    assert len(turns) == 1
    # 预热信号不落盘: transcript 只含配对完成的工具块
    assert turns[0].blocks == [
        {
            "kind": "tool",
            "tool": "write_file",
            "args": {"path": "a.txt", "content": "x" * 500},
            "result": "ok",
        }
    ]


_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - key: a
      name: "A"
      base_url: u
      interface_format: openai_compatible
      api_key: k
      models:
        - {name: M1, model: m1}
"""


class _NativeStreamFake:
    """Seeded cache entry: the native event stream a real engine produces."""

    def __init__(self, events: list[LLMStreamEvent]) -> None:
        self._events = events

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        for event in self._events:
            yield event


async def test_tool_args_delta_composition_provider_client_emitter_ws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全链回归锁: BotModelProvider → ReactLlmClient → WebBotEmitter → WS。

    原缺陷正藏在这条组合链里——包装层还是回调折叠时, ToolCallDelta 死在
    chat_stream 内部, 而引擎侧与 emitter 侧单测(各绕过包装层)全绿。种子缓存
    里的原生事件流必须经真实 wrapper/客户端/emitter 三段后以心跳信封落地。
    """
    monkeypatch.setattr("bot.webui.emitter.web_bot._TOOL_ARGS_THROTTLE_SECONDS", 0.0)

    yml = tmp_path / "model.yml"
    yml.write_text(_YML, encoding="utf-8")
    cfg = BotModelConfig.from_yaml(yml)
    resolved = cfg.default_resolved()
    provider = BotModelProvider(cfg)
    provider._cache[("a", "m1")] = _NativeStreamFake(  # type: ignore[attr-defined]
        [
            ToolCallDelta(
                call_id="call-1", tool_name="write_file", args_fragment='{"path": "a'
            ),
            ToolCallDelta(call_id="call-1", tool_name="write_file", args_fragment='.txt"}'),
            ToolCallComplete(
                call_id="call-1", tool_name="write_file", arguments={"path": "a.txt"}
            ),
            Finish(finish_reason=FinishReason.TOOL_CALLS),
        ]
    )

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    output = MagicMock()
    output.send_envelope = AsyncMock()
    # ReactLlmClient gates per-delta emits on wants_streaming(); the WS
    # adapter declares NATIVE in production — the mock must too.
    output.streaming_mode = StreamingMode.NATIVE
    emitter = WebBotEmitter(
        output_adapter=output,
        session_id="conv.main",
        config=EmitterConfig(),
        pool="main",
        transcript_store=store,
        session_meta_resolver=lambda: SessionMeta(parent_session_id=None),
    )

    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="main", session=SessionInfo.from_str("conv.main"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    from modex_agent.core.agent import AgentContext

    ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("conv.main"),
        max_iterations=5,
        identity=state.identity,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )
    ctx.emitter = emitter

    try:
        client = ReactLlmClient(provider)
        with bind_workspace_root(tmp_path):
            response = await client.call(
                [ChatMessage(role=MessageRole.USER, content="hi")], ctx
            )
    finally:
        await provider.aclose()

    # The loop also assembled the completed call from the delegated stream.
    assert response.tool_calls is not None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == {"path": "a.txt"}

    envelopes = [
        call_args.args[0] for call_args in output.send_envelope.await_args_list
    ]
    args_deltas = [
        envelope
        for envelope in envelopes
        if envelope.event_type == WebUIEventType.TOOL_ARGS_DELTA
    ]
    assert len(args_deltas) == 2
    assert args_deltas[0].payload["tool"] == "write_file"
    assert args_deltas[0].payload["call_id"] == "call-1"
    assert args_deltas[0].payload["chars"] == len('{"path": "a')
    assert args_deltas[-1].payload["chars"] == len('{"path": "a.txt"}')
