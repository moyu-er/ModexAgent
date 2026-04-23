from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
import tempfile
import os

from framework.security import SecurityConfig


def _get_default_temp_dir() -> str:
    """获取系统默认临时目录（跨平台）"""
    return tempfile.gettempdir()


def _get_default_workspace_dir() -> str:
    """获取默认工作目录（跨平台）"""
    return os.path.join(tempfile.gettempdir(), "sandbox_workspace")


@dataclass
class SandboxConfig:
    allowed_dirs: List[str] = field(default_factory=lambda: [_get_default_temp_dir()])
    deny_dirs: List[str] = field(default_factory=lambda: ["/home", "/root", "/etc", "/var"] if os.name != 'nt' else [])
    max_file_size_mb: int = 100
    max_execution_time_seconds: int = 60
    workspace_dir: str = field(default_factory=_get_default_workspace_dir)
    enable_network: bool = False
    memory_limit_mb: Optional[int] = None
    cpu_limit: Optional[float] = None
    enable_validation: bool = True
    artifact_max_size: int = 10485760
    # E2B-specific configuration
    auto_download_artifacts: bool = False
    auto_download_patterns: Optional[List[str]] = None
    # Security configuration for command execution
    security: Optional[SecurityConfig] = None

    def __post_init__(self):
        default_temp = _get_default_temp_dir()
        if not self.allowed_dirs:
            self.allowed_dirs = [default_temp]

        for d in self.allowed_dirs:
            p = Path(d)
            if not p.is_absolute():
                raise ValueError(f"allowed_dirs must be absolute paths: {d}")

        if self.max_execution_time_seconds <= 0:
            raise ValueError(
                f"max_execution_time_seconds must be positive, got {self.max_execution_time_seconds}"
            )

        if self.memory_limit_mb is not None and self.memory_limit_mb <= 0:
            raise ValueError(
                f"memory_limit_mb must be positive when specified, got {self.memory_limit_mb}"
            )

        if self.cpu_limit is not None and self.cpu_limit <= 0:
            raise ValueError(
                f"cpu_limit must be positive when specified, got {self.cpu_limit}"
            )

        if self.artifact_max_size <= 0:
            raise ValueError(
                f"artifact_max_size must be positive, got {self.artifact_max_size}"
            )

        self.workspace_dir = str(Path(self.workspace_dir).absolute())
