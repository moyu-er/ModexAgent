from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .env_builder import EnvPolicy
from .guard import CommandPatternGuardConfig
from .workspace_policy import WorkspacePolicyConfig


def _get_default_temp_dir() -> str:
    """Return the platform's default temporary directory."""
    return tempfile.gettempdir()


def _get_default_workspace_dir() -> str:
    """Return the default workspace path under the temporary directory."""
    return os.path.join(tempfile.gettempdir(), "sandbox_workspace")


class SandboxConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    allowed_dirs: list[str] = Field(default_factory=lambda: [_get_default_temp_dir()])
    max_execution_time_seconds: int = 60
    workspace_dir: str = Field(default_factory=_get_default_workspace_dir)
    enable_network: bool = False
    memory_limit_mb: int | None = None
    cpu_limit: float | None = None
    enable_validation: bool = True
    artifact_max_size: int = 10485760
    # E2B-specific configuration
    auto_download_artifacts: bool = False
    auto_download_patterns: list[str] | None = None
    # Security configuration for command execution
    command_guard: CommandPatternGuardConfig | None = None
    workspace: WorkspacePolicyConfig | None = None
    env_policy: EnvPolicy = EnvPolicy.STANDARD

    @field_validator("allowed_dirs")
    @classmethod
    def _validate_allowed_dirs(cls, v: list[str]) -> list[str]:
        if not v:
            v = [_get_default_temp_dir()]
        for d in v:
            p = Path(d)
            if not p.is_absolute():
                raise ValueError(f"allowed_dirs must be absolute paths: {d}")
        return v

    @field_validator("max_execution_time_seconds")
    @classmethod
    def _validate_max_execution_time_seconds(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"max_execution_time_seconds must be positive, got {v}")
        return v

    @field_validator("memory_limit_mb")
    @classmethod
    def _validate_memory_limit_mb(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(f"memory_limit_mb must be positive when specified, got {v}")
        return v

    @field_validator("cpu_limit")
    @classmethod
    def _validate_cpu_limit(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(f"cpu_limit must be positive when specified, got {v}")
        return v

    @field_validator("artifact_max_size")
    @classmethod
    def _validate_artifact_max_size(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"artifact_max_size must be positive, got {v}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _normalize_workspace_dir(cls, values: dict) -> dict:
        """Absolutize workspace_dir before validation (rule 17: no
        object.__setattr__ on the frozen instance)."""
        if isinstance(values, dict) and "workspace_dir" in values:
            values = dict(values)
            values["workspace_dir"] = str(Path(values["workspace_dir"]).absolute())
        return values
