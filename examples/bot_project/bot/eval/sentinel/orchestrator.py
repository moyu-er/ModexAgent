from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import assert_never

from evals.sentinel.tasks import (
    MEMORY_CHAIN_V1_CHAIN,
    SentinelArm,
    SentinelTask,
    experiment_name,
)
from pydantic import BaseModel, ConfigDict, Field

from bot.eval.harbor.agent import (
    HarborTaskResult,
    InstallExecutionResult,
    InstallPlan,
    InstallProbeResult,
    InstallSettings,
    build_install_plan,
)
from bot.eval.harbor.memory_workspace import (
    MemoryArm,
    MemoryWorkspacePlan,
    MemoryWorkspaceRequest,
    build_memory_workspace,
    cleanup_memory_workspace,
    prepare_memory_workspace,
)
from bot.eval.memory_harness import MemoryRuntimeServices, build_memory_runtime_services
from bot.eval.sentinel.report import SentinelDifferenceReport, generate_difference_report
from bot.eval.sentinel.results import (
    SentinelTaskObservation,
    SentinelTaskResult,
    SentinelTaskStatus,
)
from modex_agent.core.provider import LLMProvider

type MemoryHarnessFactory = Callable[
    [Path, LLMProvider, str],
    Awaitable[MemoryRuntimeServices],
]


class SentinelExecutionError(RuntimeError):
    pass


class SentinelRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    seed: int
    memory_root: Path
    install_settings: InstallSettings = Field(default_factory=InstallSettings)


class SentinelInstance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str = Field(min_length=1)
    arm: SentinelArm
    experiment_name: str = Field(min_length=1)
    seed: int
    task: SentinelTask
    workspace: MemoryWorkspacePlan


class SentinelTaskRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance: SentinelInstance
    install_plan: InstallPlan
    environment: dict[str, str]


class SentinelArmRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: SentinelArm
    experiment_name: str
    task_results: tuple[SentinelTaskResult, ...]


class SentinelRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    seed: int
    arms: tuple[SentinelArmRunResult, SentinelArmRunResult]
    report: SentinelDifferenceReport


class SentinelExecutionPlane(ABC):
    """Injected container, install, and agent-turn surface used by T30/T31."""

    @abstractmethod
    async def probe_install(self, instance: SentinelInstance) -> InstallProbeResult:
        """Probe the instance's Python and apt capabilities."""

    @abstractmethod
    async def execute_install(
        self,
        request: SentinelTaskRunRequest,
    ) -> InstallExecutionResult:
        """Execute the T26 install plan inside the instance."""

    @abstractmethod
    async def run_agent_turn(
        self,
        request: SentinelTaskRunRequest,
        memory_harness_factory: MemoryHarnessFactory,
    ) -> SentinelTaskObservation:
        """Run one fresh-session task with the T13 memory harness factory."""


class SentinelOrchestrator:
    def __init__(
        self,
        execution: SentinelExecutionPlane,
        memory_harness_factory: MemoryHarnessFactory = build_memory_runtime_services,
    ) -> None:
        self._execution = execution
        self._memory_harness_factory = memory_harness_factory

    async def run(self, request: SentinelRunRequest) -> SentinelRunResult:
        arm_results = (
            await self._run_arm(request, SentinelArm.MEMORY),
            await self._run_arm(request, SentinelArm.NOMEMORY),
        )
        task_results = tuple(
            task_result for arm_result in arm_results for task_result in arm_result.task_results
        )
        return SentinelRunResult(
            run_id=request.run_id,
            seed=request.seed,
            arms=arm_results,
            report=generate_difference_report(task_results),
        )

    async def _run_arm(
        self,
        request: SentinelRunRequest,
        arm: SentinelArm,
    ) -> SentinelArmRunResult:
        arm_experiment_name = experiment_name(
            MEMORY_CHAIN_V1_CHAIN.name,
            request.run_id,
            arm,
        )
        task_results: list[SentinelTaskResult] = []
        for index, task in enumerate(MEMORY_CHAIN_V1_CHAIN.tasks, start=1):
            instance = SentinelInstance(
                instance_id=f"{arm.value}-{index}-{task.task_id}",
                arm=arm,
                experiment_name=arm_experiment_name,
                seed=request.seed,
                task=task,
                workspace=build_memory_workspace(
                    MemoryWorkspaceRequest(
                        root=request.memory_root,
                        arm=_workspace_arm(arm),
                        namespace=arm_experiment_name,
                        instance_id=f"{arm.value}-{index}-{task.task_id}",
                    )
                ),
            )
            task_results.append(await self._run_task(instance, request.install_settings))
        return SentinelArmRunResult(
            arm=arm,
            experiment_name=arm_experiment_name,
            task_results=tuple(task_results),
        )

    async def _run_task(
        self,
        instance: SentinelInstance,
        settings: InstallSettings,
    ) -> SentinelTaskResult:
        prepare_memory_workspace(instance.workspace)
        try:
            probe = await self._execution.probe_install(instance)
            install_plan = build_install_plan(probe, settings)
            run_request = SentinelTaskRunRequest(
                instance=instance,
                install_plan=install_plan,
                environment={**install_plan.environment, **instance.workspace.environment},
            )
            install_result = await self._execution.execute_install(run_request)
            observation = await self._observe_task(run_request, install_result)
            return SentinelTaskResult(
                task_id=instance.task.task_id,
                arm=instance.arm,
                status=observation.status,
                world_assertions=observation.world_assertions,
                memory_assertions=observation.memory_assertions,
                error=observation.error,
            )
        except SentinelExecutionError as error:
            return SentinelTaskResult(
                task_id=instance.task.task_id,
                arm=instance.arm,
                status=SentinelTaskStatus.ERROR,
                world_assertions=(),
                memory_assertions=(),
                error=str(error),
            )
        finally:
            if instance.workspace.cleanup_after_run:
                cleanup_memory_workspace(instance.workspace)

    async def _observe_task(
        self,
        request: SentinelTaskRunRequest,
        install_result: InstallExecutionResult,
    ) -> SentinelTaskObservation:
        match install_result.task_result:
            case HarborTaskResult.READY:
                return await self._execution.run_agent_turn(
                    request,
                    self._memory_harness_factory,
                )
            case HarborTaskResult.NO_TEST:
                return SentinelTaskObservation(
                    status=SentinelTaskStatus.NO_TEST,
                    world_assertions=(),
                    memory_assertions=(),
                    error=(
                        install_result.install_skipped.value
                        if install_result.install_skipped is not None
                        else HarborTaskResult.NO_TEST.value
                    ),
                )
            case HarborTaskResult.INSTALL_FAILED:
                return SentinelTaskObservation(
                    status=SentinelTaskStatus.INSTALL_FAILED,
                    world_assertions=(),
                    memory_assertions=(),
                    error=install_result.guidance or HarborTaskResult.INSTALL_FAILED.value,
                )
            case unreachable:
                assert_never(unreachable)


def _workspace_arm(arm: SentinelArm) -> MemoryArm:
    match arm:
        case SentinelArm.MEMORY:
            return MemoryArm.MEMORY
        case SentinelArm.NOMEMORY:
            return MemoryArm.NO_MEMORY
        case unreachable:
            assert_never(unreachable)


__all__ = [
    "MemoryHarnessFactory",
    "SentinelArmRunResult",
    "SentinelExecutionError",
    "SentinelExecutionPlane",
    "SentinelInstance",
    "SentinelOrchestrator",
    "SentinelRunRequest",
    "SentinelRunResult",
    "SentinelTaskRunRequest",
]
