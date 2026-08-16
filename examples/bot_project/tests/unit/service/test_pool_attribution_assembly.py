from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.workspace.handle import WorkspaceResolverCell

from modex_agent.core.emitter import StreamingAwareEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.tools.presets import ToolPreset


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

    pool_spec = PoolSpec(
        name=pool_name,
        main_agent_name="main",
        main=MainAgentSpec(agent_name="main", tool_preset=ToolPreset.NONE),
    )
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
                pool_spec=pool_spec,
                assembly_deps=PoolAssemblyDeps(),
                project_dir=tmp_path,
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
