"""Real host execution plane for the B7 memory sentinel."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal, assert_never
from uuid import uuid4

from evals.sentinel.tasks import SentinelArm, SentinelTask
from pydantic import BaseModel, ConfigDict

from bot.eval.agent_harness import build_runtime_services
from bot.eval.harbor.agent import HarborTaskResult, InstallExecutionResult, InstallProbeResult
from bot.eval.memory_harness import run_dream_until_exhausted
from bot.eval.sentinel import orchestrator as sentinel_orchestrator
from bot.eval.sentinel.observation import evaluate_observation
from bot.eval.sentinel.results import SentinelTaskObservation, SentinelTaskStatus
from bot.service.pool.declaration import boot_scope_spec
from bot.workspace.handle import WorkspaceHandle, WorkspaceHandleRootProvider
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.scope import MemoryAgentRole, MemoryContext
from modex_agent.messaging.models import InputMessage
from modex_agent.plugins.assembly import single_agent
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.trace.experiment_attrs import ExperimentLinkage, attach_experiment_attrs
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.score_injector import L2ScoreInjector, ScoreSpec
from modex_agent.trace.semconv import GenAiAttr, SpanKind
from modex_agent.trace.store import SpanModel

type LinkageFactory = Callable[[sentinel_orchestrator.SentinelInstance], ExperimentLinkage]
SENTINEL_VERDICT_SCORE: Final = "verdict_memory_chain_v1"
SENTINEL_VERDICT_VERSION: Final = "sentinel.v1"
_SENTINEL_SYSTEM_PROMPT: Final = "Complete the task using the available workspace tools."
_BOT_PROJECT: Final = Path(__file__).resolve().parents[3]
_DECLARATIONS_DIR: Final = _BOT_PROJECT / "config" / "scopes" / "eval" / "agents"


class SentinelTraceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    arm: SentinelArm
    trace_id: str
    observation_id: str


class _VerdictProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: Literal["verifier"] = "verifier"
    version: Literal["sentinel.v1"] = SENTINEL_VERDICT_VERSION
    report_source: Literal["official_harness"] = "official_harness"
    run_ref: str


class SentinelTraceRequiredError(RuntimeError):
    pass


class HostSentinelExecutionPlane(sentinel_orchestrator.SentinelExecutionPlane):
    """Run B7 directly on the host while preserving the Harbor execution contract."""

    def __init__(
        self,
        provider: LLMProvider,
        linkage_factory: LinkageFactory,
        *,
        run_ref: str,
        score_injector: L2ScoreInjector | None = None,
    ) -> None:
        self._provider = provider
        self._linkage_factory = linkage_factory
        self._run_ref = run_ref
        self._score_injector = score_injector
        self.trace_records: list[SentinelTraceRecord] = []

    async def probe_install(self, instance: sentinel_orchestrator.SentinelInstance) -> InstallProbeResult:
        _ = instance
        return InstallProbeResult(python_available=True, apt_available=False)

    async def execute_install(
        self, request: sentinel_orchestrator.SentinelTaskRunRequest
    ) -> InstallExecutionResult:
        _ = request
        return InstallExecutionResult(task_result=HarborTaskResult.READY, include_in_aggregate=True)

    async def run_agent_turn(
        self,
        request: sentinel_orchestrator.SentinelTaskRunRequest,
        memory_harness_factory: sentinel_orchestrator.MemoryHarnessFactory,
    ) -> SentinelTaskObservation:
        instance = request.instance
        linkage = self._linkage_factory(instance)
        world = instance.workspace.mount.host_path / "world"
        world.mkdir(parents=True, exist_ok=True)
        assembled, runtime_services = await self._assemble_arm(
            instance, memory_harness_factory, linkage
        )
        session = SessionInfo.from_str(f"sentinel.{instance.instance_id}.react")
        pipeline = assembled.instance.pipeline
        assert pipeline is not None
        try:
            message = InputMessage(
                content=instance.task.prompt,
                session=session,
                metadata={"user_id": "sentinel-user"},
                workspace=world,
            )
            result = await pipeline.process_message(message)
            if result is None:
                raise sentinel_orchestrator.SentinelExecutionError("sentinel turn suspended")
            if result.error is None and instance.task.establishes_facts:
                await self._persist_facts(
                    assembled.memory_system, session.session_id, instance.task
                )
            if assembled.descriptor.memory_config.dream_engine is not None:
                await run_dream_until_exhausted(assembled.context_manager.memory_system)
            observation = evaluate_observation(instance.task, world, result.content or "", result.error)
            await self._emit_task_span(
                runtime_services.trace_store,
                linkage,
                session.session_id,
                instance.arm,
                instance.task.task_id,
                observation.status is SentinelTaskStatus.SUCCESS,
            )
            return observation
        finally:
            await assembled.close()

    async def _assemble_arm(
        self,
        instance: sentinel_orchestrator.SentinelInstance,
        memory_harness_factory: sentinel_orchestrator.MemoryHarnessFactory,
        linkage: ExperimentLinkage,
    ) -> tuple[single_agent.SingleAgentAssembled, AgentRuntimeServices]:
        workspace = instance.workspace.mount.host_path
        match instance.arm:
            case SentinelArm.MEMORY:
                declaration_path = _DECLARATIONS_DIR / "sentinel-memory.yml"
                memory_bundle = await memory_harness_factory(
                    workspace, self._provider, _SENTINEL_SYSTEM_PROMPT
                )
                runtime_services = memory_bundle.runtime_services
                memory_bundle.memory_trace_hook.experiment_linkage = linkage
            case SentinelArm.NOMEMORY:
                declaration_path = _DECLARATIONS_DIR / "sentinel-nomemory.yml"
                memory_bundle = None
                runtime_services = build_runtime_services(
                    workspace / "trace", model=self._provider.get_default_model()
                )
            case unreachable:
                assert_never(unreachable)
        component_registry = ComponentRegistry()
        with PluginRegistrationContext(component_registry) as registration:
            DefaultPlugin().register(registration)
        declaration = load_scope_declaration(declaration_path)
        scope_boot = boot_scope_spec(
            declaration,
            project_dir=_BOT_PROJECT,
            data_dir=workspace,
            graphs_dirs=(),
            default_llm_provider="default",
            registry=component_registry,
        )
        hooks = ()
        if runtime_services.hooks is not None:
            hooks = tuple(spec.hook for spec in runtime_services.hooks.hook_specs)
        world = workspace / "world"
        root_provider = WorkspaceHandleRootProvider(WorkspaceHandle(target=world, data_root=workspace))
        infra = single_agent.SingleAgentInfra(
            llm_provider=self._provider,
            safety=RuntimeSafetyPolicy(),
            root_provider=root_provider,
            extra_hooks=hooks,
            governance_enabled=True,
            emitter_factory=None,
        )
        assembled = await single_agent.assemble_declared_single_agent(
            scope_boot.compilation.agents[0],
            infra,
            project_dir=_BOT_PROJECT,
            data_dir=workspace,
            component_registry=component_registry,
        )
        if memory_bundle is not None:
            assert assembled.descriptor.memory_config == memory_bundle.memory_config
            assembled.memory_system.add_cleanup_hook(memory_bundle.memory_trace_hook)
            await memory_bundle.assembly.close()
        return assembled, runtime_services

    async def _persist_facts(
        self,
        memory_system: MemorySystem,
        session_id: str,
        task: SentinelTask,
    ) -> None:
        # Pre-run data seeding for later chain tasks, not assembly.
        context = MemoryContext(
            session_id=session_id, user_id="sentinel-user", agent_id="react",
            agent_role=MemoryAgentRole.MAIN,
        )
        directory = await memory_system.get_core_memory_directory(context)
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "MEMORY.md"
        existing = path.read_text(encoding="utf-8") if path.is_file() else "# Memory\n"
        facts = "\n".join(f"- {fact.statement}" for fact in task.establishes_facts)
        path.write_text(f"{existing.rstrip()}\n{facts}\n", encoding="utf-8")

    async def _emit_task_span(
        self,
        store: OtelSpanTraceStore | None,
        linkage: ExperimentLinkage,
        session_id: str,
        arm: SentinelArm,
        task_id: str,
        succeeded: bool,
    ) -> SentinelTraceRecord:
        if store is None:
            raise SentinelTraceRequiredError
        now = time.time()
        span = attach_experiment_attrs(
            SpanModel(
                trace_id=uuid4().hex,
                span_id=uuid4().hex[:16],
                name="sentinel.task",
                kind=SpanKind.INTERNAL,
                start_time=now, end_time=now,
                attributes={GenAiAttr.CONVERSATION_ID: session_id},
            ),
            linkage,
        )
        await store.save_span(span)
        record = SentinelTraceRecord(
            task_id=task_id, arm=arm, trace_id=span.trace_id, observation_id=span.span_id
        )
        self.trace_records.append(record)
        if self._score_injector is not None:
            provenance = _VerdictProvenance(run_ref=self._run_ref).model_dump_json()
            await self._score_injector.inject_score_batch(
                span.trace_id,
                [
                    ScoreSpec(
                        name=SENTINEL_VERDICT_SCORE, value=succeeded,
                        data_type="BOOLEAN", comment=provenance,
                    )
                ],
                observation_id=span.span_id,
            )
        return record


__all__ = [
    "HostSentinelExecutionPlane",
    "LinkageFactory",
    "SENTINEL_VERDICT_SCORE",
    "SENTINEL_VERDICT_VERSION",
    "SentinelTraceRequiredError",
    "SentinelTraceRecord",
    "evaluate_observation",
]
