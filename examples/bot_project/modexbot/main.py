"""Service orchestration — supervisor, signal handling, and entry points.

Moved from ``bot_service.py`` so the CLI + orchestration layer lives under
``modexbot/`` while business logic stays in ``bot/service/``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal as signal_mod
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.runtime.bundled_bin import ensure_bundled_bin_on_path

if TYPE_CHECKING:
    from bot.service.core import BotService

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_FAILURES: int = 2
_STABLE_RUN_SECONDS: float = 30.0


# ── Factory helpers ──────────────────────────────────────────────────────────


def create_qq_service(config_dir: Path) -> "BotService":
    """Create a QQ Bot service instance."""
    from bot.logging import setup_logging

    setup_logging()

    from bot.service.qq_service import QQBotService

    return QQBotService(config_dir)


def create_webui_service(
    config_dir: Path, *, port: int | None = None, static_dist: Path | None = None
) -> "BotService":
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


# ── Signal handling ──────────────────────────────────────────────────────────


def _install_signal_handlers(service: "BotService") -> None:
    """Register graceful-shutdown signal handlers (cross-platform).

    - Linux/macOS: ``loop.add_signal_handler`` (asyncio-native).
    - Windows: ``signal.signal(SIGINT, SIGBREAK)`` — SIGTERM is
      uncatchable on Windows (TerminateProcess), but ``taskkill``
      without ``/f`` sends CTRL_BREAK_EVENT which Python maps to
      ``SIGBREAK``. Both SIGINT (Ctrl+C) and SIGBREAK (taskkill) are
      registered so either signal triggers graceful shutdown.
    """

    def _graceful_shutdown() -> None:
        logger.info("Shutdown signal received, setting _shutdown_event")
        service._shutdown_event.set()

    if sys.platform == "win32":
        for _sig in (signal_mod.SIGINT, signal_mod.SIGBREAK):
            signal_mod.signal(_sig, lambda _s, _f: _graceful_shutdown())
    else:
        loop = asyncio.get_running_loop()
        for _sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
            loop.add_signal_handler(_sig, _graceful_shutdown)


# ── Process-level supervisor ─────────────────────────────────────────────────


def run_with_supervisor(
    service_factory: Callable[[], "BotService"],
    argv: list[str] | None = None,
) -> None:
    """Process-level supervisor: auto-restart on crash, exit on user stop.

    Behaviour:
    - Normal exit (Ctrl+C / signal): **no restart**, process exits.
    - ``KeyboardInterrupt`` / ``SystemExit``: propagated, **no restart**.
    - Unhandled ``Exception``: restart, up to ``_MAX_CONSECUTIVE_FAILURES``
      consecutive times.  If the process ran for ≥ ``_STABLE_RUN_SECONDS``
      before crashing, the consecutive-failure counter resets (it was a
      runtime crash, not a startup failure).
    """

    async def _main() -> None:
        service = service_factory()
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

    consecutive_failures = 0

    ensure_bundled_bin_on_path()

    while True:
        start_time = time.monotonic()
        try:
            asyncio.run(_main())
            # Normal return — user-initiated shutdown (signal).
            return
        except KeyboardInterrupt:
            return
        except SystemExit:
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
            signal_mod.signal(signal_mod.SIGINT, signal_mod.SIG_DFL)
            time.sleep(3)
