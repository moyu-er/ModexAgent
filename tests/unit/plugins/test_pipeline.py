"""Tests for the assembly pipeline runner.

Asserts (SPEC §6.3 as revised by Errata-5 — main-agent orchestrator only):

- ``AssemblyStage`` ABC with async ``process()`` method.
- ``AssemblyPipeline`` constructor takes 4 stage instances (no hardcoded classes).
- ``_stages_for`` returns four native stages and three external stages.
- Subagent ``AgentType`` values raise ``ValueError`` — subagents construct
  through ``AgentTemplate.materialize``, never through this pipeline.
- ``run()`` is async.
- ``run()`` executes stages in order for each main agent_type.
- ``run()`` calls ``builder.cleanup()`` on exception, then re-raises.
- ``run()`` calls ``builder.build_agent()`` on success, returns ``AssembledAgent``.

Stage subset table (Errata-5):

    ============== ======= ==================================================
    Agent type     Stages  Description
    ============== ======= ==================================================
    native_main    1->2->3->4 WorkspaceMaterialize, Infra, Pool, Agent
    external_main  1->2->3 WorkspaceMaterialize, Infra, Pool
    native_sub     (none)  AgentTemplate.materialize (direct construction)
    external_sub   (none)  AgentTemplate.materialize -> assemble_sub
    ============== ======= ==================================================
"""
from __future__ import annotations

import inspect
from abc import ABC
from pathlib import Path
from typing import Any

import pytest

from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.assembly.builder import AssembledAgent, AssemblyBuilder
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.assembly.pipeline import AssemblyPipeline, AssemblyStage
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


def _make_workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_pipeline_ws")
    return WorkspaceContext(
        target=target,
        paths=WorkspacePaths(root=target),
        is_home=False,
    )


def _make_spec(agent_type: AgentType) -> AssemblySpec:
    """Minimal AssemblySpec — only agent_type drives stage selection."""
    return AssemblySpec(
        agent_type=agent_type,
        agent_name="test_agent",
        pool_name="test_pool",
        tools=[],
        hooks=[],
        llm_provider="test",
        system_prompt_provider="test",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="native",
        workspace_ctx=_make_workspace_ctx(),
    )


class _RecordingStage(AssemblyStage):
    """Stub stage that records its name and optionally registers a cleanup.

    The cleanup callback appends the stage name to ``cleanup_order`` so the
    test can verify that ``builder.cleanup()`` ran (and in what order).
    """

    def __init__(
        self,
        name: str,
        call_order: list[str],
        cleanup_order: list[str] | None = None,
    ) -> None:
        self._name = name
        self._call_order = call_order
        self._cleanup_order = cleanup_order

    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        self._call_order.append(self._name)
        co = self._cleanup_order
        if co is not None:
            async def _cleanup() -> None:
                co.append(self._name)
            builder.register_cleanup(_cleanup)


class _FailingStage(AssemblyStage):
    """Stub stage that records, registers cleanup, then raises."""

    def __init__(
        self,
        name: str,
        call_order: list[str],
        cleanup_order: list[str],
        exc: Exception,
    ) -> None:
        self._name = name
        self._call_order = call_order
        self._cleanup_order = cleanup_order
        self._exc = exc

    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        self._call_order.append(self._name)
        co = self._cleanup_order
        async def _cleanup() -> None:
            co.append(self._name)
        builder.register_cleanup(_cleanup)
        raise self._exc


class _AgentSettingStage(AssemblyStage):
    """Stage that sets ``builder.agent`` to a sentinel for build_agent verification."""

    def __init__(self, name: str, call_order: list[str], sentinel: Any) -> None:
        self._name = name
        self._call_order = call_order
        self._sentinel = sentinel

    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        self._call_order.append(self._name)
        builder.agent = self._sentinel


def _make_pipeline(
    call_order: list[str],
    cleanup_order: list[str] | None = None,
) -> AssemblyPipeline:
    """Build a pipeline with recording stages sharing call/cleanup lists."""
    co = cleanup_order if cleanup_order is not None else []
    return AssemblyPipeline(
        workspace_materialize=_RecordingStage("workspace_materialize", call_order, co),
        infra_assemble=_RecordingStage("infra_assemble", call_order, co),
        pool_assemble=_RecordingStage("pool_assemble", call_order, co),
        agent_assemble=_RecordingStage("agent_assemble", call_order, co),
    )


# ─── AssemblyStage ABC ──────────────────────────────────────────────────────


