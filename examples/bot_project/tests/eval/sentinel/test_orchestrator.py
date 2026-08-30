"""Mock-backend tests for the ticket-14 sentinel dual-arm orchestrator."""

from __future__ import annotations

from pathlib import Path

from bot.eval.harbor.agent import (
    HarborTaskResult,
    InstallExecutionResult,
    InstallProbeResult,
)
from bot.eval.harbor.memory_workspace import MODEX_MEMORY_NS
from bot.eval.memory_harness import MemoryRuntimeServices
from bot.eval.sentinel.orchestrator import (
    MemoryHarnessFactory,
    SentinelExecutionError,
    SentinelExecutionPlane,
    SentinelInstance,
    SentinelOrchestrator,
    SentinelRunRequest,
    SentinelTaskRunRequest,
)
from bot.eval.sentinel.report import generate_difference_report
from bot.eval.sentinel.results import (
    AssertionResult,
    SentinelTaskObservation,
    SentinelTaskResult,
    SentinelTaskStatus,
)
from evals.sentinel.tasks import MEMORY_CHAIN_V1_CHAIN, SentinelArm

from modex_agent.core.provider import LLMProvider


async def _fake_harness_factory(
    workspace: Path,
    provider: LLMProvider,
    base_system_prompt: str = "",
) -> MemoryRuntimeServices:
    _ = (workspace, provider, base_system_prompt)
    raise AssertionError("the mock execution plane must not construct a real memory harness")


class _FakeExecutionPlane(SentinelExecutionPlane):
    """Mutable fake recording every injected container/install/turn call."""

    def __init__(
        self,
        *,
        fail_arm: SentinelArm | None = None,
        fail_task_id: str | None = None,
    ) -> None:
        self._fail_arm = fail_arm
        self._fail_task_id = fail_task_id
        self.probe_requests: list[SentinelInstance] = []
        self.install_requests: list[SentinelTaskRunRequest] = []
        self.turn_requests: list[SentinelTaskRunRequest] = []
        self.harness_factories: list[MemoryHarnessFactory] = []
        self.memory_seen: dict[tuple[SentinelArm, str], bool] = {}

    async def probe_install(self, instance: SentinelInstance) -> InstallProbeResult:
        self.probe_requests.append(instance)
        return InstallProbeResult(python_available=True, apt_available=False)

    async def execute_install(
        self,
        request: SentinelTaskRunRequest,
    ) -> InstallExecutionResult:
        self.install_requests.append(request)
        return InstallExecutionResult(
            task_result=HarborTaskResult.READY,
            include_in_aggregate=True,
        )

    async def run_agent_turn(
        self,
        request: SentinelTaskRunRequest,
        memory_harness_factory: MemoryHarnessFactory,
    ) -> SentinelTaskObservation:
        self.turn_requests.append(request)
        self.harness_factories.append(memory_harness_factory)
        instance = request.instance
        host_path = instance.workspace.mount.host_path
        (host_path / "instance.txt").write_text(instance.instance_id, encoding="utf-8")
        marker = host_path / "task-1-memory.txt"
        if instance.task.task_id == MEMORY_CHAIN_V1_CHAIN.tasks[0].task_id:
            marker.write_text("task-1 durable memory", encoding="utf-8")
        if instance.task.task_id == MEMORY_CHAIN_V1_CHAIN.tasks[1].task_id:
            self.memory_seen[(instance.arm, instance.task.task_id)] = marker.is_file()
        if instance.arm is self._fail_arm and instance.task.task_id == self._fail_task_id:
            raise SentinelExecutionError("mock agent turn failed")
        return SentinelTaskObservation(
            status=SentinelTaskStatus.SUCCESS,
            world_assertions=(AssertionResult(assertion_id="world", passed=True),),
            memory_assertions=tuple(
                AssertionResult(assertion_id=item.fact_id, passed=True)
                for item in instance.task.memory_assertions
            ),
        )


