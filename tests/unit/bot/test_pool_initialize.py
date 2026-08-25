"""Test pool-mode initialization with pools declared in config/scopes (no llm in top-level)."""
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
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter


class _StubInput(InputAdapter):
    name = "stub"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        if False:
            yield InputMessage(content="", session=SessionInfo.from_str(""))

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
    """Create a minimal pool-mode config directory with a scope declaration
    but no top-level llm."""
    tmp = Path(tempfile.mkdtemp(prefix="bot_test_"))

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

    # Scope declaration (config/scopes/bot.yml) — the single pool source
    # since the legacy config/pools format was deleted (ticket 11).
    declaration = """
workspace:
  name: bot
  pools:
    default:
      agents:
        default:
          description: test default agent
          max_steps: 5
"""
    scopes_dir = tmp / "config" / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    (scopes_dir / "bot.yml").write_text(declaration, encoding="utf-8")

    yield tmp

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class TestPoolModeInitializeNoTopLevelLlm:
    """Verify pool-mode initialization works when bot_config.yml has no llm section."""

    def test_declaration_loads_pools_without_top_level_llm(
        self, pool_mode_config_dir: Path
    ) -> None:
        """The scope declaration lists the pools; AppConfig reads no pools."""
        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))
        assert "pools" not in cfg.model_fields

        from bot.config.scope_pools import declared_pool_names, list_pool_summaries

        declaration_path = pool_mode_config_dir / "config" / "scopes" / "bot.yml"
        assert declared_pool_names(declaration_path) == {"default"}
        summaries = list_pool_summaries(declaration_path)
        assert [summary.name for summary in summaries] == ["default"]
        assert summaries[0].root_agent_name == "default"
        assert summaries[0].subagent_count == 0

    def test_bot_service_constructor_pool_mode_no_crash(
        self, pool_mode_config_dir: Path
    ) -> None:
        """BotService construction in pool mode doesn't crash."""
        from bot.service._model_config_loader import (
            _apply_bot_model_config,
            _load_app_config,
        )
        from bot.service.core import BotService

        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
        )
        bot._app_config = _load_app_config(bot.config_dir)
        bot._bot_model_config = _apply_bot_model_config(bot.config_dir, bot._app_config)
        assert bot._app_config is not None
        assert "pools" not in bot._app_config.model_fields

    def test_pool_mode_llm_from_model_yml_not_pool(
        self, pool_mode_config_dir: Path,
    ) -> None:
        """In pool mode, LLM config comes from model.yml, not the declaration."""
        from bot.service.core import BotService

        cfg = AppConfig.from_yaml(str(pool_mode_config_dir / "bot_config.yml"))
        bot = BotService(
            config_dir=pool_mode_config_dir,
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=cfg,
        )

        from bot.config.scope_pools import list_pool_summaries

        summaries = list_pool_summaries(
            pool_mode_config_dir / "config" / "scopes" / "bot.yml"
        )
        assert [summary.name for summary in summaries] == ["default"]
        assert summaries[0].root_agent_name == "default"
        assert bot._bot_model_config is not None
        default = bot._bot_model_config.default_resolved()
        assert default.model.model == "test-model"
