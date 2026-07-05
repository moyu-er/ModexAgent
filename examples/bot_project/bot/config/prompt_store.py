"""PromptStore — read/write agent prompt markdown files (``agents/<name>.md``).

Single source of truth for prompt-md persistence. Operates on a configurable
base dir (default ``examples/bot_project``-relative; overridable per-instance
for ``tmp_path`` tests).

The prompt convention is pool-independent by agent name: the loader reads
``agents/<name>.md`` regardless of pool. The HTTP route still carries the pool
path segment for API clarity, but the store does not need it.

All writes are atomic (``.tmp`` + ``os.replace``), UTF-8, and preserve a
trailing newline.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from bot.config.pool_payloads import PromptContent

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]+$")


class UnknownPromptError(KeyError):
    """Raised when an agent prompt md is not present on disk."""


class PromptValidationError(ValueError):
    """Raised on a bad agent name (regex or path-traversal failure)."""


def _validate_agent_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PromptValidationError(
            f"Invalid agent name {name!r}: must match {_NAME_RE.pattern}"
        )
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise PromptValidationError(f"Invalid agent name {name!r}: traversal")


class PromptStore:
    """Read/write ``agents/<name>.md``. Plain runtime class (base_dir)."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir: Path = Path(base_dir) if base_dir is not None else Path(".")
        self.agents_dir: Path = self.base_dir / "agents"

    def _md_path(self, agent: str) -> Path:
        _validate_agent_name(agent)
        return self.agents_dir / f"{agent}.md"

    def read_prompt(self, agent: str) -> PromptContent:
        """Read ``agents/<agent>.md``.

        Raises :class:`UnknownPromptError` if the md is absent.
        """
        md = self._md_path(agent)
        if not md.exists():
            raise UnknownPromptError(f"No prompt for agent {agent!r}")
        return PromptContent(name=agent, content=md.read_text(encoding="utf-8"))

    def write_prompt(self, agent: str, content: str) -> PromptContent:
        """Atomically write ``agents/<agent>.md`` (UTF-8, trailing newline kept).

        Validates the agent name regex first (path-traversal guard); on any
        validation failure the disk is untouched.
        """
        md = self._md_path(agent)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        tmp = md.with_name(md.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, md)
        return PromptContent(name=agent, content=content)

    def prompt_exists(self, agent: str) -> bool:
        """True if ``agents/<agent>.md`` exists."""
        return self._md_path(agent).exists()
