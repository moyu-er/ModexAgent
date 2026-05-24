"""Bot service entry point.

QQ Bot specialization of the generic BotService.
Run with: python bot_service.py

Uses IOC AppConfig.from_yaml() for config loading.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Literal

# Path setup
framework_dir = Path(__file__).parent.parent.parent
if str(framework_dir) not in sys.path:
    sys.path.insert(0, str(framework_dir))

from bot.logging import setup_logging  # noqa: E402

setup_logging()

from bot.adapters.qq import (  # noqa: E402
    QQBotEmitter,
    QQEmitterConfig,
    QQInputAdapter,
    QQOutputAdapter,
)
from bot.service import BotService  # noqa: E402
from bot.utils.config_loader import ConfigLoader  # noqa: E402

from framework.ioc.configs.app import AppConfig  # noqa: E402
from framework.pipeline.adapters import SessionPrefixStripAdapter  # noqa: E402


class QQBotService(BotService):
    """QQ Bot service — IOC AppConfig drives all initialization."""

    def __init__(self, config_dir: Path, mode: Literal["pipeline", "pool"] = "pipeline") -> None:
        yaml_path = config_dir / "bot_config.yml"

        # Load .env BEFORE AppConfig.from_yaml() so ${LLM_API_KEY} resolves
        from dotenv import load_dotenv
        load_dotenv(config_dir.parent / ".env")

        # IOC config — primary config source
        app_cfg = AppConfig.from_yaml(yaml_path)
        print(f"[IOC] Loaded: {len(app_cfg.agents)} agents, "
              f"MCP={app_cfg.mcp is not None}")

        # QQ adapter config (business layer)
        config_loader = ConfigLoader(config_dir)
        raw_config = config_loader.load_yaml("bot_config.yml")
        qq_cfg = raw_config.get("qq", {})

        input_adapter = QQInputAdapter(
            app_id=qq_cfg["app_id"],
            secret=qq_cfg["secret"],
            sandbox=qq_cfg.get("sandbox", False),
            allow_from=qq_cfg.get("allow_from", ["*"]),
            media_dir=qq_cfg.get("media_dir"),
        )
        qq_output_adapter = QQOutputAdapter(input_adapter)
        output_adapter = SessionPrefixStripAdapter(qq_output_adapter)

        def emitter_factory(session_id: str) -> QQBotEmitter:
            return QQBotEmitter(
                output_adapter=qq_output_adapter,
                session_id=session_id,
                config=QQEmitterConfig.minimal(),
            )

        # Pass app_config only — MCP servers come from sibling mcp.json
        super().__init__(
            config_dir, input_adapter, output_adapter, emitter_factory,
            mode=mode, app_config=app_cfg,
        )


def create_qq_service(
    config_dir: Path, mode: Literal["pipeline", "pool"] = "pipeline"
) -> QQBotService:
    """Create a QQ Bot service instance."""
    return QQBotService(config_dir, mode=mode)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the bot service."""
    parser = argparse.ArgumentParser(description="Run the QQ Bot example service.")
    parser.add_argument(
        "--mode",
        choices=("pipeline", "pool"),
        default="pool",
        help="Runtime mode: pipeline for single-agent mode, pool for AgentPool mode.",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    args = parse_args(argv)
    config_dir = Path(__file__).parent / "config"
    service = create_qq_service(config_dir, mode=args.mode)

    loop = asyncio.get_running_loop()
    import signal as signal_mod
    try:
        for _sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
            loop.add_signal_handler(_sig, service._shutdown_event.set)
    except NotImplementedError:
        pass

    try:
        await service.initialize()
        await service.start()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
