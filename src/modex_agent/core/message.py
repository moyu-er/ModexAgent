"""Core message types — ChatMessage and ContentFormat.

Foundational message primitives used across the framework, not memory-specific.
Moved from framework.memory.core.message to break the core <-> memory cycle.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modex_agent.core.capabilities import Modality
from modex_agent.utils.timezone import get_user_timezone


class MessageRole(StrEnum):
    """LLM conversation role."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    AGENT = "agent"
    COMPACT = "compact"
    PENDING = "pending"
    SYSTEM_REMINDER = "system_reminder"


class ToolCall(BaseModel):
    """Tool invocation requested by an LLM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    arguments: dict[str, Any]
    call_id: str | None = None


class ContentFormat(StrEnum):
    """内容格式枚举。

    PLAIN  — 纯文本内容，可安全截断
    XML    — XML 结构化内容，支持按路径定位截断
    """

    PLAIN = "plain"
    XML = "xml"


class ContentPartType(StrEnum):
    """多模态内容部分类型（遵循 OpenAI content part 格式）。"""

    TEXT = "text"
    IMAGE_URL = "image_url"


class TextPart(BaseModel):
    """文本内容部分。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[ContentPartType.TEXT] = ContentPartType.TEXT
    text: str


class ImageUrl(BaseModel):
    """图片 URL 子结构。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    detail: str | None = None


class ImageUrlPart(BaseModel):
    """图片 URL 内容部分。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[ContentPartType.IMAGE_URL] = ContentPartType.IMAGE_URL
    image_url: ImageUrl


# V2 预留: FilePart(type="file", media_type, data|path, filename) —— 文档文件直传, 见 ADR-0046 Consequences
ContentPart = Annotated[TextPart | ImageUrlPart, Field(discriminator="type")]

MEDIA_URL_SCHEME: Final = "media"


def content_part_modality(part: ContentPart) -> Modality:
    """返回内容部分对应的模型模态。"""
    match part:
        case TextPart() | ImageUrlPart():
            return {ContentPartType.TEXT: Modality.TEXT, ContentPartType.IMAGE_URL: Modality.IMAGE}[part.type]
        case _:
            raise TypeError(part)


def build_media_ref(attachment_id: str) -> str:
    """构造持久媒体引用。"""
    return f"{MEDIA_URL_SCHEME}://{attachment_id}"


def parse_media_ref(url: str) -> str | None:
    """解析持久媒体引用，非有效引用时返回 None。"""
    return attachment_id or None if (attachment_id := url.removeprefix(build_media_ref(""))) != url else None


def render_content_part_ref(part: ContentPart) -> str:
    """将内容部分渲染为不携带媒体字节的引用文本。"""
    match part:
        case TextPart(text=text):
            return text
        case ImageUrlPart(image_url=ImageUrl(url=url)) if parse_media_ref(url) is not None:
            return f"[image: {url}]"
        case ImageUrlPart(image_url=ImageUrl(url=url)) if url.startswith("data:"):
            mime, marker, payload = url[5:].partition(";base64,")
            try:
                return f"[image: data:{mime}, {len(base64.b64decode(payload if marker else '%', validate=True))} bytes]"
            except ValueError:
                return f"[image: data:{mime}, ? bytes]"
        case ImageUrlPart(image_url=ImageUrl(url=url)):
            return f"[image: {url}]"
        case _:
            raise TypeError(part)


