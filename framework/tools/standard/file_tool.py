"""文件系统工具: read, write, edit, list.

提供简洁独立的文件操作工具。
"""

from pathlib import Path
from typing import Any

from ...core.tool_manager import Tool


def _resolve_path(path: str, allowed_dirs: list[Path] | None = None) -> Path:
    """Resolve path and optionally enforce directory containment."""
    resolved = Path(path).expanduser().resolve()
    if allowed_dirs:
        for allowed_dir in allowed_dirs:
            allowed_resolved = allowed_dir.resolve()
            try:
                resolved.relative_to(allowed_resolved)
                return resolved
            except ValueError:
                continue
        dir_label = "directory" if len(allowed_dirs) == 1 else "directories"
        raise PermissionError(f"Path {path} is outside allowed {dir_label}: {allowed_dirs}")
    return resolved


def _build_allowed_dirs(allowed_dir: Path | None, extra_allowed_dirs: list[Path] | None) -> list[Path] | None:
    """构建允许的目录列表."""
    dirs: list[Path] = []
    if allowed_dir:
        dirs.append(allowed_dir)
    if extra_allowed_dirs:
        dirs.extend(extra_allowed_dirs)
    return dirs if dirs else None


class ReadFileTool(Tool):
    """读取文件内容的工具."""

    DEFAULT_MAX_LINES = 500

    def __init__(self, allowed_dir: Path | None = None, extra_allowed_dirs: list[Path] | None = None):
        super().__init__()
        self._allowed_dirs = _build_allowed_dirs(allowed_dir, extra_allowed_dirs)

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        desc = f"Read the contents of a file at the given path. By default reads first {self.DEFAULT_MAX_LINES} lines."
        if self._allowed_dirs:
            desc += f" Files are restricted to: {self._allowed_dirs}"
        return desc

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read"
                },
                "start_line": {
                    "type": "integer",
                    "description": f"Starting line number include (start from 1, default: 1)",
                    "default": 1
                },
                "end_line": {
                    "type": "integer",
                    "description": f"Ending line number include (default: {self.DEFAULT_MAX_LINES})",
                    "default": self.DEFAULT_MAX_LINES
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, start_line: int = 1, end_line: int = DEFAULT_MAX_LINES, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._allowed_dirs)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            # 校验行号
            if start_line < 1:
                return f"Error: start_line must be >= 1, got {start_line}"
            if end_line < start_line:
                return f"Error: end_line ({end_line}) must be >= start_line ({start_line})"

            # 限制读取行数
            max_end = start_line + self.DEFAULT_MAX_LINES - 1
            actual_end = min(end_line, max_end)

            # 单次遍历：读取指定范围并检测是否还有更多内容
            selected_lines = []
            has_more = False

            with file_path.open("r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, start=1):
                    if line_num < start_line:
                        continue
                    if line_num > actual_end:
                        has_more = True
                        break
                    selected_lines.append(line.rstrip('\n\r'))

            # 如果起始行就超出范围
            if not selected_lines and start_line > 1:
                return f"[Start line {start_line} is beyond file end]"

            result = "\n".join(selected_lines)

            # 如果有更多内容，简单提示
            if has_more:
                result += "\n\n[... more lines below ...]"
            else:
                result += "\n\n[No more lines below]"

            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


class WriteFileTool(Tool):
    """写入内容到文件的工具."""

    def __init__(self, allowed_dir: Path | None = None, extra_allowed_dirs: list[Path] | None = None):
        super().__init__()
        self._allowed_dirs = _build_allowed_dirs(allowed_dir, extra_allowed_dirs)

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        desc = "Write content to a file at the given path. Creates parent directories if needed."
        if self._allowed_dirs:
            desc += f" Files are restricted to: {self._allowed_dirs}"
        return desc

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write"
                }
            },
            "required": ["path", "content"]
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._allowed_dirs)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """通过替换文本编辑文件的工具."""

    def __init__(self, allowed_dir: Path | None = None, extra_allowed_dirs: list[Path] | None = None):
        super().__init__()
        self._allowed_dirs = _build_allowed_dirs(allowed_dir, extra_allowed_dirs)

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        desc = "Edit a file by replacing old_text with new_text. The old_text must exist exactly in the file."
        if self._allowed_dirs:
            desc += f" Files are restricted to: {self._allowed_dirs}"
        return desc

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace"
                },
                "new_text": {
                    "type": "string",
                    "description": "The text to replace with"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._allowed_dirs)
            if not file_path.exists():
                return f"Error: File not found: {path}"

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return f"Error: old_text not found in file. Make sure it matches exactly."

            # 统计出现次数
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")

            return f"Successfully edited {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """列出目录内容的工具."""

    def __init__(self, allowed_dir: Path | None = None, extra_allowed_dirs: list[Path] | None = None):
        super().__init__()
        self._allowed_dirs = _build_allowed_dirs(allowed_dir, extra_allowed_dirs)

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        desc = "List the contents of a directory."
        if self._allowed_dirs:
            desc += f" Directories are restricted to: {self._allowed_dirs}"
        return desc

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            dir_path = _resolve_path(path, self._allowed_dirs)
            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"


# 为兼容性保留 FileTool 别名
FileTool = ReadFileTool
