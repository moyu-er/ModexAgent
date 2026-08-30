"""LLM 流式事件层 —— LLMStreamEvent 六变体封闭联合。

协议引擎(openai_compat / openai_responses / anthropic)把 SSE 帧翻译为本
模块的事件变体, EventAssembler(T9)再把事件序列折叠为一个 LLMResponse。
``Finish.replay``(``ReplayFields``)是事件层唯一的思维链回放传输通道。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .constants import FinishReason
from .llm_struct import LLMErrorInfo
from .types import TokenUsage


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


__all__ = [
    "Finish",
    "LLMStreamEvent",
    "ReasoningDelta",
    "ReplayFields",
    "StreamFailure",
    "TextDelta",
    "ToolCallComplete",
    "UsageSnapshot",
]
