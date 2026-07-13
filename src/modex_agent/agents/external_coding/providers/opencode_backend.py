"""``OpenCodeBackend`` — the real OpenCode CLI :class:`StreamingProviderBackend`.

Constructs ``opencode run --format json --dangerously-skip-permissions
--dir <workdir> [--model M] [--session <id>] <prompt>``, injects
``PWD=<workdir>`` into env (OpenCode prefers ``PWD`` over ``cwd`` for
``AGENTS.md`` discovery), spawns via the OS layer, reads stdout JSONL
line by line, hands each line to :class:`OpenCodeEventParser`,
captures the provider-minted session id from the parser's out-of-band
``captured_session_id`` channel, and returns it in
:class:`BackendResult.session_id` for the harness (T5) to commit.

System prompt is injected exclusively through the ``AGENTS.md``
file (written by :class:`ExternalCodingAgent._run_turn` via
:func:`write_runtime_block`). The OpenCode CLI does not expose a
``--prompt`` flag for system-prompt override.

Stale-session detection mirrors :class:`PiBackend`: the stderr tail
on non-zero exit is scanned for session-not-found patterns and, when
matched, :class:`StaleSessionError` is raised so the harness
invalidates the stored mapping and retries fresh.
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
from ..types import BackendResult, BackendStatus, Emission, ExecOptions
from .opencode_parser import OpenCodeEventParser

logger = logging.getLogger(__name__)

__all__ = ["OpenCodeBackend"]


class OpenCodeBackend(StreamingProviderBackend):
    """Real OpenCode CLI backend — spawns ``opencode run`` via the OS layer."""

    @override
    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        oc_args = self._build_args(opts)
        resolved = resolve_executable("opencode", logger)
        full_args = [resolved.argv0, *resolved.extra_args, *oc_args]

        spawn_env = dict(env)
        spawn_env["PWD"] = str(opts.workdir)

        proc = await spawn_process_group(
            full_args,
            cwd=opts.workdir,
            env=spawn_env,
            stdin=asyncio.subprocess.DEVNULL,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        assert stdout is not None  # noqa: S101
        assert stderr is not None  # noqa: S101

        parser = OpenCodeEventParser()
        try:
            async for raw_line in stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                for emission in parser.parse_line(line):
                    await on_emission(emission)
        except Exception as exc:
            await _safe_terminate(proc)
            if _is_stale_session(str(exc)):
                raise StaleSessionError(f"OpenCode session is stale: {exc}") from exc
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
            raise StaleSessionError(f"OpenCode session is stale: {stderr_tail.strip()}")

        status: BackendStatus = BackendStatus.COMPLETED if proc.returncode == 0 else BackendStatus.FAILED
        trimmed = stderr_tail.strip()
        error = trimmed if (proc.returncode != 0 and trimmed) else None
        return BackendResult(
            status=status,
            session_id=parser.captured_session_id,
            error=error,
        )

    def _build_args(self, opts: ExecOptions) -> list[str]:
        args: list[str] = [
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            "--thinking",
            "--dir",
            str(opts.workdir),
        ]
        if opts.model is not None:
            args.extend(["--model", opts.model])
        if opts.resume_session_id is not None:
            args.extend(["--session", opts.resume_session_id])
        args.append(opts.prompt)
        return args