class TestAssemblyStageABC:
    def test_is_abc(self) -> None:
        assert issubclass(AssemblyStage, ABC)

    def test_cannot_instantiate_without_process(self) -> None:
        with pytest.raises(TypeError):
            AssemblyStage()  # type: ignore[abstract]

    def test_process_is_abstract(self) -> None:
        assert getattr(AssemblyStage.process, "__isabstractmethod__", False) is True

    def test_process_is_async(self) -> None:
        assert inspect.iscoroutinefunction(AssemblyStage.process)

    def test_process_signature(self) -> None:
        sig = inspect.signature(AssemblyStage.process)
        params = list(sig.parameters)
        assert params == ["self", "spec", "builder", "ctx"]

    def test_process_returns_none(self) -> None:
        sig = inspect.signature(AssemblyStage.process)
        # With ``from __future__ import annotations``, return_annotation is
        # the string 'None' (not the None object). Both forms are acceptable.
        assert sig.return_annotation in (None, "None")


# ─── Constructor injection ──────────────────────────────────────────────────


class TestPipelineConstructor:
    def test_constructor_accepts_3_stages(self) -> None:
        call_order: list[str] = []
        pipeline = _make_pipeline(call_order)
        assert pipeline is not None

    def test_no_hardcoded_stage_classes(self) -> None:
        """Constructor takes instances — _stages_for returns the injected
        instances, not some internally-created class."""
        call_order: list[str] = []
        pipeline = _make_pipeline(call_order)
        stages = pipeline._stages_for(AgentType.native_main)
        assert all(isinstance(s, _RecordingStage) for s in stages)

    def test_injected_instances_are_identity_preserving(self) -> None:
        """The exact instances passed to the constructor are the ones returned
        by _stages_for (no copying, no factory creation)."""
        call_order: list[str] = []
        ws = _RecordingStage("ws", call_order)
        infra = _RecordingStage("infra", call_order)
        pool = _RecordingStage("pool", call_order)
        pipeline = AssemblyPipeline(
            workspace_materialize=ws,
            infra_assemble=infra,
            pool_assemble=pool,
            agent_assemble=_RecordingStage("agent", call_order),
        )
        result = pipeline._stages_for(AgentType.native_main)
        assert result[0] is ws
        assert result[1] is infra
        assert result[2] is pool


# ─── _stages_for subset mapping (SPEC §6.3 Errata-5) ────────────────────────


class TestStagesForSubsetMapping:
    """Native main runs four stages; external main runs three."""

    def test_native_main_runs_four_stages_in_order(self) -> None:
        call_order: list[str] = []
        pipeline = _make_pipeline(call_order)
        stages = pipeline._stages_for(AgentType.native_main)
        assert [s._name for s in stages] == [  # type: ignore[attr-defined]
            "workspace_materialize",
            "infra_assemble",
            "pool_assemble",
            "agent_assemble",
        ]

    def test_external_main_runs_three_stages_in_order(self) -> None:
        call_order: list[str] = []
        pipeline = _make_pipeline(call_order)
        stages = pipeline._stages_for(AgentType.external_main)
        assert [s._name for s in stages] == [  # type: ignore[attr-defined]
            "workspace_materialize",
            "infra_assemble",
            "pool_assemble",
        ]

    @pytest.mark.parametrize(
        "agent_type",
        [AgentType.native_sub, AgentType.external_sub],
        ids=["native_sub", "external_sub"],
    )
    def test_sub_types_raise(self, agent_type: AgentType) -> None:
        """Subagents never assemble via the pipeline (Errata-5)."""
        call_order: list[str] = []
        pipeline = _make_pipeline(call_order)
        with pytest.raises(ValueError, match="AgentTemplate.materialize"):
            pipeline._stages_for(agent_type)


# ─── run() is async ─────────────────────────────────────────────────────────


class TestRunIsAsync:
    def test_run_is_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(AssemblyPipeline.run)


# ─── Stage order for each main agent_type ───────────────────────────────────


