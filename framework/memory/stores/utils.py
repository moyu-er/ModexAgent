"""Storage utilities for cross-platform filesystem safety."""

import hashlib
import re
from pathlib import Path

MAX_FILENAME_LENGTH = 100


def sanitize_scope_key(scope_key: str) -> str:
    """将 scope_key 转换为安全的文件系统名称。

    策略：
    1. 替换非法字符为下划线
    2. 如果长度过长（超过 MAX_FILENAME_LENGTH），使用 MD5 哈希
    3. 如果 key 是纯数字或过于简单，也做一定保护

    Windows 非法字符: < > : " / \\ | ? *
    """
    if not scope_key:
        return "_empty_"

    # 替换所有非法字符（含冒号、斜杠）
    safe = re.sub(r'[<>:"/\\|?*]', "_", scope_key)

    # 如果替换后长度仍然可接受，直接返回
    if len(safe) <= MAX_FILENAME_LENGTH:
        return safe or "_empty_"

    # 过长：截断后追加 MD5 尾缀
    digest = hashlib.md5(scope_key.encode("utf-8")).hexdigest()[:16]
    truncated = safe[: MAX_FILENAME_LENGTH - len(digest) - 1]
    return f"{truncated}_{digest}"


def ensure_scope_dir(workspace: Path, scope_key: str) -> Path:
    """获取并创建 scope_key 对应的存储目录。"""
    safe_key = sanitize_scope_key(scope_key)
    scope_dir = workspace / safe_key
    scope_dir.mkdir(parents=True, exist_ok=True)
    return scope_dir
