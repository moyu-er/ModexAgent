"""Standard tool set — file I/O, content search, and file discovery.

Provides cross-platform file operations (read/write/edit/ls), content search
(grep), and file pattern matching (glob). These are the core tools available
to all agents by default.
"""

from .file_tool import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from .glob_tool import GlobTool
from .search_tool import SearchFilesTool

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
    "GlobTool",
    "TodoWriteTool",
    "TodoReadTool",
    "TodoCompletionProbeHook",
    "ACTIVE_TODO_STATUSES",
]
