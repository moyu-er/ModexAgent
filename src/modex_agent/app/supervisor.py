"""Process-level supervisor for an :class:`~modex_agent.app.service.AppService`.

Promoted from ``examples/bot_project/modexbot/main.py`` (W4b):
crash-restart with a stable-period reset, cooperative cross-platform signal
handling, and the initialize → start → stop(finally) entry shape.
"""

from __future__ import annotations

import asyncio
import logging
import signal as signal_mod
import sys
import time
from collections.abc import Callable

from modex_agent.app.service import AppService
from modex_agent.utils.bundled_bin import ensure_bundled_bin_on_path

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_FAILURES: int = 2
_STABLE_RUN_SECONDS: float = 30.0

__all__ = ["run_with_supervisor", "install_signal_handlers"]


def install_signal_handlers(service: AppService) -> None:
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
        service._shutdown_event.set()  # noqa: SLF001 — supervisor drives the contract

    if sys.platform == "win32":
        for _sig in (signal_mod.SIGINT, signal_mod.SIGBREAK):
            signal_mod.signal(_sig, lambda _s, _f: _graceful_shutdown())
    else:
        loop = asyncio.get_running_loop()
        for _sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
            loop.add_signal_handler(_sig, _graceful_shutdown)


def run_with_supervisor(service_factory: Callable[[], AppService]) -> None:
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
        install_signal_handlers(service)

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
