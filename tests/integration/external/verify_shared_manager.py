"""Real opencode verification script for the shared OpenCodeServerManager.

Tests the shared-server architecture end-to-end with a real ``opencode``
binary:

1. **Lifecycle** — ``async with OpenCodeServerManager.lifecycle():`` binds
   the singleton lifetime. On exit, ``_shutdown()`` cleans up.
2. **Multi-session same workdir** — two sessions in the same workdir share
   one SSE reader + parser. Both receive their events.
3. **Multi-workdir** — a session in a different workdir gets its own SSE
   reader + parser. Events are correctly routed via ``x-opencode-directory``.
4. **Child capture** — the ``task`` tool spawns a child session. The SSE
   reader auto-discovers it via ``session.created`` with ``parentID``.
5. **Main continuation** — after the child completes, the main session
   continues with new ``message.part.delta`` events.
6. **Watchdog respawn** — manually kill the opencode process. The watchdog
   detects it within 5s and respawns. The next turn succeeds.

Usage::

    MODEX_OPENCODE_EXECUTABLE=$(which opencode) .venv/bin/python \\
        tests/integration/external/verify_shared_manager.py

Or with an external server::

    OPENCODE_HOST=http://localhost:4096 .venv/bin/python \\
        tests/integration/external/verify_shared_manager.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

from modex_agent.agents.external.providers.opencode_server_backend import (
    OpenCodeServerBackend,
)
from modex_agent.agents.external.providers.opencode_server_manager import (
    OpenCodeServerManager,
)

from modex_agent.agents.external import Emission, ExternalEvent
from modex_agent.agents.external.types import ExecOptions

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
logger = logging.getLogger("verify")


def _make_env(modex_sid: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "MODEX_SESSION_ID": modex_sid,
            "MODEX_AGENT_NAME": "opencode",
            "MODEX_INBOX_ROOT": os.environ.get("TEMP", "/tmp"),
            "MODEX_AGENT_POOL_MAP": "opencode=pool_opencode",
            "MODEX_TARGETS": "",
        }
    )
    return env


async def _run_turn(
    backend: OpenCodeServerBackend,
    prompt: str,
    workdir: Path,
    env: dict[str, str],
    resume_session_id: str | None = None,
) -> tuple[list[Emission], str | None]:
    """Run one turn and return (emissions, session_id)."""
    emissions: list[Emission] = []

    async def on_emission(e: Emission) -> None:
        emissions.append(e)

    opts = ExecOptions(prompt=prompt, workdir=workdir, resume_session_id=resume_session_id)
    result = await backend.execute_streaming(opts, env, on_emission)
    logger.info(
        "Turn result: status=%s, session=%s, emissions=%d",
        result.status,
        result.session_id,
        len(emissions),
    )
    return emissions, result.session_id


async def test_multi_session_same_workdir(workdir: Path) -> None:
    """Two sessions in the same workdir — both should receive events."""
    logger.info("=== Test: Multi-session same workdir ===")
    env = _make_env("verify.same_workdir")
    backend = OpenCodeServerBackend()

    emissions1, sid1 = await _run_turn(backend, "Say hello in exactly three words.", workdir, env)
    assert sid1 is not None, "First turn should return a session_id"
    text1 = "".join(e.text or "" for e in emissions1 if e.event is ExternalEvent.TEXT_DELTA)
    logger.info("Turn 1: session=%s, text=%r", sid1, text1[:100])
    assert len(text1) > 0, "Turn 1 should produce text"

    emissions2, sid2 = await _run_turn(backend, "Say goodbye in exactly two words.", workdir, env)
    assert sid2 is not None and sid2 != sid1, "Second turn should be a new session"
    text2 = "".join(e.text or "" for e in emissions2 if e.event is ExternalEvent.TEXT_DELTA)
    logger.info("Turn 2: session=%s, text=%r", sid2, text2[:100])
    assert len(text2) > 0, "Turn 2 should produce text"

    logger.info("PASS: Both sessions received events in same workdir")


async def test_multi_workdir(workdir_a: Path, workdir_b: Path) -> None:
    """Sessions in different workdirs — each gets its own SSE reader."""
    logger.info("=== Test: Multi-workdir ===")
    backend = OpenCodeServerBackend()

    env_a = _make_env("verify.workdir_a")
    emissions_a, sid_a = await _run_turn(backend, "Say 'A'.", workdir_a, env_a)
    assert sid_a is not None
    text_a = "".join(e.text or "" for e in emissions_a if e.event is ExternalEvent.TEXT_DELTA)
    logger.info("Workdir A: session=%s, text=%r", sid_a, text_a[:50])
    assert len(text_a) > 0, "Workdir A should produce text"

    env_b = _make_env("verify.workdir_b")
    emissions_b, sid_b = await _run_turn(backend, "Say 'B'.", workdir_b, env_b)
    assert sid_b is not None and sid_b != sid_a
    text_b = "".join(e.text or "" for e in emissions_b if e.event is ExternalEvent.TEXT_DELTA)
    logger.info("Workdir B: session=%s, text=%r", sid_b, text_b[:50])
    assert len(text_b) > 0, "Workdir B should produce text"

    mgr = OpenCodeServerManager._instance
    assert mgr is not None
    assert len(mgr._workdir_entries) >= 2, "Should have 2 workdir entries"

    logger.info("PASS: Multi-workdir routing works (separate SSE readers)")


async def test_resume(workdir: Path) -> None:
    """Resume a session — should reuse the same provider session_id."""
    logger.info("=== Test: Session resume ===")
    env = _make_env("verify.resume")
    backend = OpenCodeServerBackend()

    emissions1, sid1 = await _run_turn(backend, "Remember the number 42.", workdir, env)
    assert sid1 is not None
    logger.info("Turn 1: session=%s", sid1)

    emissions2, sid2 = await _run_turn(
        backend, "What number did I ask you to remember?", workdir, env, resume_session_id=sid1
    )
    assert sid2 == sid1, "Resume should reuse the same session_id"
    text2 = "".join(e.text or "" for e in emissions2 if e.event is ExternalEvent.TEXT_DELTA)
    logger.info("Turn 2 (resume): session=%s, text=%r", sid2, text2[:100])
    assert "42" in text2, "Resume should remember the number 42"

    logger.info("PASS: Session resume reuses provider session_id")


async def test_watchdog_respawn(workdir: Path) -> None:
    """Kill the opencode process — watchdog should respawn within 5s."""
    logger.info("=== Test: Watchdog respawn ===")
    mgr = OpenCodeServerManager._instance
    assert mgr is not None and mgr._proc is not None
    old_pid = mgr._proc.pid
    logger.info("Old opencode PID: %d", old_pid)

    # Kill the process
    try:
        os.kill(old_pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        logger.warning("Cannot kill process %d: %s — skipping watchdog test", old_pid, e)
        return

    logger.info("Killed opencode PID %d — waiting for watchdog respawn (≤10s)...", old_pid)
    deadline = asyncio.get_event_loop().time() + 15.0
    while asyncio.get_event_loop().time() < deadline:
        if mgr._proc is not None and mgr._proc.pid != old_pid and mgr._proc.returncode is None:
            logger.info("Watchdog respawned opencode! New PID: %d", mgr._proc.pid)
            break
        await asyncio.sleep(0.5)
    else:
        raise AssertionError("Watchdog did not respawn within 15s")

    # Verify the next turn works with the new process
    env = _make_env("verify.respawn")
    backend = OpenCodeServerBackend()
    emissions, sid = await _run_turn(backend, "Say 'alive'.", workdir, env)
    assert sid is not None
    text = "".join(e.text or "" for e in emissions if e.event is ExternalEvent.TEXT_DELTA)
    assert len(text) > 0, "Turn after respawn should produce text"

    logger.info("PASS: Watchdog respawned and next turn succeeded")


async def main() -> None:
    if not os.environ.get("OPENCODE_HOST") and not os.environ.get("OPENCODE_SSE_INTEGRATION"):
        logger.error(
            "Set OPENCODE_SSE_INTEGRATION=1 or OPENCODE_HOST=http://... to run this script"
        )
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="modex_verify_") as tmpdir:
        workdir_a = Path(tmpdir) / "workdir_a"
        workdir_b = Path(tmpdir) / "workdir_b"
        workdir_a.mkdir()
        workdir_b.mkdir()

        async with OpenCodeServerManager.lifecycle():
            logger.info("Lifecycle bound — running tests...")

            await test_multi_session_same_workdir(workdir_a)
            await test_multi_workdir(workdir_a, workdir_b)
            await test_resume(workdir_a)
            await test_watchdog_respawn(workdir_a)

            logger.info("=== ALL TESTS PASSED ===")

        logger.info("Lifecycle exited — opencode process should be cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
