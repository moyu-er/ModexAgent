"""LLM 流式事件层 —— LLMStreamEvent 六变体封闭联合。

协议引擎(openai_compat / openai_responses / anthropic)把 SSE 帧翻译为本
模块的事件变体, EventAssembler(T9)再把事件序列折叠为一个 LLMResponse。
``Finish.replay``(``ReplayFields``)是事件层唯一的思维链回放传输通道。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .llm_struct import FinishReason, LLMErrorInfo, LLMErrorKind, LLMResponse, TokenUsage
from .message import ToolCall


class ReplayFields(BaseModel):
    """一次响应的思维链回放状态。

    由 anthropic/responses 引擎在流终结时经 ``Finish.replay`` 交付,
    assembler 组装进 LLMResponse 对应字段(reasoning_content /
    reasoning_signature / reasoning_item_id / reasoning_encrypted_content)。
    它是事件层唯一的回放传输通道, 引擎不暴露任何 per-response 实例方法/
    属性。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    reasoning_item_id: str | None = None
    reasoning_encrypted_content: str | None = None


class TextDelta(BaseModel):
    """正文文本增量。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["text_delta"] = "text_delta"
    text: str


class ReasoningDelta(BaseModel):
    """思维链文本增量。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["reasoning_delta"] = "reasoning_delta"
    text: str


class ToolCallComplete(BaseModel):
    """一个工具调用的完整(非流式)形态。

    引擎侧由 tool_stream 累积器在块终结/流终结时产出; ``call_id`` 是
    工具结果的配对键(responses 侧恒不携带 item_id)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["tool_call_complete"] = "tool_call_complete"
    call_id: str
    tool_name: str
    # 开放 JSON 参数载荷, 与 ToolCall.arguments 同型 (rule 14 开放扩展位)
    arguments: dict[str, Any]


class UsageSnapshot(BaseModel):
    """用量快照, 通常在流终结前产出一次。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["usage_snapshot"] = "usage_snapshot"
    usage: TokenUsage


class Finish(BaseModel):
    """正常终结事件。

    ``replay`` 携带本次响应的思维链回放状态(anthropic/responses 引擎);
    为 ``None`` 表示该协议无回放载荷。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["finish"] = "finish"
    finish_reason: FinishReason
    replay: ReplayFields | None = None


class StreamFailure(BaseModel):
    """失败终结事件。

    ``partial_content`` 是失败前已流出的正文前缀, assembler 将其拼接到
    已累积内容之前(保留已到达的部分)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stream_failure"] = "stream_failure"
    error_info: LLMErrorInfo
    partial_content: str = ""


LLMStreamEvent = Annotated[
    TextDelta | ReasoningDelta | ToolCallComplete | UsageSnapshot | Finish | StreamFailure,
    Field(discriminator="kind"),
]
"""判别联合(rule 15)。两条不变量:

1. **封闭联合, 变体仅增不改**: ``kind`` 判别值是封闭 ``Literal``, 新事件
   变体只能追加并同步扩展本联合, 既有变体的字段与语义永不修改。
2. **每个流必须以恰好一个 ``Finish`` 或 ``StreamFailure`` 终结**:
   EventAssembler 强制该终态不变量(EOF 无终态事件时组装器合成
   StreamFailure, PRD 第 6 章纪律 3)。
"""

# V2 候选(勿实现): ToolCallDelta(kind="tool_call_delta", call_id, tool_name, args_fragment: str)
#   —— 工具参数流式外露, 前提是出现真实消费方(如 WebUI 工具面板流式渲染);
#   事件变体是增量添加, 消费方用 match + 显式 ignore 分支处理未知变体


_EOF_WITHOUT_TERMINAL = "stream ended without terminal event"


