from __future__ import annotations

import shutil
from enum import StrEnum
from pathlib import Path
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, Field

MODEX_MEMORY_NS: Final = "MODEX_MEMORY_NS"
CONTAINER_MEMORY_PATH: Final = "~/.modex/memory"


class MemoryArm(StrEnum):
    MEMORY = "memory"
    NO_MEMORY = "nomemory"


class MemoryWorkspaceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path
    arm: MemoryArm
    namespace: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)


class MemoryMount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host_path: Path
    container_path: str


class MemoryWorkspacePlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: MemoryArm
    mount: MemoryMount
    environment: dict[str, str]
    cleanup_after_run: bool


def build_memory_workspace(request: MemoryWorkspaceRequest) -> MemoryWorkspacePlan:
    namespace_root = request.root / request.namespace
    match request.arm:
        case MemoryArm.MEMORY:
            host_path = namespace_root
            cleanup_after_run = False
        case MemoryArm.NO_MEMORY:
            host_path = namespace_root / request.instance_id
            cleanup_after_run = True
        case unreachable:
            assert_never(unreachable)
    return MemoryWorkspacePlan(
        arm=request.arm,
        mount=MemoryMount(
            host_path=host_path,
            container_path=CONTAINER_MEMORY_PATH,
        ),
        environment={MODEX_MEMORY_NS: request.namespace},
        cleanup_after_run=cleanup_after_run,
    )


def prepare_memory_workspace(plan: MemoryWorkspacePlan) -> None:
    plan.mount.host_path.mkdir(parents=True, exist_ok=True)


def cleanup_memory_workspace(plan: MemoryWorkspacePlan) -> None:
    match plan.arm:
        case MemoryArm.MEMORY:
            return
        case MemoryArm.NO_MEMORY:
            shutil.rmtree(plan.mount.host_path)
        case unreachable:
            assert_never(unreachable)


__all__ = [
    "CONTAINER_MEMORY_PATH",
    "MODEX_MEMORY_NS",
    "MemoryArm",
    "MemoryMount",
    "MemoryWorkspacePlan",
    "MemoryWorkspaceRequest",
    "build_memory_workspace",
    "cleanup_memory_workspace",
    "prepare_memory_workspace",
]
