"""聊天消息结构体定义。

ChatMessage 封装了消息的核心字段（role、content、tool_calls 等），
同时通过 pydantic extra='allow' 保留未知字段，确保向前兼容。

使用方式：
    # 从 dict 构造
    msg = ChatMessage.from_dict({"role": "user", "content": "hello"})
    # 转换为 dict（如保存到 storage）
    d = msg.to_dict()
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContentFormat(StrEnum):
    """内容格式枚举。

    PLAIN  — 纯文本内容，可安全截断
    XML    — XML 结构化内容，支持按路径定位截断
    """

    PLAIN = "plain"
    XML = "xml"


class ChatMessage(BaseModel):
    """聊天消息模型。

    固定字段：role、content、tool_calls、tool_call_id、name
    未知字段：通过 model_config extra='allow' 保留
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: str = Field(..., description="消息角色: user / assistant / system / tool")
    content: str | list[dict[str, Any]] | None = Field(
        default=None, description="消息内容，支持 str 或 list[dict]（多模态）"
    )
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, description="assistant 请求的工具调用列表"
    )
    tool_call_id: str | None = Field(
        default=None, description="tool 消息对应的 tool_call_id"
    )
    name: str | None = Field(
        default=None, description="工具名称（OpenAI function calling）"
    )
    created_at: datetime | None = Field(
        default=None, description="消息创建时间戳"
    )
    content_format: ContentFormat = Field(
        default=ContentFormat.PLAIN, description="内容格式：plain 或 xml"
    )
    truncatable_paths: list[str] | None = Field(
        default=None, description="XML 内容中可截断的路径列表"
    )

    @field_validator("created_at", mode="before")
    @classmethod
    def _parse_created_at(cls, v: Any) -> datetime | None:
        """Parse created_at from string ("YYYY-MM-DD HH:MM:SS") or datetime."""
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            # Try "YYYY-MM-DD HH:MM:SS" format
            try:
                return datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
            # Try ISO format
            return datetime.fromisoformat(v)
        return v

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessage:
        """从 dict 构造 ChatMessage，保留未知字段。

        对 tool_calls 中的非 dict 对象（如 pydantic model、dataclass）
        自动调用 model_dump / asdict / __dict__ 转换为 dict，确保兼容性。
        """
        data = dict(data)

        # 预处理 tool_calls：将非 dict 对象转换为 dict
        if "tool_calls" in data and data["tool_calls"] is not None:
            tool_calls = data["tool_calls"]
            converted: list[dict[str, Any]] = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    converted.append(tc)
                elif hasattr(tc, "model_dump"):
                    converted.append(tc.model_dump())
                elif hasattr(tc, "__dict__"):
                    converted.append(tc.__dict__)
                else:
                    converted.append(dict(tc))
            data = {**data, "tool_calls": converted}
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
        """
        try:
            result = self.model_dump(mode="json", exclude_none=True)
        except Exception:
            result = self._to_dict_fallback()
        result.pop("reasoning_content", None)
        # Omit content_format when it is the default (PLAIN)
        if "content_format" in result and self.content_format == ContentFormat.PLAIN:
            result.pop("content_format", None)
        # Format created_at to "YYYY-MM-DD HH:MM:SS"
        if "created_at" in result and self.created_at is not None:
            result["created_at"] = self.created_at.strftime("%Y-%m-%d %H:%M:%S")
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
        if isinstance(value, str | int | float | bool):
            return value
        if isinstance(value, list):
            return [ChatMessage._serialize_value(v) for v in value]
        if isinstance(value, dict):
            return {k: ChatMessage._serialize_value(v) for k, v in value.items()}
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if hasattr(value, "__dict__"):
            return ChatMessage._serialize_value(value.__dict__)
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
