"""Long-term memory update types."""

from dataclasses import dataclass
from enum import StrEnum


class MemoryUpdateMode(StrEnum):
    """长期记忆更新模式。"""

    INCREMENTAL = "incremental"
    APPEND = "append"
    SECTION_REPLACE = "section_replace"
    REPLACE_TEXT = "replace_text"
    REMOVE = "remove"


@dataclass
class MemoryUpdate:
    """长期记忆更新指令。"""

    file_name: str
    content: str
    mode: str = str(MemoryUpdateMode.INCREMENTAL)
    reason: str = ""
    search_text: str = ""  # for replace_text mode: text to find
