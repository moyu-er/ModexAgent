"""QQ Bot service — IOC AppConfig driven specialization of BotService."""

from __future__ import annotations

import logging
from pathlib import Path

from bot.adapters.qq import (
    QQBotEmitter,
    QQEmitterConfig,
    QQInputAdapter,
    QQOutputAdapter,
)
from bot.service.core import BotService
from bot.utils.config_loader import ConfigLoader
from modex_agent.ioc.configs.app import AppConfig

logger = logging.getLogger(__name__)


class QQBotService(BotService):
    """QQ Bot service — IOC AppConfig drives all initialization."""

    def __init__(self, config_dir: Path) -> None:
        yaml_path = config_dir / "bot_config.yml"

        # Load .env BEFORE AppConfig.from_yaml() so ${QQ_SECRET} etc. resolve
        # (model settings live in config/model.yml, not in .env)
        from dotenv import load_dotenv

        load_dotenv(config_dir.parent / ".env")

        # IOC config — primary config source
        app_cfg = AppConfig.from_yaml(yaml_path)

        # QQ adapter config (business layer)
        config_loader = ConfigLoader(config_dir)
        raw_config = config_loader.load_yaml("bot_config.yml")
        qq_cfg = raw_config.get("qq", {})

        input_adapter = QQInputAdapter(
            app_id=qq_cfg["app_id"],
            secret=qq_cfg["secret"],
            allow_from=qq_cfg.get("allow_from", ["*"]),
        )
        qq_output_adapter = QQOutputAdapter(input_adapter)
        output_adapter = qq_output_adapter

        def emitter_factory(session_id: str) -> QQBotEmitter:
            return QQBotEmitter(
                output_adapter=qq_output_adapter,
                session_id=session_id,
                config=QQEmitterConfig.minimal(),
            )

        super().__init__(
            config_dir,
            input_adapter,
            output_adapter,
            emitter_factory,
            app_config=app_cfg,
        )


def create_qq_service(config_dir: Path) -> QQBotService:
    """Create a QQ Bot service instance."""
    return QQBotService(config_dir)
