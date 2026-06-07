from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class CdError(StrEnum):
    """cd/exit 失败原因。"""

    INVALID_PATH = "invalid_path"
    ALREADY_HOME = "already_home"
    PATH_NOT_FOUND = "path_not_found"
    NOT_A_DIRECTORY = "not_a_directory"
    PERMISSION_DENIED = "permission_denied"
    AGENTS_BUSY = "agents_busy"
    CALLBACK_ERROR = "callback_error"


@dataclass(frozen=True)
class CdResult:
    """cd/exit 操作结果。

    Attributes:
        success: 是否切换成功。
        current_path: 切换后的路径（成功）或原路径（失败）。
        original_path: 原始路径（启动时记录，不可变）。
        notice: 用户反馈消息（发送到 IM/CLI）。
        error: 失败原因标识，成功时为 None。
    """

    success: bool
    current_path: Path
    original_path: Path
    notice: str
    error: CdError | None = None


class WorkspaceSwitchCallback(Protocol):
    """工作空间切换回调协议。

    实现此 Protocol 的类可在工作空间切换时接收通知，
    以执行子系统的重建或清理工作。
    """

    async def on_workspace_switch(
        self,
        old_data_dir: Path,
        new_data_dir: Path,
    ) -> None:
        """路径切换通知。

        Args:
            old_data_dir: 切换前的数据目录。
            new_data_dir: 切换后的数据目录。
        """
        ...
