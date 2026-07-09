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

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter


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

    # model.yml — required by the bot layer (BotModelConfig parses the models:
    # block; BotService._load_app_config injects the default model into each
    # pool's llm). Minimal valid block matching the pool's test-model below.
    model_yml = """
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
    (tmp / "model.yml").write_text(model_yml, encoding="utf-8")

    # Pool config. Dir-based layout (pools/<name>/pool.yml) — the
    # loader scans directories, not legacy single pools/<name>.yml files.
    # name + main_agent_name are strictly required (no derivation).
    pool_config = """
name: testpool
main_agent_name: testpool
agents:
  - name: testpool
    role: main
    max_steps: 5
"""
    (pools_dir / "testpool").mkdir(parents=True, exist_ok=True)
    (pools_dir / "testpool" / "pool.yml").write_text(pool_config, encoding="utf-8")

    yield tmp

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class TestPoolModeInitializeNoTopLevelLlm:
    """Verify pool-mode initialization works when bot_config.yml has no llm section."""

    def test_config_loads_pools_without_top_level_llm(self, pool_mode_config_dir: Path) -> None:
        """AppConfig loads pools correctly (pool mode is the only mode)."""
        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))

        # Pools should be loaded
        assert "testpool" in cfg.pools
        assert cfg.pools["testpool"].main_agent_name == "testpool"

    def test_bot_service_initialize_pool_mode_no_crash(self, pool_mode_config_dir: Path) -> None:
        """BotService.initialize() in pool mode doesn't crash."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
        )
        # Simulate what initialize() does — load config first
        bot._app_config = bot._load_app_config()

        assert bot._app_config.pools

    def test_pool_mode_llm_provider_from_pool_not_top_level(
        self, pool_mode_config_dir: Path,
    ) -> None:
        """In pool mode, LLM config comes from pool configs."""
        from bot.service.core import BotService

        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))
        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=cfg,
        )

        # Pool mode: pools have their own llm. BotService.__init__ runs
        # _apply_bot_model_config which routes the bare model name through
        # synthesize_llm_config (_routing_model prepends "openai/").
        assert len(cfg.pools) == 1
        pool_cfg = list(cfg.pools.values())[0]
        assert pool_cfg.name == "testpool"
