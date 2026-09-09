"""stdio entry — stdout is protocol-owned; all logging goes to stderr.

Runnable as ``python -m modex_agent.acp``. Without a backend the scripted
backend (``scripted.py``) is used; business wiring (T08) passes a
project-bound ``AcpSessionBackend`` (the bot's ``AcpRuntime``).

Lifecycle (SDK 0.12.1 official instance path, DESIGN.md §4.3): the agent is
built backend-only and passed as an *instance* to ``acp.run_agent`` — the
SDK's ``AgentSideConnection.__init__`` calls ``agent.on_connect`` with the
live connection before the router starts listening, so no factory is needed
here.

Cleanup runs as ONE entry-owned task: drain the agent first, then the boot
owner's ``backend.close()`` — each step runs even when the previous one
failed, and collected errors surface only after all cleanup ran (a single
error as-is, several as an ``ExceptionGroup``). The serve awaits that task
behind ``asyncio.shield``; its loop exists only to absorb repeated
cancellations of the WAITER while shielding the same concrete cleanup task
— no polling, sleeping, or watchdog. Cancelling the serve can therefore
never cut the drain short or close the backend early. A serve cancellation
propagates the original ``CancelledError`` only AFTER cleanup completed,
with a real cleanup failure chained as its ``__cause__`` — a cancel never
masks a cleanup failure as a clean shutdown, and a cleanup failure never
replaces the cancel. This is not a completion mechanism: turn-level
completion stays the business tree's ``wait_quiesce`` (convergence rule 3);
``agent.aclose`` itself is not wrapped (direct callers keep plain asyncio
semantics — the entry's shield serves the serve path). No
retry/sleep/timeout wrapping here (T10).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from .backend import AcpSessionBackend

__all__ = ["main"]


def main(backend: AcpSessionBackend | None = None) -> None:
    """Run the ACP agent-server on stdin/stdout until the client disconnects."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_serve(backend))


async def _serve(backend: AcpSessionBackend | None) -> None:
    import acp

    from .scripted import ScriptedAcpBackend
    from .server import ModexAcpAgent

    session_backend: AcpSessionBackend = backend if backend is not None else ScriptedAcpBackend()
    agent = ModexAcpAgent(session_backend)

    async def run_cleanup() -> None:
        # drain the agent first, then the boot owner's backend close — each
        # step runs even when the previous one failed; errors surface after
        # all cleanup ran (DESIGN.md §4.3, T10)
        errors: list[Exception] = []
        try:
            await agent.aclose()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        try:
            await session_backend.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("acp serve shutdown incomplete", errors)

    serve_error: Exception | None = None
    serve_cancel: asyncio.CancelledError | None = None
    cleanup_error: Exception | None = None
    try:
        await acp.run_agent(agent)
    except Exception as exc:  # noqa: BLE001 — settled after full cleanup below
        serve_error = exc
    except asyncio.CancelledError as exc:
        serve_cancel = exc  # re-raised after cleanup, cleanup cause chained
    finally:
        cleanup_task = asyncio.create_task(run_cleanup())
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError as exc:
                # the WAITER was cancelled, not the cleanup task: keep
                # shielding the same concrete task until it completes (this
                # loop only absorbs repeated cancels of the serve — it never
                # polls, sleeps, or times out); the first cancel is the one
                # propagated
                if serve_cancel is None:
                    serve_cancel = exc
            except Exception as exc:  # noqa: BLE001 — surfaced after the loop
                cleanup_error = exc
                break
    if serve_cancel is not None:
        if cleanup_error is not None and serve_error is not None:
            raise serve_cancel from ExceptionGroup(
                "ACP transport and cleanup failed", [serve_error, cleanup_error],
            )
        if cleanup_error is not None:
            raise serve_cancel from cleanup_error
        raise serve_cancel from serve_error
    if cleanup_error is not None:
        raise cleanup_error from serve_error
    if serve_error is not None:
        raise serve_error
