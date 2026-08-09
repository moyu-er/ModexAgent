from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider
from bot.service.pool.pipeline_wiring import _wire_main_pipeline
from bot.service.react_strategy import ReactExecutionStrategy

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook.runner import HookRunner
from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

pytestmark = pytest.mark.skipif(
    shutil.which("modexctl") is None,
    reason="modexctl CLI not available",
)

_YML = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: openai/m1}]}
"""


def _cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(_YML, encoding="utf-8")
    return BotModelConfig.from_yaml(p)


class _Agent:
    name = "main"

    async def run(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401  test stub matches Agent ABC loosely
        ...


def test_build_llm_provider_returns_bot_model_provider(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    strategy = ReactExecutionStrategy()
    prov = strategy._build_llm_provider("main", cfg)
    assert isinstance(prov, BotModelProvider)


def test_wire_main_pipeline_adds_model_choice_hook(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry()
    main_spec = MainAgentSpec(agent_name="main", approval=ApprovalConfig(enabled=False))
    assembly_deps = PoolAssemblyDeps(memory=MemoryConfig())

    agent = _Agent()
    registry = TurnSessionRegistry()
    hook_runner = HookRunner()
    builder = TurnContextBuilder(
        agent=agent,
        tool_manager=InMemoryToolManager(),
        sanitizer=None,
        command_processor=None,
        skill_manager=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=10,
        safety=RuntimeSafetyPolicy(),
        runtime_services=None,
        runtime_context_manager=None,
        governance=None,
        hook_runner=hook_runner,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=None,
        output_adapter=MagicMock(),
        turn_store=None,
        registry=registry,
    )
    approval = ApprovalRenderer(agent=agent, user_interface=None)  # type: ignore[arg-type]
    resumer = ApprovalResumer(agent=agent, turn_store=None, user_interface=None)
    turn_runner = ReActTurnRunner(
        agent=agent,
        context_manager=MagicMock(),
        context_manager_factory=None,
        on_session_start=None,
        on_session_end=None,
        safety=RuntimeSafetyPolicy(),
        turn_store=None,
        registry=registry,
        builder=builder,
        resumer=resumer,
        approval=approval,
        workspace_manager=None,
        pool_name=None,
        pool_data_resolver=None,
        agent_descriptor=None,
    )
    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=turn_runner,
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        registry=registry,
    )
    turn_runner.bind_to_pipeline(pipeline)
    main_inst = MagicMock()
    main_inst.pipeline = pipeline
    pool = MagicMock()
    pool._agents = {"main": main_inst}

    _wire_main_pipeline(
        pool=pool,
        main_agent_name="main",
        inbox_consumer=MagicMock(),
        notification_service=MagicMock(),
        shared_interceptor_chain=MagicMock(),
        im_ui=MagicMock(),
        main_spec=main_spec,
        assembly_deps=assembly_deps,
        project_dir=Path("/proj"),
        command_processor=None,
        pool_name="main",
        tool_manager=InMemoryToolManager(),
        pool_spec=PoolSpec(name="main", main_agent_name="main", main=main_spec),
        bot_model_config=cfg,
        model_choice_registry=reg,
    )
    assert pipeline.hook_runner is not None
    hooks = [spec.hook for spec in pipeline.hook_runner.hook_specs]
    assert any(isinstance(h, ModelChoiceBindHook) for h in hooks)
