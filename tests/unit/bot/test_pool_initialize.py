"""Test pool-mode initialization with pools-loaded config (no llm in top-level)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterator

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from framework.core.session_id import SessionInfo
from framework.core.types import InputMessage, OutputMessage
from framework.ioc.configs.app import AppConfig
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
def pool_mode_config_dir() -> Path:
    """Create a minimal pool-mode config directory with pools but no top-level llm."""
    tmp = Path(tempfile.mkdtemp(prefix="bot_test_"))
    pools_dir = tmp / "pools"
    pools_dir.mkdir(parents=True, exist_ok=True)

    # bot_config.yml — shared infra only, NO llm section
    bot_config = """
multi_agent:
  default_pool: "testpool"
  session_retention:
    max_sessions_per_subagent: 5
    max_sessions_global: 50
    ttl_seconds: 3600
    cleanup_interval_seconds: 600
paths:
  data_dir: "data"
"""
    (tmp / "bot_config.yml").write_text(bot_config, encoding="utf-8")

    # Pool config with llm
    pool_config = """
llm:
  model: "test-model"
  api_key: "test-key"
  temperature: 0.5
  max_tokens: 1000
agents:
  - name: testpool
    role: main
    max_steps: 5
"""
    (pools_dir / "testpool.yml").write_text(pool_config, encoding="utf-8")

    yield tmp

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class TestPoolModeInitializeNoTopLevelLlm:
    """Verify pool-mode initialization works when bot_config.yml has no llm section."""

    def test_config_loads_pools_without_top_level_llm(self, pool_mode_config_dir: Path) -> None:
        """AppConfig loads pools correctly even without top-level llm."""
        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))

        # Top-level llm should be None (not in bot_config.yml)
        assert cfg.llm is None

        # Pools should be loaded
        assert "testpool" in cfg.pools
        assert cfg.pools["testpool"].llm.model == "test-model"
        assert cfg.pools["testpool"].main_agent_name == "testpool"

    def test_bot_service_initialize_pool_mode_no_crash(self, pool_mode_config_dir: Path) -> None:
        """BotService.initialize() in pool mode doesn't crash on missing top-level llm."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
        )
        # Simulate what initialize() does — load config first
        bot._app_config = bot._load_app_config()

        # Top-level llm should be None (not in bot_config.yml)
        assert bot._app_config.llm is None
        assert bot._app_config.pools

        # _main_agent_cfg should safely return None (no agents in top-level)
        assert bot._main_agent_cfg is None

        # _main_memory_cfg should safely return None
        assert bot._main_memory_cfg is None

    def test_pool_mode_llm_provider_from_pool_not_top_level(
        self, pool_mode_config_dir: Path,
    ) -> None:
        """In pool mode, LLM config comes from pool configs, not top-level."""
        from bot.service.core import BotService

        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))
        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=cfg,
        )

        # Pool mode: top-level llm is None, pools have their own
        assert cfg.llm is None
        assert len(cfg.pools) == 1
        pool_cfg = list(cfg.pools.values())[0]
        assert pool_cfg.llm.model == "test-model"

        # BotService properties adapt: no crash
        assert bot._main_agent_cfg is None
        assert bot._main_memory_cfg is None
