from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool

from ...declaration_driver import build_declared

_POOL_DECLARATION = """\
pool:
  name: {pool_name}
  agents:
    main:
      description: attribution test root
      toolset: none
"""
from bot.workspace.handle import WorkspaceResolverCell

from modex_agent.core.emitter import StreamingAwareEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage


async def test_create_pool_binds_pool_at_single_assembly_point(tmp_path: Path) -> None:
    pool_name = "attributed-pool"
    emitter_pools: list[str] = []
    created_subagents: list[tuple[str, str, str]] = []
    output_adapter = MagicMock(spec=OutputAdapter)

    def emitter_factory(session_id: str, pool: str) -> StreamingAwareEmitter:
        emitter_pools.append(pool)
        return StreamingAwareEmitter(output_adapter, session_id)

    async def on_subagent_created(child_id: str, parent_id: str, pool: str) -> None:
        created_subagents.append((child_id, parent_id, pool))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()

    pool_instance = None
    try:
        with patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}):
            pool_instance = await create_pool(
                pool_name=pool_name,
                declared=build_declared(
                    _POOL_DECLARATION.format(pool_name=pool_name),
                    project_dir=tmp_path,
                    data_dir=tmp_path / ".modex",
                    pool_name=pool_name,
                ),
                assembly_deps=PoolAssemblyDeps(),
                project_dir=tmp_path,
                workspace_registry=object(),
                workspace_resources=object(),
                data_dir=tmp_path / ".modex",
                broker=broker,
                output_adapter=output_adapter,
                safety=RuntimeSafetyPolicy(),
                retention=SessionRetentionPolicy(),
                im_ui=MagicMock(),
                shared_hooks=[],
                shared_hook_runner=HookRunner(),
                shared_interceptor_chain=InterceptorChain(),
                workspace_resolver=WorkspaceResolverCell(),
                emitter_factory=emitter_factory,
                on_subagent_created=on_subagent_created,
                bot_model_config=None,
                model_choice_registry=ModelChoiceRegistry(),
            )

        materialize_deps = pool_instance.pool.materialize_deps
        assert materialize_deps is not None
        assert materialize_deps.emitter_factory is not None
        assert materialize_deps.on_subagent_created is not None

        materialize_deps.emitter_factory("conversation.main")
        await materialize_deps.on_subagent_created("child", "parent")

        assert emitter_pools == [pool_name]
        assert created_subagents == [("child", "parent", pool_name)]
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()


async def test_create_pool_raises_when_stage3_strategy_result_missing(
    tmp_path: Path,
) -> None:
    """Stage-4 defense-in-depth: ``build_native_inputs`` raises RuntimeError
    when the Stage-3 strategy result is absent — an agent is never silently
    assembled without its strategy products. Stage 3's process is skipped to
    simulate the missing result (a None-returning strategy would crash inside
    PoolAssembleStage first, before the guard could fire)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()

    async def _skip_stage(*args: object) -> None:
        return None

    try:
        with (
            patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}),
            patch.object(PoolAssembleStage, "process", _skip_stage),pytest.raises(
            RuntimeError,
            match="Native Stage4 requires the Stage3 strategy result",
        )
        ):
            await create_pool(
                pool_name="stage3-missing",
                declared=build_declared(
                    _POOL_DECLARATION.format(pool_name="stage3-missing"),
                    project_dir=tmp_path,
                    data_dir=tmp_path / ".modex",
                    pool_name="stage3-missing",
                ),
                assembly_deps=PoolAssemblyDeps(),
                project_dir=tmp_path,
                workspace_registry=object(),
                workspace_resources=object(),
                data_dir=tmp_path / ".modex",
                broker=broker,
                output_adapter=MagicMock(spec=OutputAdapter),
                safety=RuntimeSafetyPolicy(),
                retention=SessionRetentionPolicy(),
                im_ui=MagicMock(),
                shared_hooks=[],
                shared_hook_runner=HookRunner(),
                shared_interceptor_chain=InterceptorChain(),
                workspace_resolver=WorkspaceResolverCell(),
                bot_model_config=None,
                model_choice_registry=ModelChoiceRegistry(),
            )
    finally:
        await broker.stop()
