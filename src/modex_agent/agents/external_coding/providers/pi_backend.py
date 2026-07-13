"""``PiBackend`` — the real Pi CLI :class:`StreamingProviderBackend`.

Constructs ``pi -p --mode json --session <path>
[--provider X --model Y] [--append-system-prompt <s>] <prompt>``,
spawns via the T3 OS layer (``cwd=workdir``, ``env=...``), closes stdin
immediately (``asyncio.subprocess.DEVNULL`` — Pi does not read stdin
but leaving it open can hang under systemd), reads stdout JSONL line
by line, hands each line to :class:`PiEventParser`, captures the
stderr tail into :class:`BackendResult.error` on non-zero exit, and
raises :class:`StaleSessionError` when stderr carries a
session-not-found message so the harness (T5) can invalidate the
stored mapping and retry once with a fresh session.

Session continuity: Pi identifies sessions by **file path**. The
backend reuses ``opts.resume_session_id`` as the ``--session`` path
when present; otherwise it derives the canonical
``<workdir>/.modex/external/pi-session.jsonl`` path (matching
:class:`ExternalPaths`) and returns it in
:class:`BackendResult.session_id` for the harness to commit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import override

from ..agent import (
    _STDERR_TAIL_CHARS,
    StaleSessionError,
    StreamingProviderBackend,
    _is_stale_session,
    _safe_terminate,
)
from ..os_layer import resolve_executable, spawn_process_group
from ..paths import ExternalPaths, ProviderKind
from ..types import BackendResult, BackendStatus, Emission, ExecOptions
from .pi_parser import PiEventParser

logger = logging.getLogger(__name__)

__all__ = ["PiBackend"]


class PiBackend(StreamingProviderBackend):
    """Real Pi CLI backend — spawns ``pi`` via the OS layer and streams events."""

    def __init__(self, *, provider: str | None = None) -> None:
        super().__init__()
        self._provider = provider

    @override
    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        session_path = self._session_path(opts)
        pi_args = self._build_args(opts, session_path)
        resolved = resolve_executable("pi", logger)
        full_args = [resolved.argv0, *resolved.extra_args, *pi_args]

        proc = await spawn_process_group(
            full_args,
            cwd=opts.workdir,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        assert stdout is not None  # noqa: S101
        assert stderr is not None  # noqa: S101

        parser = PiEventParser()
        try:
            async for raw_line in stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                for emission in parser.parse_line(line):
                    await on_emission(emission)
        except Exception as exc:
            await _safe_terminate(proc)
            if _is_stale_session(str(exc)):
                raise StaleSessionError(f"Pi session {session_path} is stale: {exc}") from exc
            raise

        await proc.wait()

        stderr_tail = ""
        if proc.returncode != 0:
            try:
                stderr_data = await stderr.read()
                stderr_tail = stderr_data.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                stderr_tail = ""
            stderr_tail = stderr_tail[-_STDERR_TAIL_CHARS:]

        if proc.returncode != 0 and _is_stale_session(stderr_tail):
            raise StaleSessionError(f"Pi session {session_path} is stale: {stderr_tail.strip()}")

        status: BackendStatus = BackendStatus.COMPLETED if proc.returncode == 0 else BackendStatus.FAILED
        trimmed = stderr_tail.strip()
        error = trimmed if (proc.returncode != 0 and trimmed) else None
        return BackendResult(status=status, session_id=session_path, error=error)

    # ------------------------------------------------------------------
    # Internals — testable helpers
    # ------------------------------------------------------------------

    def _session_path(self, opts: ExecOptions) -> str:
        """The ``--session`` file path Pi uses for this invocation.

        Resume path: the stored provider session id (which for Pi IS
        the file path). Fresh path: the canonical
        ``<workdir>/.modex/external/pi-session.jsonl`` from
        :class:`ExternalPaths`.
        """
        if opts.resume_session_id is not None:
            return opts.resume_session_id
        return str(ExternalPaths(opts.workdir).provider_session(ProviderKind.PI))

    def _build_args(self, opts: ExecOptions, session_path: str) -> list[str]:
        """Build the Pi-specific argv (after ``argv0`` and shim extras).

        Flag order follows the spec verbatim:
        ``-p --mode json --session <path> [--provider X --model Y]
        [--append-system-prompt <s>] <prompt>``.
        """
        args: list[str] = ["-p", "--mode", "json", "--session", session_path]
        if self._provider is not None:
            args.extend(["--provider", self._provider])
        if opts.model is not None:
            args.extend(["--model", opts.model])
        if opts.system_prompt is not None:
            args.extend(["--append-system-prompt", opts.system_prompt])
        args.append(opts.prompt)
        return args
