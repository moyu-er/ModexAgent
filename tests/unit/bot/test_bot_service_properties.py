"""Test BotService _main_agent_cfg and _main_memory_cfg properties."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

import pytest

# bot_project is not on the default Python path
_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from framework.core.session_id import SessionInfo
from framework.core.types import InputMessage, OutputMessage
from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.memory import MemoryConfig
from framework.pipeline.adapters import InputAdapter, OutputAdapter


class _StubInput(InputAdapter):
    name = "stub"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        if False:
            yield InputMessage(content="", session=SessionInfo.from_str("", default_agent_name="main"))

    async def send_reply(self, msg: OutputMessage, session_id: str) -> None:
        pass


class _StubOutput(OutputAdapter):
    name = "stub"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutputMessage, session_id: str) -> None:
        pass

    async def send_streaming(self, stream, session_id: str) -> None:
        pass


@pytest.fixture
def agent_config() -> AppConfig:
    return AppConfig(
        llm=LLMConfig(model="test-model", api_key="k"),
        agents=[
            AgentConfig(name="main", role="main", max_steps=30),
            AgentConfig(name="helper", role="subagent"),
        ],
    )


@pytest.fixture
def pool_config() -> AppConfig:
    return AppConfig(
        multi_agent=AppConfig.model_fields["multi_agent"].default,
        pools={},
    )


class TestMainAgentCfgProperty:
    def test_returns_main_agent_by_role(self, agent_config: AppConfig) -> None:
        """_main_agent_cfg finds the agent where role='main'."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=agent_config,
        )
        cfg = bot._main_agent_cfg
        assert cfg is not None
        assert cfg.name == "main"
        assert cfg.role == "main"

    def test_returns_none_when_no_agents(self) -> None:
        """_main_agent_cfg returns None when agents list is empty."""
        from bot.service.core import BotService

        cfg = AppConfig(
            llm=LLMConfig(model="test-model", api_key="k"),
        )
        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=cfg,
        )
        assert bot._main_agent_cfg is None

    def test_returns_none_when_no_app_config(self) -> None:
        """_main_agent_cfg returns None when _app_config is None."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
        )
        assert bot._main_agent_cfg is None

    def test_pool_config_returns_none(self, pool_config: AppConfig) -> None:
        """Pool configs have no top-level agents, so _main_agent_cfg returns None."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=pool_config,
        )
        assert bot._main_agent_cfg is None


class TestMainMemoryCfgProperty:
    def test_returns_agent_memory(self, agent_config: AppConfig) -> None:
        """_main_memory_cfg returns the main agent's memory config."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=agent_config,
        )
        mem = bot._main_memory_cfg
        # Agent has no explicit memory, so returns None
        assert mem is None

    def test_returns_none_when_no_main_agent(self) -> None:
        """_main_memory_cfg returns None when there's no main agent."""
        from bot.service.core import BotService

        cfg = AppConfig(
            llm=LLMConfig(model="test-model", api_key="k"),
        )
        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=cfg,
        )
        assert bot._main_memory_cfg is None
