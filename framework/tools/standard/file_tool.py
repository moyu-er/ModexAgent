"""文件系统工具: read, write, edit, list.

提供简洁独立的文件操作工具。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ...core.tool_manager import Tool

logger = logging.getLogger(__name__)


# -- 路径解析 ---------------------------------------------------------------


def _resolve_path(path: str) -> Path:
    """Resolve path. No permission check — that's the interceptor's job."""
    return Path(path).expanduser().resolve()


# -- 引号归一化与保留 -------------------------------------------------------

_LEFT_SINGLE_CURLY = "\u2018"   # '
_RIGHT_SINGLE_CURLY = "\u2019"  # '
_LEFT_DOUBLE_CURLY = "\u201c"   # "
_RIGHT_DOUBLE_CURLY = "\u201d"  # "


def _normalize_quotes(s: str) -> str:
    """将弯引号归一化为直引号。"""
    return (
        s.replace(_LEFT_SINGLE_CURLY, "'")
        .replace(_RIGHT_SINGLE_CURLY, "'")
        .replace(_LEFT_DOUBLE_CURLY, '"')
        .replace(_RIGHT_DOUBLE_CURLY, '"')
    )


def _preserve_quote_style(old_model: str, old_actual: str, new_model: str) -> str:
    """当 old_string 通过引号归一化匹配时，将 new_string 中的直引号替换为文件中的弯引号风格。

    简化策略：检测 actual_old 中使用的弯引号类型，对 new_string 中
    对应的直引号统一应用 opening/closing 启发式替换。
    """
    if old_model == old_actual:
        return new_model

    has_double = _LEFT_DOUBLE_CURLY in old_actual or _RIGHT_DOUBLE_CURLY in old_actual
    has_single = _LEFT_SINGLE_CURLY in old_actual or _RIGHT_SINGLE_CURLY in old_actual

    if not has_double and not has_single:
        return new_model

    result = new_model
    if has_double:
        result = _apply_curly_double_quotes(result)
    if has_single:
        result = _apply_curly_single_quotes(result)
    return result


def _is_opening_context(chars: list[str], index: int) -> bool:
    """判断引号位置是否为 opening context。"""
    if index == 0:
        return True
    prev = chars[index - 1]
    return prev in (" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013")


def _apply_curly_double_quotes(s: str) -> str:
    """将直双引号替换为弯双引号（opening/closing 启发式）。"""
    chars = list(s)
    out: list[str] = []
    for i, ch in enumerate(chars):
        if ch == '"':
            out.append(_LEFT_DOUBLE_CURLY if _is_opening_context(chars, i) else _RIGHT_DOUBLE_CURLY)
        else:
            out.append(ch)
    return "".join(out)


def _apply_curly_single_quotes(s: str) -> str:
    """将直单引号替换为弯单引号（跳过缩写中的撇号）。"""
    chars = list(s)
    out: list[str] = []
    for i, ch in enumerate(chars):
        if ch == "'":
            prev = chars[i - 1] if i > 0 else None
            nxt = chars[i + 1] if i < len(chars) - 1 else None
            if prev and nxt and prev.isalpha() and nxt.isalpha():
                out.append(_RIGHT_SINGLE_CURLY)
            else:
                out.append(_LEFT_SINGLE_CURLY if _is_opening_context(chars, i) else _RIGHT_SINGLE_CURLY)
        else:
            out.append(ch)
    return "".join(out)


# -- 空白归一化与位置映射 ---------------------------------------------------


def _normalize_whitespace(s: str) -> str:
    """将 tabs 归一化为 4 个空格（处理 Read tool 渲染导致的差异）。"""
    return s.replace("\t", "    ")


def _map_whitespace_back(
    original: str,
    ws_normalized: str,
    ws_start: int,
    ws_len: int,
) -> str:
    """将 whitespace 归一化后的匹配位置映射回原始内容。

    逐字符遍历原始内容，同时追踪归一化位置。tab 在归一化中扩展为 4 个空格。
    如果目标位置落在 tab 扩展的中间，snap 到 tab 的边界。
    """
    orig_i = 0
    norm_i = 0
    start_orig: int | None = None
    end_orig: int | None = None

    while orig_i < len(original) and norm_i <= ws_start + ws_len:
        if norm_i == ws_start:
            start_orig = orig_i
        if norm_i == ws_start + ws_len:
            end_orig = orig_i
            break

        ch = original[orig_i]
        if ch == "\t":
            next_norm = norm_i + 4
            if norm_i < ws_start < next_norm and start_orig is None:
                start_orig = orig_i
            if norm_i < ws_start + ws_len < next_norm and end_orig is None:
                end_orig = orig_i + 1
                break
            norm_i = next_norm
        else:
            norm_i += 1
        orig_i += 1

    if start_orig is None:
        start_orig = 0
    if end_orig is None:
        end_orig = len(original)

    return original[start_orig:end_orig]


# -- 文件 I/O（保留编码和换行符）-------------------------------------------


def _read_file(path: Path) -> tuple[str, str, str]:
    """读取文件，返回 (内容, 编码, 换行符风格)。

    内容中的换行符统一归一化为 \n（便于后续处理），
    但会记录原始换行符风格以便写入时恢复。
    """
    raw = path.read_bytes()
    has_crlf = b"\r\n" in raw

    if raw.startswith(b"\xff\xfe"):
        encoding = "utf-16-le"
        content = raw[2:].decode("utf-16-le").replace("\r\n", "\n")
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
        content = raw.decode("utf-8-sig").replace("\r\n", "\n")
    else:
        encoding = "utf-8"
        try:
            content = raw.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            encoding = "utf-16-le"
            content = raw.decode("utf-16-le").replace("\r\n", "\n")

    line_endings = "CRLF" if has_crlf else "LF"
    return content, encoding, line_endings


def _write_file(path: Path, content: str, encoding: str, line_endings: str) -> None:
    """写入文件，恢复原始编码和换行符风格。"""
    if line_endings == "CRLF":
        content = content.replace("\n", "\r\n")

    if encoding == "utf-16-le":
        raw = b"\xff\xfe" + content.encode("utf-16-le")
    elif encoding == "utf-8-sig":
        raw = b"\xef\xbb\xbf" + content.encode("utf-8")
    else:
        raw = content.encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


# -- 核心查找逻辑 -----------------------------------------------------------


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    """四级级联匹配，返回文件中实际存在的字符串。

    匹配级联：
    1. 精确匹配
    2. 引号归一化（弯引号 → 直引号）
    3. Tab/空格归一化（tabs ↔ 4 spaces）
    4. 引号 + 空白组合归一化
    """
    if not search_string:
        return ""

    # 1. 精确匹配
    if search_string in file_content:
        return search_string

    # 2. 引号归一化
    norm_search = _normalize_quotes(search_string)
    norm_file = _normalize_quotes(file_content)
    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx : idx + len(search_string)]

    # 3. Tab/空格归一化
    ws_search = _normalize_whitespace(search_string)
    ws_file = _normalize_whitespace(file_content)
    idx = ws_file.find(ws_search)
    if idx != -1:
        return _map_whitespace_back(file_content, ws_file, idx, len(ws_search))

    # 4. 组合归一化
    combined_search = _normalize_whitespace(norm_search)
    combined_file = _normalize_whitespace(norm_file)
    idx = combined_file.find(combined_search)
    if idx != -1:
        return _map_whitespace_back(file_content, combined_file, idx, len(combined_search))

    return None


# -- 分页读取核心逻辑 -------------------------------------------------------


# 分页读取内部常量
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 300
_MAX_CHARS = 20_000


def _paginate_file(
    file_path: Path,
    offset: int = 0,
    limit: int = _DEFAULT_LIMIT,
    max_chars: int = _MAX_CHARS,
) -> str:
    """分页读取文件，返回带结构化元数据的结果字符串。

    参数:
        file_path: 已校验的文件路径
        offset: 跳过前 N 行 (0-based)
        limit: 最多读取行数 (自动 clamp 到 _MAX_LIMIT)
        max_chars: 总字符数硬上限

    返回:
        带结构化后缀的文件内容字符串，或错误信息字符串
    """
    # ── 参数校验 ─────────────────────────────────────────────
    if offset < 0:
        return f"Error: offset must be >= 0, got {offset}"
    if limit < 1:
        return f"Error: limit must be >= 1, got {limit}"

    # clamp limit
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT

    # ── 第一遍：统计总行数 ──────────────────────────────────
    total_lines = 0
    with file_path.open("r", encoding="utf-8") as f:
        for line in f:
            total_lines += 1

    # ── 空文件 ──────────────────────────────────────────────
    if total_lines == 0:
        return (
            "(empty file)\n"
            "\n"
            "total_lines: 0\n"
            "offset: 0\n"
            "read_status: empty"
        )

    # ── offset 超出范围 ──────────────────────────────────────
    if offset >= total_lines:
        return (
            f"Error: offset ({offset}) exceeds file length ({total_lines} lines).\n"
            f"File has {total_lines} lines (line numbers 1-{total_lines}).\n"
            f"Valid offset range: 0 ~ {total_lines - 1}.\n"
            f"hint: use offset=0 to read from the beginning"
        )

    # ── 第二遍：分页读取 ─────────────────────────────────────
    selected_lines: list[str] = []
    accumulated_chars = 0
    char_truncated = False
    last_line_read = offset  # 0-based, 最后成功读取的行索引

    with file_path.open("r", encoding="utf-8") as f:
        line_idx = 0  # 0-based
        lines_collected = 0

        for raw_line in f:
            # 跳过 offset 之前的行
            if line_idx < offset:
                line_idx += 1
                continue

            # limit 用尽 → 还有更多行
            if lines_collected >= limit:
                break

            line = raw_line.rstrip("\n\r")

            # 字符数软上限：先带上该行，再判断是否截断
            selected_lines.append(line)
            accumulated_chars += len(line) + 1  # +1 for newline
            last_line_read = line_idx
            lines_collected += 1
            line_idx += 1

            if accumulated_chars > max_chars:
                char_truncated = True
                break

        # 检查读完之后是否还有更多行（仅当 limit 未触发且 char 也未触发时）
        has_more_by_limit = lines_collected >= limit
        # 如果没有被 char 截断，检查文件是否已读完
        remaining = total_lines - (last_line_read + 1) if not char_truncated else total_lines - (last_line_read + 1)

    # ── 计算状态 ─────────────────────────────────────────────
    actual_start = offset + 1       # 1-based 显示
    actual_end = last_line_read + 1  # 1-based 显示

    is_complete = (not char_truncated) and (actual_end == total_lines)
    is_truncated_by_limit = has_more_by_limit and not char_truncated

    # ── 构建结果 ─────────────────────────────────────────────
    content = "\n".join(selected_lines)
    parts: list[str] = [content, ""]

    # metadata
    parts.append(f"total_lines: {total_lines}")
    parts.append(f"offset: {offset}")

    if is_complete and not is_truncated_by_limit:
        # 完整读取
        if lines_collected < limit:
            parts.append(f"read_lines: {actual_start}-{actual_end} (requested {limit}, file has {lines_collected} remaining)")
        else:
            parts.append(f"read_lines: {actual_start}-{actual_end}")
        parts.append("read_status: complete")

    elif is_truncated_by_limit:
        # 行数截断
        parts.append(f"read_lines: {actual_start}-{actual_end} (limit reached)")
        parts.append(f"remaining_lines: {remaining}")
        parts.append("read_status: truncated_by_limit")
        parts.append(f"hint: use offset={last_line_read + 1} to read next chunk")

    elif char_truncated:
        # 字符数截断（limit 未用尽就提前返回）
        parts.append(f"read_lines: {actual_start}-{actual_end} (stopped before limit)")
        parts.append(f"remaining_lines: {remaining}")
        parts.append("read_status: truncated_by_chars")
        parts.append(
            f"warning: char limit ({max_chars}) reached, "
            f"only read {lines_collected} of requested {limit} lines"
        )
        parts.append(f"hint: use offset={last_line_read + 1} to read next chunk")

    return "\n".join(parts)


# -- 工具类 -----------------------------------------------------------------


class ReadFileTool(Tool):
    """读取文件内容的工具."""

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file at the given path. "
            f"By default reads up to {_DEFAULT_LIMIT} lines from the beginning. "
            "Use offset to skip lines and limit to control how many lines to read."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read"
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of lines to skip from the beginning (0-based, default: 0)",
                    "default": 0
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of lines to read (default: {_DEFAULT_LIMIT}, max: {_MAX_LIMIT})",
                    "default": _DEFAULT_LIMIT
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, offset: int = 0, limit: int = _DEFAULT_LIMIT, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            return _paginate_file(file_path, offset=offset, limit=limit)
        except Exception as e:
            return f"Error reading file: {str(e)}"


class WriteFileTool(Tool):
    """写入内容到文件的工具."""

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

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
            file_path = _resolve_path(path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """通过精确字符串替换编辑文件的工具。

    核心能力：
    - 四级模糊匹配：精确、引号归一化、tab/空格归一化、组合归一化
    - replace_all：批量替换所有出现
    - 空 old_string：文件不存在时创建新文件，文件存在且非空时报错
    - 保留原始编码和换行符风格
    """

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "edit"

    @property
    def description(self) -> str:
        return (
            "Perform exact string replacements in files. "
            "The edit will FAIL if old_string is not found in the file. "
            "Either provide a larger string with more surrounding context to make it unique, "
            "or use replace_all=true to change every occurrence. "
            "When editing text from read_file output, preserve the exact indentation "
            "(tabs/spaces) as it appears in the file content — never include line-number prefixes. "
            "To delete text, set new_string to an empty string. "
            "To create a new file, set old_string to an empty string on a nonexistent file."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit"
                },
                "old_string": {
                    "type": "string",
                    "description": (
                        "The text to find and replace. Must match exactly in the file "
                        "(supports quote and whitespace normalization). "
                        "Empty string means create new file or write to an empty existing file."
                    )
                },
                "new_string": {
                    "type": "string",
                    "description": "The text to replace with. Empty string means delete old_string."
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace all occurrences of old_string (default false). "
                        "Useful for renaming variables across the file."
                    ),
                    "default": False
                }
            },
            "required": ["path", "old_string", "new_string"]
        }

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            file_path = _resolve_path(path)

            # 无变化检查
            if old_string == new_string:
                return "Error: No changes to make — old_string and new_string are identical."

            # 读取或初始化文件
            if file_path.exists():
                content, encoding, line_endings = _read_file(file_path)
                file_exists = True
            else:
                content, encoding, line_endings = "", "utf-8", "LF"
                file_exists = False

            # 空 old_string 语义
            if old_string == "":
                if not file_exists:
                    _write_file(file_path, new_string, encoding, line_endings)
                    return f"Successfully created {path} with {len(new_string)} bytes."
                if content.strip() == "":
                    _write_file(file_path, new_string, encoding, line_endings)
                    return f"Successfully wrote {len(new_string)} bytes to {path}."
                return (
                    "Error: Cannot create new file — file already exists and is not empty. "
                    "Use write_file to overwrite, or provide old_string to edit."
                )

            # 非空 old_string 且文件不存在
            if not file_exists:
                return f"Error: File not found: {path}"

            # 查找实际匹配字符串（四级模糊匹配）
            actual_old = _find_actual_string(content, old_string)

            # 最后 fallback：尝试去掉 trailing whitespace 再匹配
            if actual_old is None:
                stripped = old_string.rstrip()
                if stripped != old_string:
                    actual_old = _find_actual_string(content, stripped)

            if actual_old is None:
                preview = old_string[:200]
                if len(old_string) > 200:
                    preview += "..."
                return f"Error: String to replace not found in file.\nString: {preview}"

            # 检查匹配次数
            matches = content.count(actual_old)
            if matches > 1 and not replace_all:
                preview = old_string[:200]
                if len(old_string) > 200:
                    preview += "..."
                return (
                    f"Error: Found {matches} matches of the string to replace, "
                    f"but replace_all is false. To replace all occurrences, set replace_all=true. "
                    f"To replace only one occurrence, provide more context to uniquely identify the instance.\n"
                    f"String: {preview}"
                )

            # 引号风格保留
            actual_new = _preserve_quote_style(old_string, actual_old, new_string)

            # 应用替换
            updated = content.replace(actual_old, actual_new) if replace_all else content.replace(actual_old, actual_new, 1)

            # 验证替换确实发生了
            if updated == content:
                return "Error: Edit produced no changes."

            # 写入文件
            _write_file(file_path, updated, encoding, line_endings)

            if replace_all:
                return f"Successfully edited {path}. All {matches} occurrences replaced."
            return f"Successfully edited {path}."

        except Exception as e:
            logger.exception("EditFileTool error")
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """列出目录内容的工具."""

    def __init__(self):
        super().__init__()

    @property
    def name(self) -> str:
        return "ls"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

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
            dir_path = _resolve_path(path)
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
        except Exception as e:
            return f"Error listing directory: {str(e)}"

