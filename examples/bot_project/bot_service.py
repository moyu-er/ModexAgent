"""Bot service entry point.

QQ Bot specialization of the generic BotService.
Run with: python bot_service.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal

# --------------------------------------------------------------------------- #
# Path setup (must happen before importing framework)
# --------------------------------------------------------------------------- #

framework_dir = Path(__file__).parent.parent.parent
if str(framework_dir) not in sys.path:
    sys.path.insert(0, str(framework_dir))

# --------------------------------------------------------------------------- #
# Logging bootstrap (no import-time side effects)
# --------------------------------------------------------------------------- #
from bot.logging import setup_logging  # noqa: E402

setup_logging()

# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #
from bot.adapters.qq import (  # noqa: E402
    QQBotEmitter,
    QQEmitterConfig,
    QQInputAdapter,
    QQOutputAdapter,
)
from bot.service import BotService  # noqa: E402
from bot.utils.config_loader import ConfigLoader  # noqa: E402

from framework.pipeline.adapters import SessionPrefixStripAdapter  # noqa: E402


class QQBotService(BotService):
    """QQ Bot service -- specialization of the generic BotService.

    Defaults to pipeline mode; pass mode="pool" for multi-agent collaboration.
    """

    def __init__(self, config_dir: Path, mode: Literal["pipeline", "pool"] = "pipeline"):
        config_loader = ConfigLoader(config_dir)
        config = config_loader.load_yaml("bot_config.yml")
        mcp_config = config_loader.load_mcp_config(config.get("mcp", {}))
        config["mcp"] = mcp_config

        qq_config = config.get("qq", {})

        media_dir = qq_config.get("media_dir")
        input_adapter = QQInputAdapter(
            app_id=qq_config["app_id"],
            secret=qq_config["secret"],
            sandbox=qq_config["sandbox"],
            allow_from=qq_config["allow_from"],
            media_dir=media_dir,
        )
        qq_output_adapter = QQOutputAdapter(input_adapter)
        output_adapter = SessionPrefixStripAdapter(qq_output_adapter)

        def emitter_factory(session_id: str) -> QQBotEmitter:
            return QQBotEmitter(
                output_adapter=qq_output_adapter,
                session_id=session_id,
                config=QQEmitterConfig.minimal(),
            )

        super().__init__(config_dir, input_adapter, output_adapter, emitter_factory, mode=mode, config=config)


def create_qq_service(
    config_dir: Path, mode: Literal["pipeline", "pool"] = "pipeline"
) -> QQBotService:
    """Create a QQ Bot service instance."""
    return QQBotService(config_dir, mode=mode)


async def main() -> None:
    """Main entry point."""
    config_dir = Path(__file__).parent / "config"
    service = create_qq_service(config_dir, mode="pool")
    await service.initialize()
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