async def test_dual_arm_run_uses_exact_names_seed_tasks_and_memory_factory(
    tmp_path: Path,
) -> None:
    # Given: one injected execution plane and one injected T13 harness factory.
    execution = _FakeExecutionPlane()
    orchestrator = SentinelOrchestrator(
        execution=execution,
        memory_harness_factory=_fake_harness_factory,
    )

    # When: the frozen chain runs through both arms.
    result = await orchestrator.run(
        SentinelRunRequest(run_id="run-42", seed=731, memory_root=tmp_path)
    )

    # Then: names, task order, seed, install path, and harness assembly seam match.
    assert [arm.experiment_name for arm in result.arms] == [
        "memory-chain-v1.run-42.memory",
        "memory-chain-v1.run-42.nomemory",
    ]
    expected_tasks = [task.task_id for task in MEMORY_CHAIN_V1_CHAIN.tasks]
    assert [[item.task_id for item in arm.task_results] for arm in result.arms] == [
        expected_tasks,
        expected_tasks,
    ]
    assert [request.instance.seed for request in execution.install_requests] == [731] * 6
    assert len(execution.install_requests) == len(execution.turn_requests) == 6
    assert execution.harness_factories == [_fake_harness_factory] * 6


async def test_memory_workspace_is_shared_while_nomemory_is_isolated_and_cleaned(
    tmp_path: Path,
) -> None:
    execution = _FakeExecutionPlane()
    result = await SentinelOrchestrator(
        execution=execution,
        memory_harness_factory=_fake_harness_factory,
    ).run(SentinelRunRequest(run_id="paths", seed=7, memory_root=tmp_path))

    memory_requests = [
        request for request in execution.turn_requests if request.instance.arm is SentinelArm.MEMORY
    ]
    nomemory_requests = [
        request
        for request in execution.turn_requests
        if request.instance.arm is SentinelArm.NOMEMORY
    ]
    memory_paths = [request.instance.workspace.mount.host_path for request in memory_requests]
    nomemory_paths = [request.instance.workspace.mount.host_path for request in nomemory_requests]

    assert memory_paths == [tmp_path / "memory-chain-v1.paths.memory"] * 3
    assert len(set(nomemory_paths)) == 3
    assert all(
        path.parent == tmp_path / "memory-chain-v1.paths.nomemory" for path in nomemory_paths
    )
    assert {request.environment[MODEX_MEMORY_NS] for request in memory_requests} == {
        "memory-chain-v1.paths.memory"
    }
    assert {request.environment[MODEX_MEMORY_NS] for request in nomemory_requests} == {
        "memory-chain-v1.paths.nomemory"
    }
    assert execution.memory_seen[(SentinelArm.MEMORY, "apply-signoff-codename")] is True
    assert execution.memory_seen[(SentinelArm.NOMEMORY, "apply-signoff-codename")] is False
    assert memory_paths[0].is_dir()
    assert all(not path.exists() for path in nomemory_paths)
    assert all(item.status is SentinelTaskStatus.SUCCESS for item in result.arms[0].task_results)


async def test_one_arm_task_failure_preserves_complete_other_arm_data(
    tmp_path: Path,
) -> None:
    execution = _FakeExecutionPlane(
        fail_arm=SentinelArm.MEMORY,
        fail_task_id="apply-signoff-codename",
    )

    result = await SentinelOrchestrator(
        execution=execution,
        memory_harness_factory=_fake_harness_factory,
    ).run(SentinelRunRequest(run_id="failure", seed=99, memory_root=tmp_path))

    memory_arm, nomemory_arm = result.arms
    assert [item.status for item in memory_arm.task_results] == [
        SentinelTaskStatus.SUCCESS,
        SentinelTaskStatus.ERROR,
        SentinelTaskStatus.SUCCESS,
    ]
    assert memory_arm.task_results[1].error == "mock agent turn failed"
    assert [item.task_id for item in nomemory_arm.task_results] == [
        task.task_id for task in MEMORY_CHAIN_V1_CHAIN.tasks
    ]
    assert all(item.status is SentinelTaskStatus.SUCCESS for item in nomemory_arm.task_results)
    assert len(result.report.rows) == 6


