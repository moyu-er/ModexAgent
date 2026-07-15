"""Unit tests for pool_builder external_coding dispatch and availability gate.

These tests exercise the bot-layer wiring without booting the whole service or
spawning a real external-coding CLI.  They verify:

* ``execution_strategy == "external_coding"`` causes the pool builder to
  construct an :class:`ExternalCodingAgent` when the provider executable is on
  PATH.
* A missing provider causes the main agent to be skipped with a warning, leaving
  the pool structurally intact so other pools are unaffected.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from modex_agent.agents.external_coding.agent import ExternalCodingAgent
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.multi_agent.pool_instance import PoolInstance

_BOT_PROJECT = Path(__file__).parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))


async def _build_external_pool(
    tmp_path: Path, *, provider_kind: str = "pi", which_result: str | None
) -> PoolInstance:
    """Call ``create_pool`` for an external_coding pool with ``shutil.which`` mocked."""
    from bot.service.model_choice import ModelChoiceRegistry
    from bot.service.model_config import BotModelConfig
    from bot.service.pool_builder import create_pool
    from bot.workspace.handle import WorkspaceHandle

    target = tmp_path / "ws"
    target.mkdir()

    yml = """\
models:
  default_provider: "Test"
  default_model: "test-model"
  providers:
    - key: test
      name: "Test"
      url: "http://localhost"
      api_key: "test-key"
      models:
        - name: "test-model"
          model: "test-model"
"""
    (tmp_path / "model.yml").write_text(yml, encoding="utf-8")
    bot_model_config = BotModelConfig.from_yaml(tmp_path / "model.yml")

    pool_spec = PoolSpec(
        name="ext_pool",
        main_agent_name="ext",
        main=MainAgentSpec(
            agent_name="ext",
            execution_strategy="external_coding",
            provider_kind=provider_kind,
        ),
    )
    assembly_deps = PoolAssemblyDeps(memory=MemoryConfig())
    broker = InMemoryMessageBroker()
    await broker.start()
    workspace_handle = WorkspaceHandle(target=target, data_root=target / ".modex")

    with patch("bot.service.pool_builder.shutil.which", return_value=which_result):
        pool_instance = await create_pool(
            pool_name="ext_pool",
            pool_spec=pool_spec,
            assembly_deps=assembly_deps,
            project_dir=tmp_path,
            data_dir=target / ".modex",
            broker=broker,
            output_adapter=object(),  # type: ignore[arg-type]
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=object(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            control_channel=InMemoryControlChannel(),
            workspace_handle=workspace_handle,
            bot_model_config=bot_model_config,
            model_choice_registry=ModelChoiceRegistry(),
        )

    await broker.stop()
    return pool_instance


@pytest.mark.asyncio
async def test_external_coding_pool_builds_agent_when_provider_available(
    tmp_path: Path,
) -> None:
    """When the provider executable is found, the main agent is an ExternalCodingAgent."""
    pool_instance = await _build_external_pool(tmp_path, which_result="/usr/bin/pi")

    assert "ext" in pool_instance.pool._agents
    agent = pool_instance.pool._agents["ext"].pipeline.agent
    assert isinstance(agent, ExternalCodingAgent)
    assert agent._provider_kind.value == "pi"


@pytest.mark.asyncio
async def test_external_coding_pool_skips_agent_when_provider_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing provider skips main-agent registration and logs a warning."""
    with caplog.at_level("WARNING"):
        pool_instance = await _build_external_pool(tmp_path, which_result=None)

    assert "ext" not in pool_instance.pool._agents
    assert any(
        "external_coding provider 'pi' not found on PATH" in r.message
        for r in caplog.records
    )
    assert any(
        "main agent registration skipped" in r.message for r in caplog.records
    )
