from __future__ import annotations

from pathlib import Path

from bot.eval.harbor.memory_workspace import (
    CONTAINER_MEMORY_PATH,
    MODEX_MEMORY_NS,
    MemoryArm,
    MemoryWorkspaceRequest,
    build_memory_workspace,
    cleanup_memory_workspace,
    prepare_memory_workspace,
)


def test_memory_workspace_contract_uses_fixed_mount_and_namespace_env(tmp_path: Path) -> None:
    plan = build_memory_workspace(
        MemoryWorkspaceRequest(
            root=tmp_path,
            arm=MemoryArm.MEMORY,
            namespace="memory-chain-v1.run.memory",
            instance_id="task-1",
        )
    )

    assert CONTAINER_MEMORY_PATH == "~/.modex/memory"
    assert MODEX_MEMORY_NS == "MODEX_MEMORY_NS"
    assert plan.mount.container_path == CONTAINER_MEMORY_PATH
    assert plan.environment == {MODEX_MEMORY_NS: "memory-chain-v1.run.memory"}


def test_memory_arm_shares_host_workspace_across_instances(tmp_path: Path) -> None:
    first = build_memory_workspace(
        MemoryWorkspaceRequest(
            root=tmp_path,
            arm=MemoryArm.MEMORY,
            namespace="experiment.memory",
            instance_id="task-1",
        )
    )
    second = build_memory_workspace(
        MemoryWorkspaceRequest(
            root=tmp_path,
            arm=MemoryArm.MEMORY,
            namespace="experiment.memory",
            instance_id="task-2",
        )
    )

    assert first.mount.host_path == second.mount.host_path
    assert first.cleanup_after_run is False


def test_nomemory_arm_isolates_each_instance_and_cleans_up(tmp_path: Path) -> None:
    first = build_memory_workspace(
        MemoryWorkspaceRequest(
            root=tmp_path,
            arm=MemoryArm.NO_MEMORY,
            namespace="experiment.nomemory",
            instance_id="task-1",
        )
    )
    second = build_memory_workspace(
        MemoryWorkspaceRequest(
            root=tmp_path,
            arm=MemoryArm.NO_MEMORY,
            namespace="experiment.nomemory",
            instance_id="task-2",
        )
    )
    prepare_memory_workspace(first)
    (first.mount.host_path / "state.txt").write_text("isolated", encoding="utf-8")

    cleanup_memory_workspace(first)

    assert first.mount.host_path != second.mount.host_path
    assert first.mount.container_path == second.mount.container_path == CONTAINER_MEMORY_PATH
    assert first.cleanup_after_run is True
    assert not first.mount.host_path.exists()
