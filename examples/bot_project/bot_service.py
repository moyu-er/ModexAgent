"""Bot service entry point.

QQ Bot specialization of the generic BotService.
Run with: python bot_service.py

Uses IOC AppConfig.from_yaml() for config loading.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal as signal_mod
import sys
import time
from pathlib import Path
from typing import Literal

# Path setup
framework_dir = Path(__file__).resolve().parent.parent.parent
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

logger = logging.getLogger(__name__)


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


def _install_signal_handlers(service: BotService) -> None:
    """Register graceful-shutdown signal handlers (cross-platform).

    - Linux/macOS: ``loop.add_signal_handler`` (asyncio-native).
    - Windows: ``signal.signal(SIGINT)`` — SIGTERM is uncatchable
      on Windows (TerminateProcess), so only SIGINT is registered.
    """
    def _graceful_shutdown() -> None:
        logger.info("Shutdown signal received, setting _shutdown_event")
        service._shutdown_event.set()

    if sys.platform == "win32":
        signal_mod.signal(
            signal_mod.SIGINT,
            lambda _sig, _frame: _graceful_shutdown(),
        )
    else:
        loop = asyncio.get_running_loop()
        for _sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
            loop.add_signal_handler(_sig, _graceful_shutdown)


async def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    args = parse_args(argv)
    config_dir = Path(__file__).resolve().parent / "config"
    service = create_qq_service(config_dir, mode=args.mode)

    _install_signal_handlers(service)

    try:
        await service.initialize()
        await service.start()
    except asyncio.CancelledError:
        logger.warning("Main task cancelled unexpectedly — possible external signal")
    except Exception as e:
        logger.exception("Fatal error in main: %s", e)
        raise
    finally:
        logger.info("Initiating shutdown sequence")
        await service.stop()


# ---------------------------------------------------------------------------
# Process-level supervisor
# ---------------------------------------------------------------------------

_MAX_CONSECUTIVE_FAILURES: int = 2
_STABLE_RUN_SECONDS: float = 30.0


def run_with_supervisor(argv: list[str] | None = None) -> None:
    """Process-level supervisor: auto-restart on crash, exit on user stop.

    Behaviour:
    - Normal exit (Ctrl+C / signal): **no restart**, process exits.
    - ``KeyboardInterrupt`` / ``SystemExit``: propagated, **no restart**.
    - Unhandled ``Exception``: restart, up to *_MAX_CONSECUTIVE_FAILURES*
      consecutive times.  If the process ran for ≥ *_STABLE_RUN_SECONDS*
      before crashing, the consecutive-failure counter resets (it was a
      runtime crash, not a startup failure).
    """
    consecutive_failures = 0

    while True:
        start_time = time.monotonic()
        try:
            asyncio.run(main(argv))
            # Normal return — user-initiated shutdown (signal).
            return
        except KeyboardInterrupt:
            # Ctrl+C that bypassed the signal handler.
            return
        except SystemExit:
            # sys.exit() or fatal — don't restart.
            raise
        except Exception as e:
            elapsed = time.monotonic() - start_time
            if elapsed >= _STABLE_RUN_SECONDS:
                consecutive_failures = 0

            consecutive_failures += 1
            if consecutive_failures > _MAX_CONSECUTIVE_FAILURES:
                logger.critical(
                    "Supervisor: %d consecutive failures, giving up. Last: %s",
                    consecutive_failures,
                    e,
                )
                sys.exit(1)

            logger.warning(
                "Supervisor: crash after %.1fs (attempt %d/%d), restarting in 3s — %s",
                elapsed,
                consecutive_failures,
                _MAX_CONSECUTIVE_FAILURES,
                e,
            )
            # Restore default SIGINT so Ctrl+C during restart-delay raises
            # KeyboardInterrupt (the custom handler references the old, now
            # defunct service — it would silently swallow the signal).
            signal_mod.signal(signal_mod.SIGINT, signal_mod.SIG_DFL)
            time.sleep(3)


if __name__ == "__main__":
    run_with_supervisor()
