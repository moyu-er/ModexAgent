"""Service orchestration — service factories and CLI entry points.

The process-level supervisor (crash-restart / stable-period / signals)
lives in the framework (:func:`modex_agent.app.supervisor.run_with_supervisor`);
this module keeps the bot's service factories and argument parsing.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.service.core import BotService

logger = logging.getLogger(__name__)


# ── Factory helpers ──────────────────────────────────────────────────────────


def create_qq_service(config_dir: Path) -> BotService:
    """Create a QQ Bot service instance."""
    from bot.logging import setup_logging

    setup_logging()

    from bot.service.qq_service import QQBotService

    return QQBotService(config_dir)


def create_webui_service(
    config_dir: Path, *, port: int | None = None, static_dist: Path | None = None
) -> BotService:
    """Create a WebUI Bot service instance."""
    from bot.logging import setup_logging

    setup_logging()

    from bot.config.webui_config import load_webui_port
    from bot.service.web_ui_service import WebUIService

    if port is None:
        port = load_webui_port(config_dir)
    static_dist = static_dist or _detect_static_dist()
    return WebUIService(config_dir=config_dir, port=port, static_dist=static_dist)


def _detect_static_dist() -> Path | None:
    """Auto-detect the frontend dist directory."""
    dist_path = Path(__file__).resolve().parent.parent / "bot" / "web" / "dist"
    return dist_path if dist_path.exists() else None


# ── CLI argument parsing ─────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the bot service."""
    parser = argparse.ArgumentParser(description="Run the QQ Bot example service.")
    return parser.parse_args(argv)
