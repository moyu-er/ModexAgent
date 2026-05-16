"""通用工具模块"""

from framework.utils.helpers import (
    ThinkExtractionResult,
    ThinkFormat,
    BUILTIN_THINK_FORMATS,
    extract_think_prefix,
    strip_think,
)
from framework.utils.think_tag import ThinkTagExtractor
from framework.utils.message_builder import (
    build_assistant_message,
    build_tool_message,
)

__all__ = [
    "ThinkExtractionResult",
    "ThinkFormat",
    "BUILTIN_THINK_FORMATS",
    "extract_think_prefix",
    "strip_think",
    "ThinkTagExtractor",
    "build_assistant_message",
    "build_tool_message",
]
