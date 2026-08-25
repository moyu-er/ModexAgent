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
from datetime import UTC, datetime
from pathlib import Path

from bot.config import PromptContent, PromptSummary

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]+$")


class UnknownPromptError(KeyError):
    """Raised when an agent prompt md is not present on disk."""


class PromptValidationError(ValueError):
    """Raised on a bad agent name (regex or path-traversal failure)."""


class PromptExistsError(Exception):
    """Raised by :meth:`PromptStore.create_prompt` when the md already exists.

    Distinct from :class:`PromptValidationError` (which maps to HTTP 400) so
    the route layer can map this to HTTP 409 without string-sniffing. Mirrors
    the :class:`bot.service.pool_config_controller.PoolNotEmptyError` precedent
    for the dedicated-exception-maps-to-409 pattern.
    """


def _validate_agent_name(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PromptValidationError(f"Invalid agent name {name!r}: must match {_NAME_RE.pattern}")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise PromptValidationError(f"Invalid agent name {name!r}: traversal")


class PromptStore:
    """Read/write ``agents/<name>.md``. Plain runtime class (base_dir).

    :data:`DEFAULT_PROMPT_SEED` is the single canonical default prompt text
    (no framework-layer duplicate); it seeds new prompt md files created
    through the prompts REST API.
    """

    DEFAULT_PROMPT_SEED: str = """\
You are an AI assistant.

## Interaction Guidelines
- Respond naturally and concisely.
- Give direct answers first, then add explanation.
- Be honest about uncertainty — never fabricate information.

## Output Constraints
- Keep responses reasonably concise.
- Do not output internal debug info or raw tool returns.
"""

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

    def list_prompts(self) -> list[PromptSummary]:
        """List every ``agents/*.md`` whose stem matches the agent-name regex.

        Non-matching files (e.g. ``AGENTS.md``) are excluded. The result is
        sorted alphabetically by name. ``mtime`` is an ISO 8601 UTC string.
        """
        if not self.agents_dir.exists():
            return []
        out: list[PromptSummary] = []
        for md in self.agents_dir.glob("*.md"):
            stem = md.stem
            if not _NAME_RE.match(stem):
                continue
            stat = md.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
            out.append(
                PromptSummary(
                    name=stem,
                    size_bytes=stat.st_size,
                    mtime=mtime,
                )
            )
        out.sort(key=lambda s: s.name)
        return out

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

    def create_prompt(self, agent: str, content: str) -> PromptContent:
        """Atomically create ``agents/<agent>.md``; refuse if it already exists.

        Validation mirrors :meth:`write_prompt` (name regex + traversal guard),
        but the write is conditional on the file being absent — a second create
        on the same name raises :class:`PromptExistsError` (mapped to HTTP 409).
        The disk is untouched on any validation or existence failure.
        """
        md = self._md_path(agent)
        if md.exists():
            raise PromptExistsError(f"Prompt for agent {agent!r} already exists")
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        tmp = md.with_name(md.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, md)
        return PromptContent(name=agent, content=content)

    def prompt_exists(self, agent: str) -> bool:
        """True if ``agents/<agent>.md`` exists."""
        return self._md_path(agent).exists()

    def delete_prompt(self, agent: str) -> None:
        """Delete ``agents/<agent>.md``. Raises :class:`UnknownPromptError` if absent.

        Validates the agent name (regex + traversal guard via :meth:`_md_path`)
        before checking existence. Does NOT perform reference checking — that
        is the controller's job (cross-pool scan). The delete is a single
        ``Path.unlink`` (no atomic temp needed for a delete).
        """
        md = self._md_path(agent)
        if not md.exists():
            raise UnknownPromptError(f"No prompt for agent {agent!r}")
        md.unlink()

    def read_or_seed_prompt(
        self,
        agent: str,
        default_content: str | None = None,
    ) -> PromptContent:
        """Read ``agents/<agent>.md``; if missing, atomically seed it and return.

        Idempotent and safe for concurrent callers: if the file appears between
        the existence check and the write, the write simply overwrites with the
        same default content. Uses the same ``.tmp`` + ``os.replace`` atomicity
        as :meth:`write_prompt`. When ``default_content`` is omitted, the seed
        text is :attr:`DEFAULT_PROMPT_SEED` (the single canonical default).
        """
        md = self._md_path(agent)
        if md.exists():
            content = md.read_text(encoding="utf-8")
        else:
            content = (
                default_content
                if default_content is not None
                else PromptStore.DEFAULT_PROMPT_SEED
            )
            self.agents_dir.mkdir(parents=True, exist_ok=True)
            tmp = md.with_name(md.name + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, md)
        return PromptContent(name=agent, content=content)
