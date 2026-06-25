from __future__ import annotations

from pathlib import Path


def parse_user_path(raw: str, base: Path) -> Path:
    """解析用户输入的路径字符串。

    支持：绝对路径、相对路径、. .. ~ 符号、/ 路径分隔符。
    pathlib 原生跨平台处理。

    Args:
        raw: 用户输入的原始路径字符串。
        base: 相对路径的基准目录（当前 WorkspaceContext.current）。

    Returns:
        解析后的绝对路径。

    Raises:
        ValueError: 输入为空或仅含空白。
    """
    stripped = raw.strip()
    if not stripped:
        raise ValueError("Path input is empty")

    # Normalise backslashes to forward slashes so that \\ works as a
    # path separator on all platforms — not just Windows.
    # Guard: skip the string allocation when there are no backslashes.
    normalised = stripped if "\\" not in stripped else stripped.replace("\\", "/")
    target = Path(normalised).expanduser()
    if not target.is_absolute():
        target = base / target
    return target.resolve()