def _task_result(
    arm: SentinelArm,
    task_id: str,
    status: SentinelTaskStatus,
) -> SentinelTaskResult:
    return SentinelTaskResult(
        task_id=task_id,
        arm=arm,
        status=status,
        world_assertions=(AssertionResult(assertion_id="world", passed=status.is_success),),
        memory_assertions=(AssertionResult(assertion_id="memory", passed=status.is_success),),
        error=None if status.is_success else f"{arm.value}:{task_id}:failed",
    )


def test_difference_report_when_both_arms_succeed_has_zero_delta() -> None:
    task_ids = tuple(task.task_id for task in MEMORY_CHAIN_V1_CHAIN.tasks)
    results = tuple(
        _task_result(arm, task_id, SentinelTaskStatus.SUCCESS)
        for arm in (SentinelArm.MEMORY, SentinelArm.NOMEMORY)
        for task_id in task_ids
    )

    report = generate_difference_report(results)

    assert report.memory.succeeded_tasks == report.nomemory.succeeded_tasks == 3
    assert report.difference.success_count_delta == 0
    assert report.difference.success_rate_delta == 0.0


def test_difference_report_when_memory_is_better_has_positive_delta() -> None:
    task_ids = tuple(task.task_id for task in MEMORY_CHAIN_V1_CHAIN.tasks)
    results = tuple(
        [
            _task_result(SentinelArm.MEMORY, task_id, SentinelTaskStatus.SUCCESS)
            for task_id in task_ids
        ]
        + [
            _task_result(SentinelArm.NOMEMORY, task_ids[0], SentinelTaskStatus.SUCCESS),
            _task_result(SentinelArm.NOMEMORY, task_ids[1], SentinelTaskStatus.FAILED),
            _task_result(SentinelArm.NOMEMORY, task_ids[2], SentinelTaskStatus.FAILED),
        ]
    )

    report = generate_difference_report(results)

    assert report.memory.succeeded_tasks == 3
    assert report.nomemory.succeeded_tasks == 1
    assert report.difference.success_count_delta == 2
    assert report.difference.success_rate_delta is not None
    assert report.difference.success_rate_delta > 0


def test_difference_report_truthfully_preserves_both_arms_all_failed() -> None:
    task_ids = tuple(task.task_id for task in MEMORY_CHAIN_V1_CHAIN.tasks)
    results = tuple(
        _task_result(arm, task_id, SentinelTaskStatus.ERROR)
        for arm in (SentinelArm.MEMORY, SentinelArm.NOMEMORY)
        for task_id in task_ids
    )

    report = generate_difference_report(results)

    assert report.memory.all_failed is True
    assert report.nomemory.all_failed is True
    assert report.difference.success_count_delta == 0
    assert len(report.rows) == 6
    assert all(row.error is not None for row in report.rows)


def test_difference_report_truthfully_preserves_one_arm_all_failed() -> None:
    task_ids = tuple(task.task_id for task in MEMORY_CHAIN_V1_CHAIN.tasks)
    results = tuple(
        [
            _task_result(SentinelArm.MEMORY, task_id, SentinelTaskStatus.ERROR)
            for task_id in task_ids
        ]
        + [
            _task_result(SentinelArm.NOMEMORY, task_id, SentinelTaskStatus.SUCCESS)
            for task_id in task_ids
        ]
    )

    report = generate_difference_report(results)

    assert report.memory.all_failed is True
    assert report.nomemory.all_failed is False
    assert report.difference.success_count_delta == -3
    assert [row.error for row in report.rows[:3]] == [
        f"memory:{task_id}:failed" for task_id in task_ids
    ]
