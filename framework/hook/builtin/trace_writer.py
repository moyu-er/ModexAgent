"""TraceFileWriter — ControlEventBus subscriber that writes trace events to JSON-lines file."""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path

from framework.control.types import ControlEvent, ControlEventType

logger = logging.getLogger(__name__)


class TraceFileWriter:
    """Writes AGENT_PROGRESS events to a JSON-lines file with rotation.

    Usage::

        writer = TraceFileWriter(path=Path("logs/trace.jsonl"))
        await event_bus.subscribe(ControlEventType.AGENT_PROGRESS, writer.handle)
    """

    def __init__(
        self,
        path: Path,
        max_bytes: int = 20 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(
            str(path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        # Prevent any formatter from wrapping JSON lines
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    async def handle(self, event: ControlEvent) -> None:
        if event.type != ControlEventType.AGENT_PROGRESS:
            return
        try:
            entry = {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                **event.payload,
            }
            line = json.dumps(entry, ensure_ascii=False, default=str)
            self._handler.emit(
                logging.LogRecord(
                    name="trace",
                    level=logging.INFO,
                    pathname="",
                    lineno=0,
                    msg=line,
                    args=None,
                    exc_info=None,
                )
            )
        except Exception:
            logger.debug("TraceFileWriter write failed", exc_info=True)

    def close(self) -> None:
        self._handler.close()