class ChatMessage(BaseModel):
    """聊天消息模型。

    固定字段：role、content、tool_calls、tool_call_id、name、
    reasoning 回放四字段（ADR-0046：reasoning_content / reasoning_signature /
    reasoning_item_id / reasoning_encrypted_content）
    未知字段：通过 model_config extra='allow' 保留
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: MessageRole = Field(..., description="消息角色: user / assistant / system / tool")
    content: str | list[ContentPart] | None = Field(
        default=None, description="消息内容，支持 str 或 list[ContentPart]（多模态）"
    )
    tool_calls: list[ToolCall] | None = Field(
        default=None, description="assistant 请求的工具调用列表"
    )
    tool_call_id: str | None = Field(default=None, description="tool 消息对应的 tool_call_id")
    name: str | None = Field(default=None, description="工具名称（OpenAI function calling）")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(get_user_timezone()).replace(microsecond=0),
        description="消息创建时间戳（用户配置时区，秒级精度）",
    )
    content_format: ContentFormat = Field(
        default=ContentFormat.PLAIN, description="内容格式：plain 或 xml"
    )
    truncatable_paths: list[str] | None = Field(
        default=None, description="XML 内容中可截断的路径列表"
    )
    token_count: int | None = Field(
        default=None,
        description="缓存的 token 计数（append 时由 TokenEstimator 计算并落盘）；"
        "None 表示未计算，触发/边界逻辑会现算。",
    )
    reasoning_content: str | None = Field(
        default=None,
        description="思维链文本（DeepSeek reasoning_content / Anthropic thinking）；"
        "随消息持久化，provider 层按协议规则条件回放（ADR-0046）",
    )
    reasoning_signature: str | None = Field(
        default=None,
        description="Anthropic extended thinking 签名，thinking 回放必需（ADR-0046）",
    )
    reasoning_item_id: str | None = Field(
        default=None,
        description="OpenAI Responses reasoning item id，item_reference 回放用（ADR-0046）",
    )
    reasoning_encrypted_content: str | None = Field(
        default=None,
        description="OpenAI Responses 加密思维链，store=false 完整回放用（ADR-0046）",
    )

    @field_validator("created_at", mode="before")
    @classmethod
    def _parse_created_at(cls, v: Any) -> datetime:
        """Parse created_at from string ("YYYY-MM-DD HH:MM:SS"), datetime, or int.

        Int values are interpreted as epoch milliseconds when >= 1e12 (ADR-0029
        storage format), otherwise epoch seconds. The threshold 1e12 corresponds
        to year ~2001 in milliseconds / year ~33658 in seconds — any real-world
        timestamp is unambiguously on one side.
        """
        tz = get_user_timezone()
        if isinstance(v, datetime):
            return v
        if isinstance(v, int | float):
            if v >= 1e12:
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=tz)
        if isinstance(v, str):
            try:
                return datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            except ValueError:
                pass
            return datetime.fromisoformat(v)
        return datetime.now(tz)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessage:
        """从 dict 构造 ChatMessage，保留未知字段。

        Pydantic 自动验证 dict→ToolCall / dict→ContentPart（discriminator）。
        兼容旧 OpenAI 格式 tool_calls（``{"id":.., "function":{"name":.., "arguments":..}}``）
        自动转换为 ToolCall 格式。
        """
        data = dict(data)
        if "tool_calls" in data and data["tool_calls"] is not None:
            converted: list[dict[str, Any]] = []
            for tc in data["tool_calls"]:
                if isinstance(tc, dict) and "function" in tc and "tool_name" not in tc:
                    fn = tc["function"]
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                    converted.append({
                        "tool_name": fn.get("name", ""),
                        "arguments": args,
                        "call_id": tc.get("id"),
                    })
                else:
                    converted.append(tc)
            data["tool_calls"] = converted
        return cls.model_validate(data)

    @classmethod
    def from_dicts(cls, data_list: list[dict[str, Any]]) -> list[ChatMessage]:
        """批量从 dict 列表构造。"""
        return [cls.from_dict(d) for d in data_list]

    @staticmethod
    def coerce(value: ChatMessage | dict[str, Any]) -> ChatMessage:
        """确保返回 ChatMessage：已是 ChatMessage 时直接返回，dict 时转换。"""
        if isinstance(value, ChatMessage):
            return value
        return ChatMessage.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        """转换为 dict（包含未知字段，排除 None 值）。

        对无法 JSON 序列化的嵌套对象，递归转换为 dict 以确保兼容性。
        reasoning_content 随消息持久化（thinking-mode passback 依赖它在
        compaction / 进程重启后存活）；provider 层仅在 assistant tool-call
        轮次把它条件性回放到请求上，训练导出读取 span 属性、不经过本方法。
        content_format 为 PLAIN 时自动省略，created_at 格式化为本地时间字符串。
        tool_calls 序列化为 OpenAI wire format（id/type/function）以保持存储兼容。
        """
        result = self.model_dump(mode="json", exclude_none=True)
        if "content_format" in result and self.content_format == ContentFormat.PLAIN:
            result.pop("content_format", None)
        if "created_at" in result and self.created_at is not None:
            result["created_at"] = self.created_at.strftime("%Y-%m-%d %H:%M:%S")
        if "tool_calls" in result and result["tool_calls"]:
            dumped_calls = result["tool_calls"]
            result["tool_calls"] = [
                {
                    "id": (tc.get("call_id") or f"call_{i}") if isinstance(tc, dict) else f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc.get("tool_name", "") if isinstance(tc, dict) else "",
                        "arguments": json.dumps(tc.get("arguments")) if isinstance(tc, dict) and tc.get("arguments") else "{}",
                    },
                }
                for i, tc in enumerate(dumped_calls)
            ]
        return result

    def get(self, key: str, default: Any = None) -> Any:
        """兼容 dict 的 get 语义。"""
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        """兼容 dict 的 [] 语法。"""
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None
