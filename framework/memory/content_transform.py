"""ContentTransformer — 多媒体内容转换管线。

框架提供 ABC + 内置 Base64SanitizeTransformer + CompositeTransformer。
所有其他 transformer（vision 描述、多模态 embedding 等）由业务模块或插件提供。

使用方式：
    from framework.memory.content_transform import Base64SanitizeTransformer

    main_layers = MemorySystem.default_single_user_layers(...)
    main_layers["short_term"].content_transformer = Base64SanitizeTransformer()
"""

from __future__ import annotations

import copy
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import mimetypes
from pathlib import Path
from typing import Any

from framework.memory.core.message import ChatMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants & Enums
# ---------------------------------------------------------------------------

class BlockType(StrEnum):
    """多模态消息 block 类型（OpenAI 兼容格式）。"""

    IMAGE_URL = "image_url"
    INPUT_AUDIO = "input_audio"
    FILE = "file"
    TEXT = "text"


class UrlPrefix:
    """URL 方案前缀。"""

    DATA = "data:"
    HTTP = "http://"
    HTTPS = "https://"


class _K:
    """消息 dict 的 key 名（模块内部使用，避免硬编码字符串）。"""

    CONTENT = "content"
    METADATA = "metadata"
    MEDIA_INFO = "media_info"
    META = "_meta"
    MIME = "mime"
    PATH = "path"
    PLACEHOLDER = "placeholder"
    TEXT = "text"
    TYPE = "type"
    URL = "url"


UNKNOWN_NAME = "(unknown)"
UNKNOWN_MIME = "application/unknown"
UNKNOWN_PLACEHOLDER = "[media: (unknown)]"