class TestRunStageOrder:
    """Verify run() executes stages in the correct order for main agent_types."""

    @pytest.mark.parametrize(
        ("agent_type", "expected"),
        [
            (
                AgentType.native_main,
                ["workspace_materialize", "infra_assemble", "pool_assemble", "agent_assemble"],
            ),
            (
                AgentType.external_main,
                ["workspace_materialize", "infra_assemble", "pool_assemble"],
            ),
        ],
        ids=["native_main", "external_main"],
    )
    async def test_stage_order_main_types(
        self, agent_type: AgentType, expected: list[str]
    ) -> None:
        call_order: list[str] = []
        pipeline = _make_pipeline(call_order)
        spec = _make_spec(agent_type)
        await pipeline.run(spec, None)  # type: ignore[arg-type]
        assert call_order == expected

    @pytest.mark.parametrize(
        "agent_type",
        [AgentType.native_sub, AgentType.external_sub],
        ids=["native_sub", "external_sub"],
    )
    async def test_run_sub_types_raises_before_any_stage(
        self, agent_type: AgentType
    ) -> None:
        call_order: list[str] = []
        pipeline = _make_pipeline(call_order)
        spec = _make_spec(agent_type)
        with pytest.raises(ValueError, match="AgentTemplate.materialize"):
            await pipeline.run(spec, None)  # type: ignore[arg-type]
        assert call_order == []


# ─── Cleanup on failure ─────────────────────────────────────────────────────


class TestRunCleanupOnFailure:
    """Exception in a stage -> builder.cleanup() called -> exception re-raised.

    Stages register cleanup callbacks on the real AssemblyBuilder. If
    ``builder.cleanup()`` runs, the callbacks fire (proving cleanup was called).
    The original exception must be re-raised (not swallowed).
    """

    async def test_middle_stage_failure_calls_cleanup_and_reraises(self) -> None:
        """native_main: [WS, Infra, Pool]. Infra (stage 2 of 3) fails.

        WS ran + registered cleanup. Infra ran + registered cleanup, then raised.
        Pool did NOT run. Cleanup runs in reverse: Infra -> WS.
        Original exception re-raised.
        """
        call_order: list[str] = []
        cleanup_order: list[str] = []
        exc = RuntimeError("infra failed")

        pipeline = AssemblyPipeline(
            workspace_materialize=_RecordingStage("workspace_materialize", call_order, cleanup_order),
            infra_assemble=_FailingStage("infra_assemble", call_order, cleanup_order, exc),
            pool_assemble=_RecordingStage("pool_assemble", call_order, cleanup_order),
            agent_assemble=_RecordingStage("agent_assemble", call_order, cleanup_order),
        )
        spec = _make_spec(AgentType.native_main)

        with pytest.raises(RuntimeError, match="infra failed") as exc_info:
            await pipeline.run(spec, None)  # type: ignore[arg-type]

        # (a) original exception re-raised (not swallowed, not wrapped)
        assert exc_info.value is exc

        # (b) cleanup was called — both registered callbacks ran in reverse order
        assert cleanup_order == ["infra_assemble", "workspace_materialize"]

        # stages after the failure did not run
        assert call_order == ["workspace_materialize", "infra_assemble"]

    async def test_first_stage_failure_calls_cleanup_and_reraises(self) -> None:
        """First stage fails — its own cleanup runs, no other stages run."""
        call_order: list[str] = []
        cleanup_order: list[str] = []
        exc = ValueError("ws failed")

        pipeline = AssemblyPipeline(
            workspace_materialize=_FailingStage("workspace_materialize", call_order, cleanup_order, exc),
            infra_assemble=_RecordingStage("infra_assemble", call_order, cleanup_order),
            pool_assemble=_RecordingStage("pool_assemble", call_order, cleanup_order),
            agent_assemble=_RecordingStage("agent_assemble", call_order, cleanup_order),
        )
        spec = _make_spec(AgentType.native_main)

        with pytest.raises(ValueError, match="ws failed") as exc_info:
            await pipeline.run(spec, None)  # type: ignore[arg-type]

        assert exc_info.value is exc
        assert cleanup_order == ["workspace_materialize"]
        assert call_order == ["workspace_materialize"]

    async def test_last_stage_failure_calls_all_cleanups_in_reverse(self) -> None:
        """Last stage (pool_assemble) fails — all 3 cleanups run in reverse."""
        call_order: list[str] = []
        cleanup_order: list[str] = []
        exc = RuntimeError("pool failed")

        pipeline = AssemblyPipeline(
            workspace_materialize=_RecordingStage("workspace_materialize", call_order, cleanup_order),
            infra_assemble=_RecordingStage("infra_assemble", call_order, cleanup_order),
            pool_assemble=_FailingStage("pool_assemble", call_order, cleanup_order, exc),
            agent_assemble=_RecordingStage("agent_assemble", call_order, cleanup_order),
        )
        spec = _make_spec(AgentType.native_main)

        with pytest.raises(RuntimeError, match="pool failed") as exc_info:
            await pipeline.run(spec, None)  # type: ignore[arg-type]

        assert exc_info.value is exc
        assert call_order == [
            "workspace_materialize",
            "infra_assemble",
            "pool_assemble",
        ]
        assert cleanup_order == [
            "pool_assemble",
            "infra_assemble",
            "workspace_materialize",
        ]

    async def test_exception_type_preserved_not_wrapped(self) -> None:
        """Non-RuntimeError exception types are preserved (not wrapped)."""
        call_order: list[str] = []
        cleanup_order: list[str] = []

        class CustomError(Exception):
            pass

        exc = CustomError("custom failure")

        pipeline = AssemblyPipeline(
            workspace_materialize=_FailingStage("workspace_materialize", call_order, cleanup_order, exc),
            infra_assemble=_RecordingStage("infra_assemble", call_order, cleanup_order),
            pool_assemble=_RecordingStage("pool_assemble", call_order, cleanup_order),
            agent_assemble=_RecordingStage("agent_assemble", call_order, cleanup_order),
        )
        spec = _make_spec(AgentType.external_main)

        with pytest.raises(CustomError, match="custom failure") as exc_info:
            await pipeline.run(spec, None)  # type: ignore[arg-type]

        assert exc_info.value is exc
        assert cleanup_order == ["workspace_materialize"]

    async def test_cleanup_called_before_reraise(self) -> None:
        """Cleanup is called BEFORE the exception is re-raised.

        This is verified by the cleanup_order being populated when the
        exception is caught. If cleanup ran after re-raise, the test's
        pytest.raises block would exit before cleanup ran and cleanup_order
        would be empty.
        """
        call_order: list[str] = []
        cleanup_order: list[str] = []
        exc = RuntimeError("fail")

        pipeline = AssemblyPipeline(
            workspace_materialize=_FailingStage("workspace_materialize", call_order, cleanup_order, exc),
            infra_assemble=_RecordingStage("infra_assemble", call_order, cleanup_order),
            pool_assemble=_RecordingStage("pool_assemble", call_order, cleanup_order),
            agent_assemble=_RecordingStage("agent_assemble", call_order, cleanup_order),
        )
        spec = _make_spec(AgentType.native_main)

        with pytest.raises(RuntimeError):
            await pipeline.run(spec, None)  # type: ignore[arg-type]

        # cleanup_order is populated — cleanup ran before the exception propagated
        assert cleanup_order == ["workspace_materialize"]


