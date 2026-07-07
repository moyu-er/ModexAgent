"""标准化工具集合

提供跨平台的文件操作和搜索工具，作为 Skill 系统的基础。
参考 nanobot 实现，简洁独立。

Shell execution tools have moved to framework.tools.terminal.subprocess_tool.
"""

from .file_tool import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from .search_tool import FindFilesTool, SearchFilesTool

# DEPRECATED: TodoCompletionProbeHook is kept for backward compatibility and
# framework unit tests only. New code should use system-prompt optimization
# (TodoAwareSystemPromptProvider) and clear todo tool descriptions instead of
# injecting synthetic tool calls into the conversation history.
from .todo_probe import TodoCompletionProbeHook
from .todo_tool import ACTIVE_TODO_STATUSES, TodoReadTool, TodoWriteTool

__all__ = [
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "ListDirTool",
    "SearchFilesTool",
    "FindFilesTool",
    "TodoWriteTool",
    "TodoReadTool",
    "TodoCompletionProbeHook",
    "ACTIVE_TODO_STATUSES",
]
