from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parents[3]))

from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider
from bot.service.pool_builder import _build_llm_provider, _wire_main_pipeline

from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook.runner import HookRunner
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.pipeline.pipeline import AgentPipeline

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
    pool_cfg = PoolConfig(name="main", main_agent_name="main", llm=LLMConfig(), agents=[])
    prov = _build_llm_provider(pool_cfg, "main", cfg)
    assert isinstance(prov, BotModelProvider)


def test_wire_main_pipeline_adds_model_choice_hook(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    reg = ModelChoiceRegistry()
    main_cfg = AgentConfig(
        name="main", role="main", llm=LLMConfig(), approval=ApprovalConfig(enabled=False)
    )
    pool_cfg = PoolConfig(name="main", main_agent_name="main", llm=LLMConfig(), agents=[main_cfg], memory=MemoryConfig())

    pipeline = AgentPipeline(
        agent=_Agent(),
        context_manager=MagicMock(),
        tool_manager=InMemoryToolManager(),
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        sanitizer=None,
        hook_runner=HookRunner(),
    )
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
        pool_cfg=pool_cfg,
        project_dir=Path("/proj"),
        command_processor=None,
        pool_name="main",
        tool_manager=InMemoryToolManager(),
        bot_model_config=cfg,
        model_choice_registry=reg,
    )
    assert pipeline.hook_runner is not None
    hooks = [spec.hook for spec in pipeline.hook_runner.hook_specs]
    assert any(isinstance(h, ModelChoiceBindHook) for h in hooks)
