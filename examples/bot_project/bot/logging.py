"""Logging bootstrap for the bot project.

Call setup_logging() once at program start, before importing modules
that may trigger logging. After the call, logging.getLogger(__name__)
works normally in every module.
"""

import logging
import logging.handlers
import sys
from pathlib import Path


def _reconfigure_stdio_utf8() -> None:
    """Ensure stdout/stderr can encode the full Unicode range.

    On Windows the default console code page (e.g. cp936/GBK) cannot encode
    many characters that legitimately appear in tool output (emoji, box
    drawing, rare CJK), which crashes ``logging.StreamHandler.emit`` with
    ``UnicodeEncodeError``. Reconfigure the standard streams to UTF-8 with a
    safe error handler so logging never raises on any payload.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def setup_logging() -> None:
    """Configure root logger with console + rotating file handlers."""
    _reconfigure_stdio_utf8()

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    detailed_format = (
        "%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = []

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(detailed_format))
    root_logger.addHandler(file_handler)

    for name in ["asyncio", "LiteLLM", "litellm", "botpy", "mcp", "httpx", "httpcore", "urllib3"]:
        logging.getLogger(name).setLevel(logging.WARNING)