class EventAssembler:
    """Fold one stream's events into one ``LLMResponse`` (one instance per stream).

    纯累加器, 无 I/O: 协议引擎把 SSE 帧翻译为封闭六变体事件联合, 本类是把
    事件序列折叠为一个响应的唯一所在。两个消费方共享它(ADR-0046):
    ``LLMProvider.chat_stream``(回调外观折叠)与 React LLM 事件循环(逐事件
    ``feed``)——组装字段必须与旧 ``_stream_with_control`` 响应形态对齐。

    终态不变量(PRD 第 6 章纪律 3): 每个流以恰好一个 ``Finish`` 或一个
    ``StreamFailure`` 终结。``result()`` 强制它——feed 序列耗尽而无终态事件
    时合成保留已累积内容的 TIMEOUT 错误响应。终态事件已 feed(或 ``result()``
    已调用)后再 feed 抛 ``RuntimeError``: 宁可大声失败, 不静默合并两个流。

    组装规则:

    - ``content`` / ``reasoning_content`` 是累积的增量文本, ``Finish`` 路径上
      以 ``or None`` 归一(legacy 形态); ``completion_start_time`` 是首个事件的
      墙钟时间渲染为 UTC ISO 字符串。
    - ``Finish.replay`` 是思维链字段的权威来源: 存在时 signature / item_id /
      encrypted_content 取自它; ``reasoning_content`` 在 replay 值非 ``None``
      时取 replay 值(引擎最终值), 回退到累积的 ``ReasoningDelta`` 文本。
    - ``StreamFailure.partial_content`` 拼接到已累积正文**之前**(它是失败前
      已流出的正文前缀); 错误响应其余部分保持旧 ``build_timeout_response``
      形态(仅 content + error + error_info)。
    - 工具累积不在此处: 引擎持有 ``ToolStream`` 累加器, 只为已完成的调用产出
      ``ToolCallComplete``(LENGTH pending-drop 规则活在引擎侧, tool_stream
      契约 2)。
    """

    def __init__(
        self,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
    ) -> None:
        self._on_content_delta = on_content_delta
        self._on_reasoning_delta = on_reasoning_delta
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._usage = TokenUsage()
        self._terminal: Finish | StreamFailure | None = None
        self._first_event_time: float | None = None
        self._closed = False

    async def feed(self, event: LLMStreamEvent) -> None:
        """Dispatch one event onto the accumulator.

        Raises:
            RuntimeError: the stream already reached its terminal state — a
                ``Finish``/``StreamFailure`` was fed, or ``result()`` was
                called. A stream ends with exactly one terminal event.
        """
        if self._closed:
            raise RuntimeError(
                "EventAssembler received an event after the terminal state "
                "(Finish/StreamFailure already fed or result() already called)"
            )
        if self._first_event_time is None:
            self._first_event_time = time.time()
        match event:
            case TextDelta():
                if event.text:
                    self._content_parts.append(event.text)
                    await self._invoke_callback(self._on_content_delta, event.text)
            case ReasoningDelta():
                if event.text:
                    self._reasoning_parts.append(event.text)
                    await self._invoke_callback(self._on_reasoning_delta, event.text)
            case ToolCallComplete():
                self._tool_calls.append(
                    ToolCall(
                        call_id=event.call_id,
                        tool_name=event.tool_name,
                        arguments=event.arguments,
                    )
                )
            case UsageSnapshot():
                # Later snapshots win (engines may emit interim + final usage).
                self._usage = event.usage
            case Finish() | StreamFailure():
                self._terminal = event
                self._closed = True

    def result(self) -> LLMResponse:
        """Assemble the final ``LLMResponse`` from the accumulated state.

        Idempotent — the closed state yields an equal response on every
        call — and closing: a subsequent ``feed`` raises ``RuntimeError``
        even when no terminal event was ever fed (EOF case).
        """
        self._closed = True
        terminal = self._terminal
        match terminal:
            case None:
                # Stream exhausted without Finish/StreamFailure: synthesize
                # the TIMEOUT error response, keeping what arrived.
                return LLMResponse(
                    content="".join(self._content_parts),
                    finish_reason=FinishReason.ERROR,
                    error=_EOF_WITHOUT_TERMINAL,
                    error_info=LLMErrorInfo(
                        kind=LLMErrorKind.TIMEOUT,
                        message=_EOF_WITHOUT_TERMINAL,
                        should_retry=True,
                    ),
                )
            case StreamFailure():
                # partial_content is the pre-failure body prefix — it goes
                # in front of the content accumulated from TextDelta events.
                return LLMResponse(
                    content=terminal.partial_content + "".join(self._content_parts),
                    finish_reason=FinishReason.ERROR,
                    error=terminal.error_info.message,
                    error_info=terminal.error_info,
                )
            case Finish():
                replay = terminal.replay
                reasoning_content = "".join(self._reasoning_parts) or None
                if replay is not None and replay.reasoning_content is not None:
                    # Engine's final value wins over the accumulated deltas.
                    reasoning_content = replay.reasoning_content
                return LLMResponse(
                    content="".join(self._content_parts) or None,
                    tool_calls=self._tool_calls,
                    reasoning_content=reasoning_content,
                    reasoning_signature=(
                        replay.reasoning_signature if replay is not None else None
                    ),
                    reasoning_item_id=replay.reasoning_item_id if replay is not None else None,
                    reasoning_encrypted_content=(
                        replay.reasoning_encrypted_content if replay is not None else None
                    ),
                    finish_reason=terminal.finish_reason,
                    usage=self._usage,
                    completion_start_time=(
                        datetime.fromtimestamp(self._first_event_time, tz=UTC).isoformat()
                        if self._first_event_time is not None
                        else None
                    ),
                )

    @staticmethod
    async def _invoke_callback(callback: Callable[[str], Any] | None, value: str) -> None:
        """Invoke a sync-or-async delta callback (legacy provider pattern)."""
        if callback is None or not value:
            return
        result = callback(value)
        if asyncio.iscoroutine(result):
            await result


__all__ = [
    "EventAssembler",
    "Finish",
    "LLMStreamEvent",
    "ReasoningDelta",
    "ReplayFields",
    "StreamFailure",
    "TextDelta",
    "ToolCallComplete",
    "UsageSnapshot",
]
