# noqa: C901  # noqa: SIZE_OK - W1-b requires one Langfuse orchestration boundary.
"""Experiment runner — run declared agents against Langfuse datasets.

Wraps a declared agent as a Langfuse experiment task. Each dataset item gets a
fresh agent, per-turn execution context, and runtime services (zero state
leakage between items).

Layer 2 of the eval architecture (ADR-0024, IN15 step 6): dataset -> runner ->
traces + scores. Runs as a separate process (opt-in via the ``[eval]`` extra)
to avoid OTel tracer-provider conflicts with the bot's JSON-OTLP trace path.

Usage::

    runner = EvalRunner(
        provider=my_llm_provider,
        system_prompt="You are a helpful assistant.",
        langfuse_client=Langfuse(host=..., public_key=..., secret_key=...),
    )
    result = runner.run(
        dataset_name="react-baseline",
        experiment_name="v1",
        evaluators=[exact_match_evaluator],
    )
    print(result.format())
"""

from __future__ import annotations

import asyncio  # noqa: TID251  # noqa: ANYIO_OK - required asyncio.to_thread contract
import logging
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal, assert_never

from langfuse import Langfuse
from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from langfuse.experiment import ExperimentResult

from bot.eval.agent_harness import (
    assemble_harness_agent,
    build_runtime_services,
    build_trace_only_services,
    static_system_prompt,
    wrap_provider,
)
from bot.eval.task_output import EvalTaskOutput, ToolStats, TurnRecord, WorldResult
from bot.eval.task_spec import (
    CommandExitAssertion,
    EvalItemSpec,
    EvalToolset,
    FileAbsentAssertion,
    FileContainsAssertion,
    FileExistsAssertion,
    WorldAssertion,
)
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import Agent, AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolManager
from modex_agent.core.types import MessageRole
from modex_agent.memory.history import ListMessageHistory
from modex_agent.plugins.assembly.single_agent import SingleAgentAssembled
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.cassette import CassetteRecorder, CassetteReplayEngine
from modex_agent.trace.scoring import TrajectoryMetrics

logger = logging.getLogger(__name__)


class _ArchiveItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str
    output: dict[str, Any]


