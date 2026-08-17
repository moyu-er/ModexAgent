from __future__ import annotations

import logging
import os
import socket

from modex_graph.id_generator import default_id_generator

logger = logging.getLogger(__name__)


class ProcessIdentity:
    """Lazily generated process identity used for graph instance ownership.

    The identity remains stable for this object's lifetime. A restarted process
    creates a new object and therefore receives a new snowflake ID.
    """

    def __init__(self) -> None:
        self._id: int | None = None

    @property
    def process_id(self) -> int:
        if self._id is None:
            self._id = default_id_generator().generate()
            logger.info(
                "ProcessIdentity generated: id=%d, host=%s, pid=%d",
                self._id,
                socket.gethostname(),
                os.getpid(),
            )
        return self._id


__all__ = ["ProcessIdentity"]