# ─── Successful run -> build_agent -> AssembledAgent ────────────────────────


class TestRunSuccess:
    """Successful run -> build_agent() called -> AssembledAgent returned."""

    @pytest.mark.parametrize(
        "agent_type",
        [AgentType.native_main, AgentType.external_main],
        ids=["native_main", "external_main"],
    )
    async def test_successful_run_returns_assembled_agent(
        self, agent_type: AgentType
    ) -> None:
        call_order: list[str] = []
        sentinel = object()
        pipeline = AssemblyPipeline(
            workspace_materialize=_RecordingStage("workspace_materialize", call_order),
            infra_assemble=_RecordingStage("infra_assemble", call_order),
            pool_assemble=_AgentSettingStage("pool_assemble", call_order, sentinel),
            agent_assemble=_AgentSettingStage("agent_assemble", call_order, sentinel),
        )
        spec = _make_spec(agent_type)

        result = await pipeline.run(spec, None)  # type: ignore[arg-type]

        assert isinstance(result, AssembledAgent)
        expected = ["workspace_materialize", "infra_assemble", "pool_assemble"]
        if agent_type is AgentType.native_main:
            expected.append("agent_assemble")
        assert call_order == expected
        # sentinel set by pool_assemble is carried through build_agent
        assert result.agent is sentinel

    async def test_successful_run_no_cleanup_called(self) -> None:
        """On success, builder.cleanup() is NOT called — resources stay alive."""
        call_order: list[str] = []
        cleanup_order: list[str] = []
        pipeline = _make_pipeline(call_order, cleanup_order)
        spec = _make_spec(AgentType.native_main)

        await pipeline.run(spec, None)  # type: ignore[arg-type]

        # All stages ran and registered cleanups, but cleanup was NOT called
        assert call_order == [
            "workspace_materialize",
            "infra_assemble",
            "pool_assemble",
            "agent_assemble",
        ]
        assert cleanup_order == []
