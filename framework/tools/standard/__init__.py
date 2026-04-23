"""标准化工具集合

提供跨平台的文件操作和 Shell 执行工具，作为 Skill 系统的基础。
参考 nanobot 实现，简洁独立。

Example:
    from framework.tools.standard import (
        ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, ShellTool
    )
    from framework.tools.registry import ToolRegistry

    # 创建注册表
    registry = ToolRegistry()

    # 注册文件工具
    registry.register(ReadFileTool(allowed_dir=Path("./workspace")))
    registry.register(WriteFileTool(allowed_dir=Path("./workspace")))
    registry.register(EditFileTool(allowed_dir=Path("./workspace")))
    registry.register(ListDirTool(allowed_dir=Path("./workspace")))

    # 注册 Shell 工具
    registry.register(ShellTool(
        timeout=60,
        working_dir="./workspace",
        restrict_to_workspace=True
    ))
"""

from .file_tool import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    ListDirTool,
    FileTool,  # 兼容性别名
)
from .shell_tool import ShellTool

__all__ = [
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "ListDirTool",
    "FileTool",
    "ShellTool",
]
