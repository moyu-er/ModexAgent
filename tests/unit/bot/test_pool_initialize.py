"""Test pool-mode initialization with pool.yml loaded via PoolStore (no llm in top-level)."""
from __future__ import annotations

import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_config import PoolStore
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

    # bot_config.yml — shared infra only, NO llm section, NO pools section
    bot_config = """
multi_agent:
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
    # block). Minimal valid block matching the pool's test-model below.
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

    # Pool config. Dir-based layout (config/pools/<name>/pool.yml) — PoolStore reads this.
    pool_config = """
name: default
main_agent_name: default
agents:
  - name: default
    role: main
    max_steps: 5
"""
    pools_dir = tmp / "config" / "pools"
    pools_dir.mkdir(parents=True, exist_ok=True)
    (pools_dir / "default").mkdir(parents=True, exist_ok=True)
    (pools_dir / "default" / "pool.yml").write_text(pool_config, encoding="utf-8")

    yield tmp

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class TestPoolModeInitializeNoTopLevelLlm:
    """Verify pool-mode initialization works when bot_config.yml has no llm section."""

    def test_pool_store_loads_pools_without_top_level_llm(
        self, pool_mode_config_dir: Path
    ) -> None:
        """PoolStore loads pool.yml correctly; AppConfig no longer reads pools."""
        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))
        assert "pools" not in cfg.model_fields

        pool_store = PoolStore(base_dir=pool_mode_config_dir)
        pool_names = {s.name for s in pool_store.list_pools()}
        assert "default" in pool_names
        spec = pool_store.read_pool("default")
        assert spec.main.agent_name == "default"

    def test_bot_service_constructor_pool_mode_no_crash(
        self, pool_mode_config_dir: Path
    ) -> None:
        """BotService construction in pool mode doesn't crash."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
        )
        bot._app_config = bot._load_app_config()
        assert bot._app_config is not None
        assert "pools" not in bot._app_config.model_fields

    def test_pool_mode_llm_from_model_yml_not_pool(
        self, pool_mode_config_dir: Path,
    ) -> None:
        """In pool mode, LLM config comes from model.yml, not pool.yml."""
        from bot.service.core import BotService

        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))
        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=cfg,
        )

        pool_store = PoolStore(base_dir=pool_mode_config_dir)
        pool_names = {s.name for s in pool_store.list_pools()}
        assert "default" in pool_names
        spec = pool_store.read_pool("default")
        assert spec.main.agent_name == "default"
        assert bot._bot_model_config is not None
        default = bot._bot_model_config.default_resolved()
        assert default.model.model == "test-model"
