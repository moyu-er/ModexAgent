"""Core message types — ChatMessage and ContentFormat.

Foundational message primitives used across the framework, not memory-specific.
Moved from framework.memory.core.message to break the core <-> memory cycle.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modex_agent.core.types import MessageRole, ToolCall
from modex_agent.utils.timezone import get_user_timezone


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


ContentPart = Annotated[TextPart | ImageUrlPart, Field(discriminator="type")]


class ChatMessage(BaseModel):
    """聊天消息模型。

    固定字段：role、content、tool_calls、tool_call_id、name
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
        if isinstance(v, (int, float)):
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
        自动排除 reasoning_content 字段，防止思维链内容泄漏到存储层。
        content_format 为 PLAIN 时自动省略，created_at 格式化为本地时间字符串。
        tool_calls 序列化为 OpenAI wire format（id/type/function）以保持存储兼容。
        """
        try:
            result = self.model_dump(mode="json", exclude_none=True)
        except Exception:
            result = self._to_dict_fallback()
        result.pop("reasoning_content", None)
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

    def _to_dict_fallback(self) -> dict[str, Any]:
        """手动序列化，递归处理嵌套对象。"""
        result: dict[str, Any] = {}
        # 使用 __dict__ 避免再次触发 model_dump 序列化错误
        raw = dict(self.__dict__)
        # Pydantic v2 数据存储在 __pydantic_private__ / __pydantic_extra__ 中
        if hasattr(self, "__pydantic_extra__") and self.__pydantic_extra__:
            raw.update(self.__pydantic_extra__)
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            if value is None:
                continue
            result[key] = self._serialize_value(value)
        return result

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        """递归序列化单个值。"""
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [ChatMessage._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: ChatMessage._serialize_value(v) for k, v in value.items()}
        if isinstance(value, BaseModel):
            return value.model_dump()
        return str(value)

    def get(self, key: str, default: Any = None) -> Any:
        """兼容 dict 的 get 语义。"""
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        """兼容 dict 的 [] 语法。"""
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None
