"""Provider contracts — the ABCs every external coding backend adapts to.

Two interfaces, each carrying one abstract method:

- `ProviderBackend.execute(opts) -> BackendResult` — spawn the provider
  CLI and return its terminal result.
- `ProviderEventParser.parse_line(line) -> Iterator[Emission]` —
  consume one stdout JSONL line and yield zero or more per-event
  emissions.

Both interfaces admit concrete subclasses without touching the
framework. New providers (Pi, OpenCode, Claude Code, Codex, Cursor)
plug in as one backend file plus one parser file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from .types import BackendResult, Emission, ExecOptions


class ProviderBackend(ABC):
    """One provider's "spawn + return result" contract.

    Stateless beyond its construction-time config (binary path, default
    flags, etc.). Session continuity is the caller's job — pass
    ``opts.resume_session_id`` and recover the next session id from
    ``BackendResult.session_id``.
    """

    @abstractmethod
    async def execute(self, opts: ExecOptions) -> BackendResult:
        """Spawn the provider CLI once and return its terminal result.

        Implementations must close the child's stdin immediately if
        they do not write to it (Pi in particular can hang under
        systemd when stdin is left open). They must run the child in
        its own process group so cancellation reaches tool
        subprocesses the provider spawns.
        """
        ...


class ProviderEventParser(ABC):
    """Parse one stdout JSONL line into zero or more `Emission`s.

    A single line carrying multiple updates (Pi's ``message_update``
    with both thinking and text delta) fans out by yielding more than
    one ``Emission``. Lines that carry no provider-relevant payload
    (e.g. status / log / usage) yield nothing — they are silently
    dropped on day one.
    """

    @abstractmethod
    def parse_line(self, line: str) -> Iterator[Emission]:
        """Parse one stdout JSONL line.

        Args:
            line: A single JSONL line (without trailing newline).

        Yields:
            Zero or more `Emission` records consumers fan out through
            `ContentEmitter`. The caller must consume the iterator
            fully before passing the next line — parsers are free to
            keep incremental state (e.g. Pi's delta-stripping buffer)
            across calls.
        """
        ...  # parsed via yield — see subclass


__all__ = ["ProviderBackend", "ProviderEventParser"]
