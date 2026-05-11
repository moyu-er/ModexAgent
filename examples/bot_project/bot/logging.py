"""Logging bootstrap for the bot project.

Call setup_logging() once at program start, before importing modules
that may trigger logging. After the call, logging.getLogger(__name__)
works normally in every module.
"""

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logging() -> None:
    """Configure root logger with console + rotating file handlers."""
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    detailed_format = (
        "%(asctime)s - %(name)s - %(levelname)s - "
        "[%(filename)s:%(lineno)d] - %(message)s"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
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
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(detailed_format))
    root_logger.addHandler(file_handler)

    for name in ["asyncio", "LiteLLM", "botpy", "mcp", "httpx", "httpcore", "urllib3"]:
        logging.getLogger(name).setLevel(logging.WARNING)