# ---------------------------------------------------------------------------
# Structured types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MediaInfo:
    """媒体文件元信息（sanitize 后写入 message.metadata.media_info）。"""

    media_type: str
    path: str
    mime: str
    placeholder: str

    def to_dict(self) -> dict[str, Any]:
        return {
            _K.TYPE: self.media_type,
            _K.PATH: self.path,
            _K.MIME: self.mime,  # type: ignore[name-defined]
            _K.PLACEHOLDER: self.placeholder,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_block_url(block: dict[str, Any], block_type: BlockType) -> str:
    """从 block 中提取 URL。"""
    return block.get(str(block_type), {}).get(_K.URL, "")


def _get_block_data(block: dict[str, Any], block_type: BlockType) -> tuple[str, str]:
    """从 block 中提取 data payload 和 MIME 类型。

    兼容两种格式：
    1. 框架内部 URL 格式：{"input_audio": {"url": "data:..."}}
    2. OpenAI API 格式：{"input_audio": {"data": "base64", "format": "wav"}}

    Returns:
        (data_payload, mime_type)
        - data_payload: 可传给 _extract_mime_from_data_url 的字符串
        - mime_type: 检测到的 MIME 类型（供回退使用）
    """
    inner = block.get(str(block_type), {})

    # 优先检查 URL 格式（框架内部使用）
    url = inner.get(_K.URL, "")
    if url:
        return url, ""

    # OpenAI API 格式
    if block_type == BlockType.INPUT_AUDIO:
        fmt = inner.get("format", "wav")
        data = inner.get("data", "")
        if data:
            return f"data:audio/{fmt};base64,{data}", f"audio/{fmt}"

    if block_type == BlockType.FILE:
        data = inner.get("file_data", "")
        filename = inner.get("filename", "")
        if data:
            # 尝试从 filename 推断 MIME
            mime, _ = mimetypes.guess_type(filename) if filename else (None, None)
            mime = mime or "application/octet-stream"
            return f"data:{mime};base64,{data}", mime

    return "", ""


def _get_meta_path(block: dict[str, Any]) -> str:
    """从 block._meta 中提取 path。"""
    return (block.get(_K.META) or {}).get(_K.PATH, "")


def _extract_mime_from_data_url(url: str) -> str:
    """从 data: URI 中提取 MIME 类型。

    data:image/png;base64,xxx → image/png
    """
    if url.startswith(UrlPrefix.DATA):
        return url.split(";")[0].split(":")[1]
    return UNKNOWN_MIME


# ---------------------------------------------------------------------------
# ContentTransformer ABC
# ---------------------------------------------------------------------------

class ContentTransformer(ABC):
    """多媒体内容转换器基类。

    框架层接口。业务模块可继承实现自定义转换逻辑。
    ShortTermMemoryManager 在写入存储前调用此接口。
    """

    @abstractmethod
    async def transform_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """转换单条消息。返回深拷贝后的修改版本（如需要）。

        Args:
            message: 原始消息 dict，包含 role、content 等字段。

        Returns:
            转换后的消息 dict。不应修改原始 message。
        """
        ...

    async def transform_messages(
        self, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """批量转换。默认逐个调用，子类可优化为并发处理。

        入口接受 ChatMessage 或 dict，内部统一转为 dict 处理，
        返回值仍为 dict 列表（供 storage 层直接存储）。
        """
        dict_messages: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, ChatMessage):
                dict_messages.append(m.to_dict())
            else:
                dict_messages.append(m)
        return [await self.transform_message(m) for m in dict_messages]


# ---------------------------------------------------------------------------
# Base64SanitizeTransformer
# ---------------------------------------------------------------------------

_BASE64_DATA_PATTERN = re.compile(r"data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+;base64,")


class Base64SanitizeTransformer(ContentTransformer):
    """框架内置：将 base64 image_url / input_audio / file blocks 替换为文本占位符。

    这是唯一直接由框架提供的 transformer 实现。
    所有其他 transformer（vision 描述、多模态 embedding 等）
    由业务模块或插件提供。

    占位符默认使用文件名（不含目录），避免路径泄露敏感信息。
    media_info 中保留完整路径用于后续处理。

    支持的 block 类型：
    - image_url with data: URI → [media: filename]
    - image_url with http(s) URL → 保留原样
    - input_audio with data: URI → [media: filename]
    - file with data: URI → [media: filename]

    已 sanitize 过的消息（content 为字符串）会被跳过，避免二次处理。
    """

    PLACEHOLDER_TEMPLATE = "[media: {name}]"

    def __init__(self, placeholder_template: str | None = None) -> None:
        self._placeholder_template = placeholder_template or self.PLACEHOLDER_TEMPLATE

    async def transform_message(self, message: dict[str, Any]) -> dict[str, Any]:
        content = message.get(_K.CONTENT)

        # 纯文本消息：检查是否包含内嵌的 base64 data URI（如工具返回截图内容）
        if isinstance(content, str):
            if _BASE64_DATA_PATTERN.search(content):
                msg = dict(message)
                msg[_K.CONTENT] = _BASE64_DATA_PATTERN.sub("[media: base64_data]", content)
                return msg
            return dict(message)
        if not isinstance(content, list):
            return dict(message)
        if not content:  # 空列表直接返回，避免语义变化（[] → "")
            return dict(message)

        msg = copy.deepcopy(message)  # 仅在需要处理时才深拷贝
        sanitized, media_info = self._sanitize_blocks(content)
        msg[_K.CONTENT] = sanitized
        if media_info:
            metadata = dict(msg.get(_K.METADATA) or {})
            metadata[_K.MEDIA_INFO] = [m.to_dict() for m in media_info]
            msg[_K.METADATA] = metadata
        return msg

    def _sanitize_blocks(
        self, blocks: list[dict[str, Any]]
    ) -> tuple[str | list[dict[str, Any]], list[MediaInfo]]:
        """返回 (sanitized_content, media_info_list)。

        处理以下 block 类型：
        - image_url with data: URI → 替换为占位符
        - image_url with http(s) URL → 保留原样（不占用磁盘/token）
        - input_audio with data: URI → 替换为占位符
        - file with data: URI → 替换为占位符
        """
        sanitized: list[dict[str, Any]] = []
        media_info: list[MediaInfo] = []
        for block in blocks:
            block_type = block.get(_K.TYPE, "")
            if block_type == BlockType.IMAGE_URL:
                url = _get_block_url(block, BlockType.IMAGE_URL)
                if url.startswith(UrlPrefix.DATA):
                    sanitized.append(self._make_text_placeholder(block))
                    media_info.append(self._make_media_info(block, url, "image"))
                else:
                    # http(s) URL 不 sanitize，直接保留
                    sanitized.append(block)
            elif block_type in (BlockType.INPUT_AUDIO, BlockType.FILE):
                # 统一处理 audio/file 类型的 data payload
                # 兼容框架内部 URL 格式和 OpenAI API 格式
                data_payload, _ = _get_block_data(block, BlockType(block_type))
                if data_payload.startswith(UrlPrefix.DATA):
                    sanitized.append(self._make_text_placeholder(block))
                    media_info.append(
                        self._make_media_info(block, data_payload, str(block_type))
                    )
                else:
                    sanitized.append(block)
            else:
                sanitized.append(block)

        # 如果全是 text blocks，合并为字符串
        if all(b.get(_K.TYPE) == BlockType.TEXT for b in sanitized):
            merged_text = "\n".join(b.get(_K.TEXT, "") for b in sanitized)
            return merged_text, media_info
        return sanitized, media_info

    def _make_text_placeholder(self, block: dict[str, Any]) -> dict[str, str]:
        path = _get_meta_path(block)
        if path:
            name = Path(path).name
            placeholder = self._placeholder_template.format(name=name)
        else:
            placeholder = UNKNOWN_PLACEHOLDER
        return {_K.TYPE: BlockType.TEXT, _K.TEXT: placeholder}

    def _make_media_info(
        self, block: dict[str, Any], url: str, media_type: str
    ) -> MediaInfo:
        path = _get_meta_path(block)
        mime = _extract_mime_from_data_url(url)
        name = Path(path).name if path else UNKNOWN_NAME
        placeholder = (
            self._placeholder_template.format(name=name)
            if path
            else UNKNOWN_PLACEHOLDER
        )
        return MediaInfo(
            media_type=media_type,
            path=path,
            mime=mime,
            placeholder=placeholder,
        )


# ---------------------------------------------------------------------------
# CompositeTransformer
# ---------------------------------------------------------------------------

class CompositeTransformer(ContentTransformer):
    """按顺序组合多个 transformer。

    框架提供的工具类，便于业务模块组合多个转换步骤。

    注意：transformer 的顺序很重要。
    推荐顺序：sanitize 在前，vision 描述/其他处理在后。
    错误顺序（如先 vision 后 sanitize）会导致后续 transformer 无法消费 media_info。

    正确示例：
        CompositeTransformer([
            Base64SanitizeTransformer(),      # 第一步：生成 media_info
            VisionDescriptionTransformer(),   # 第二步：消费 media_info
        ])
    """

    def __init__(self, transformers: list[ContentTransformer]):
        self._transformers = transformers

    async def transform_message(self, message: dict[str, Any]) -> dict[str, Any]:
        for t in self._transformers:
            message = await t.transform_message(message)
        return message

    async def transform_messages(
        self, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """批量转换：顺序应用所有 transformer 到每条消息。

        默认实现逐个消息串行处理。如果子 transformer 支持并发批量处理，
        可考虑在子类中重写为并行方案。

        入口接受 ChatMessage 或 dict，内部统一转为 dict 处理，
        返回值仍为 dict 列表（供 storage 层直接存储）。
        """
        dict_messages: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, ChatMessage):
                dict_messages.append(m.to_dict())
            else:
                dict_messages.append(m)
        result: list[dict[str, Any]] = []
        for msg in dict_messages:
            transformed = msg
            for t in self._transformers:
                transformed = await t.transform_message(transformed)
            result.append(transformed)
        return result