class _RunArchive(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset: str
    experiment: str
    ts: str
    items: list[_ArchiveItem]


class UnsafeWorkspacePathError(ValueError):
    pass


def _workspace_path(workspace: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if (
        candidate.is_absolute()
        or PureWindowsPath(raw_path).is_absolute()
        or ".." in candidate.parts
    ):
        raise UnsafeWorkspacePathError(f"Unsafe eval workspace path: {raw_path}")
    resolved = (workspace / candidate).resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise UnsafeWorkspacePathError(f"Unsafe eval workspace path: {raw_path}")
    return resolved


async def _evaluate_world_assertion(
    assertion: WorldAssertion,
    workspace: Path,
) -> WorldResult:
    match assertion:
        case FileExistsAssertion(path=raw_path):
            exists = _workspace_path(workspace, raw_path).exists()
            return WorldResult(
                assertion=f"file_exists:{raw_path}",
                passed=exists,
                detail="path exists" if exists else "path does not exist",
            )
        case FileAbsentAssertion(path=raw_path):
            absent = not _workspace_path(workspace, raw_path).exists()
            return WorldResult(
                assertion=f"file_absent:{raw_path}",
                passed=absent,
                detail="path is absent" if absent else "path still exists",
            )
        case FileContainsAssertion(path=raw_path, content=expected):
            path = _workspace_path(workspace, raw_path)
            contains = path.is_file() and expected in path.read_text(encoding="utf-8")
            return WorldResult(
                assertion=f"file_contains:{raw_path}",
                passed=contains,
                detail="content found" if contains else "content not found",
            )
        case CommandExitAssertion(command=command, expected_exit=expected_exit):
            label = f"command_exit:{' '.join(command)}"
            try:
                completed = await asyncio.to_thread(
                    subprocess.run,
                    command,
                    cwd=workspace,
                    timeout=60,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return WorldResult(assertion=label, passed=False, detail=str(exc))
            return WorldResult(
                assertion=label,
                passed=completed.returncode == expected_exit,
                detail=f"exit code {completed.returncode} (expected {expected_exit})",
            )
        case unreachable:
            assert_never(unreachable)


def _span_tool_stats(contexts: list[AgentContext]) -> ToolStats:
    """Aggregate the per-turn stashed TrajectoryMetrics into item-level stats.

    Reads the stash ``RootSpanHook`` leaves on each turn's context
    (``runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]``, written
    before ``clear_trace`` on every turn outcome) — the same state object
    the runner built and the hook mutated, so no trace-store read-back is
    needed. A turn without a stash (OFF mode, hook-less harness)
    contributes zero.
    """
    total = 0
    errors = 0
    for context in contexts:
        assert context.runtime is not None
        stashed = context.runtime.state.custom.get(TurnCustomKey.TRAJECTORY_METRICS)
        if isinstance(stashed, TrajectoryMetrics):
            total += stashed.tool_call_count
            errors += stashed.error_tool_count
    success_rate = (total - errors) / total if total > 0 else 1.0
    return ToolStats(total=total, errors=errors, success_rate=success_rate, source="metrics")


class _NoopEmitter(ContentEmitter[ReActEvent]):
    """Minimal emitter that discards all events.

    Eval runs consume only the final ``AgentResult``; streaming events are
    not needed. All abstract methods are no-ops.
    """

    async def emit_delta(self, delta: str) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        pass

    async def emit_error(self, error: str) -> None:
        pass


class EvalRunner:
    """Run declared-agent experiments against Langfuse datasets.

    Constructs a fresh mode-specific agent, per-turn execution context, and
    runtime-services bundle per item. Multi-turn v2 items share those services
    across their fresh per-turn contexts.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        system_prompt: str,
        max_iterations: int = 10,
        langfuse_client: Langfuse | None = None,
        mode: Literal["clean", "production"] = "clean",
        cassette: CassetteReplayEngine | None = None,
        recorder: CassetteRecorder | None = None,
        archive_root: Path | None = None,
        model: str | None = None,
    ) -> None:
        """Store config used to build the clean-mode declared agent.

        Args:
            provider: LLM provider for the agent.
            system_prompt: System prompt applied to every eval item.
            max_iterations: ReAct loop cap per item.
            langfuse_client: Optional pre-built Langfuse client. When
                ``None``, ``Langfuse()`` is constructed from env vars
                (``LANGFUSE_HOST`` etc.) at ``run`` time.
        """
        self._provider = provider
        self._system_prompt = system_prompt
        self._max_iterations = max_iterations
        self._lf = langfuse_client
        self._mode: Literal["clean", "production"] = mode
        self._cassette = cassette
        self._recorder = recorder
        self._archive_root = archive_root
        self._model = model

    async def _build_agent_and_services(
        self,
        provider: LLMProvider,
        trace_dir: Path,
        workspace: Path,
        toolset: EvalToolset,
        deny_tools: list[str],
    ) -> tuple[
        Agent[ReActEvent],
        AgentRuntimeServices,
        ToolManager,
        SingleAgentAssembled,
    ]:
        match self._mode:
            case "clean":
                services = build_trace_only_services(trace_dir, model=self._model)
                governance_enabled = False
            case "production":
                services = build_runtime_services(
                    trace_dir=trace_dir,
                    recorder=None,
                    model=self._model,
                )
                governance_enabled = True
            case unreachable:
                assert_never(unreachable)
        assembled = await assemble_harness_agent(
            workspace=workspace,
            data_dir=trace_dir / "assembly",
            provider=provider,
            toolset=toolset,
            deny_tools=deny_tools,
            runtime_services=services,
            governance_enabled=governance_enabled,
        )
        assert assembled.instance.pipeline is not None
        return assembled.instance.pipeline.agent, services, assembled.tool_manager, assembled

    async def task(
        self,
        *,
        item: object,  # noqa: ANN401  # noqa: OBJECT_OK - Langfuse callback boundary
        **kwargs: object,  # noqa: ANN401  # noqa: OBJECT_OK - Langfuse callback boundary
    ) -> dict[str, Any]:
        """Run a legacy single-turn or validated v2 eval item."""
        _ = kwargs
        raw_input = getattr(item, "input", None)
        try:
            spec = EvalItemSpec.from_item_input(raw_input)
        except ValidationError as exc:
            logger.warning("EvalRunner: v2 item failed", exc_info=True)
            return self._error_output(str(exc))
        if spec is None:
            return await self._legacy_task(item=item)
        try:
            return await self._v2_task(spec)
        except Exception as exc:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
            logger.warning("EvalRunner: v2 item failed", exc_info=True)
            return self._error_output(str(exc))

    async def _legacy_task(
        self,
        *,
        item: object,  # noqa: ANN401  # noqa: OBJECT_OK - legacy Langfuse boundary
    ) -> dict[str, Any]:
        raw_input = getattr(item, "input", None)
        if isinstance(raw_input, dict):
            query = str(raw_input.get("query") or "")
        elif raw_input is not None:
            query = str(raw_input)
        else:
            query = ""

        trace_dir = Path(tempfile.mkdtemp(prefix="modex-eval-trace-legacy-"))
        assembled: SingleAgentAssembled | None = None
        try:
            item_id = getattr(item, "id", "unknown")
            session = SessionInfo.from_str(f"eval.{item_id}.react")
            identity = TurnIdentity(agent_id="react", session=session, turn_id="turn-1")
            state = ReActTurnState(
                identity=identity,
                agent_kind=AgentKind.REACT,
                phase=TurnPhase.CREATED,
            )
            agent, services, tool_manager, assembled = await self._build_agent_and_services(
                self._provider, trace_dir, trace_dir, EvalToolset.NONE, []
            )
            ctx = AgentContext(
                system_prompt=self._system_prompt,
                history=ListMessageHistory(),
                tool_manager=tool_manager,
                session=session,
                max_iterations=self._max_iterations,
                runtime=AgentRuntime(services=services, state=state),
                identity=identity,
            )
            await ctx.history.append(ChatMessage(role=MessageRole.USER, content=query))

            emitter = _NoopEmitter()
            try:
                result = await agent.run(ctx, emitter)
            except Exception as exc:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
                logger.warning(
                    "EvalRunner: agent run failed for item %s",
                    item_id,
                    exc_info=True,
                )
                return {
                    "output": "",
                    "stop_reason": StopReason.ERROR.value,
                    "error": str(exc),
                }
            return {
                "output": result.content or "",
                "stop_reason": result.stop_reason.value,
                "error": result.error,
            }
        finally:
            if assembled is not None:
                await assembled.instance.stop()
                await assembled.memory_system.close()
            shutil.rmtree(trace_dir, ignore_errors=True)

    async def _v2_task(self, spec: EvalItemSpec) -> dict[str, Any]:
        workspace = Path(tempfile.mkdtemp(prefix=f"modex-eval-{spec.id}-"))
        trace_dir = Path(tempfile.mkdtemp(prefix=f"modex-eval-trace-{spec.id}-"))
        assembled: SingleAgentAssembled | None = None
        try:
            for raw_path, content in spec.world_setup.items():
                path = _workspace_path(workspace, raw_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            history = ListMessageHistory()
            session = SessionInfo.from_str(f"eval.{spec.id}.react")
            provider = self._provider
            cassette = self._cassette if self._cassette is not None else self._recorder
            if cassette is not None:
                provider = wrap_provider(provider, cassette)

            agent, services, tool_manager, assembled = await self._build_agent_and_services(
                provider, trace_dir, workspace, spec.toolset, spec.deny_tools
            )

            turn_records: list[TurnRecord] = []
            turn_contexts: list[AgentContext] = []
            stop_mismatches: list[str] = []
            emitter = _NoopEmitter()
            for turn_number, turn in enumerate(spec.turns, start=1):
                identity = TurnIdentity(
                    agent_id="react",
                    session=session,
                    turn_id=f"turn-{turn_number}",
                )
                state = ReActTurnState(
                    identity=identity,
                    agent_kind=AgentKind.REACT,
                    phase=TurnPhase.CREATED,
                )
                context = AgentContext(
                    system_prompt=static_system_prompt(self._system_prompt),
                    history=history,
                    tool_manager=tool_manager,
                    session=session,
                    max_iterations=self._max_iterations,
                    runtime=AgentRuntime(services=services, state=state),
                    identity=identity,
                    workspace=workspace,
                )
                await history.append(ChatMessage(role=MessageRole.USER, content=turn.user))
                result = await agent.run(context, emitter)
                stop_reason = result.stop_reason
                turn_records.append(
                    TurnRecord(
                        stop_reason=stop_reason,
                        error=result.error,
                        content=result.content or "",
                    )
                )
                turn_contexts.append(context)
                if turn.expected_stop is not None and turn.expected_stop != stop_reason.value:
                    stop_mismatches.append(
                        f"turn {turn_number}: expected {turn.expected_stop}, got {stop_reason.value}"
                    )

            world_results = [
                await _evaluate_world_assertion(assertion, workspace)
                for assertion in spec.world_assertions
            ]
            tool_stats = _span_tool_stats(turn_contexts)
            final_turn = turn_records[-1]
            return EvalTaskOutput(
                output=final_turn.content,
                stop_reason=final_turn.stop_reason,
                error=final_turn.error,
                world_results=world_results,
                tool_stats=tool_stats,
                turns_executed=len(turn_records),
                stop_mismatches=stop_mismatches,
                turn_records=turn_records,
            ).to_output_dict()
        finally:
            if assembled is not None:
                await assembled.instance.stop()
                await assembled.memory_system.close()
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(trace_dir, ignore_errors=True)

    def _error_output(self, error: str) -> dict[str, Any]:
        return EvalTaskOutput(
            output="",
            stop_reason=StopReason.ERROR,
            error=error,
            world_results=[],
            tool_stats=ToolStats(total=0, errors=0, success_rate=1.0, source="metrics"),
            turns_executed=0,
            stop_mismatches=[],
            turn_records=[],
        ).to_output_dict()

    def run(
        self,
        *,
        dataset_name: str,
        experiment_name: str,
        description: str = "",
        evaluators: list[Any] | None = None,
        max_concurrency: int = 5,
    ) -> ExperimentResult:
        """Run an experiment against a Langfuse dataset.

        The async ``task`` method is invoked by the Langfuse SDK per dataset
        item (the SDK manages the event loop internally). Each item produces
        a trace plus scores from the supplied evaluators.

        Args:
            dataset_name: Name of the Langfuse dataset to run against.
            experiment_name: Human-readable name for this experiment run.
            description: Optional experiment description.
            evaluators: Langfuse evaluator callables. Each is called with the
                task output (and expected output when present) and returns an
                ``Evaluation``.
            max_concurrency: Maximum concurrent item executions.

        Returns:
            The Langfuse experiment result object -- call ``.format()`` for
            a human-readable summary.
        """
        item_outputs: list[_ArchiveItem] = []

        async def tee_task(
            *,
            item: object,  # noqa: ANN401  # noqa: OBJECT_OK - Langfuse callback boundary
            **kwargs: object,  # noqa: ANN401  # noqa: OBJECT_OK - Langfuse callback boundary
        ) -> dict[str, Any]:
            output = await self.task(item=item, **kwargs)
            item_outputs.append(
                _ArchiveItem(
                    item_id=str(getattr(item, "id", "unknown")),
                    output=output,
                )
            )
            return output

        lf = self._lf or Langfuse()
        dataset = lf.get_dataset(dataset_name)
        result = dataset.run_experiment(
            name=experiment_name,
            # run_name= keeps the SDK from appending a timestamp suffix; judge/compare match the clean name exactly.
            run_name=experiment_name,
            description=description,
            task=tee_task,
            evaluators=evaluators or [],
            max_concurrency=max_concurrency,
        )
        if self._archive_root is not None:
            now = datetime.now(UTC)
            archive_dir = self._archive_root / dataset_name / experiment_name
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive = _RunArchive(
                dataset=dataset_name,
                experiment=experiment_name,
                ts=now.isoformat().replace("+00:00", "Z"),
                items=item_outputs,
            )
            archive_path = archive_dir / now.strftime("%Y%m%dT%H%M%S.%fZ.json")
            archive_path.write_text(archive.model_dump_json(indent=2), encoding="utf-8")
        return result


__all__ = ["EvalRunner"]
