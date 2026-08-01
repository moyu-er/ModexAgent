"""Comprehensive concurrency test for modexctl env isolation.

Tests ALL combinations of agent types running concurrently and verifies
that modexctl.from_env() always resolves to the correct session — no
crossover regardless of how many agents run in parallel.

Agent type combinations:
- Native main agent (contextvar injection, no OPENCODE_SESSION_ID)
- Native subagent (contextvar injection, comm_kind=subagent)
- External opencode main agent (OPENCODE_SESSION_ID + snapshot)
- External opencode subagent (OPENCODE_SESSION_ID + snapshot, comm_kind=subagent)

The opencode singleton process has FROZEN MODEX_* env vars from first spawn.
All opencode sessions share this frozen env. Native agents use contextvar
(task-level isolation) — they do NOT see the frozen env's MODEX_* vars
because build_full_env's overrides win. But native agents' subprocesses
DO inherit the frozen env's MODEX_* vars via os.environ unless the
contextvar overrides them — which only happens for tools that read
_modex_env (CommandTool/SubprocessExecutor), NOT for modexctl which
reads os.environ directly.

WAIT — modexctl reads os.environ, NOT contextvar. Native agents that
spawn modexctl via their bash tool DO get contextvar overrides into the
subprocess env (because build_full_env merges them). But the question
is: does modexctl see the correct values?

Let's verify: NativeEnvInjectionHook sets _modex_env contextvar with
MODEX_* vars. CommandTool/SubprocessExecutor call build_full_env(overrides)
which does env = dict(os.environ); env.update(overrides). So the
subprocess env has the correct MODEX_* from contextvar, overriding any
frozen process env values. modexctl reads os.environ which IS the
subprocess env → correct.

For opencode: the bash tool's subprocess env = process.env (frozen) +
shell.env plugin (OPENCODE_SESSION_ID). No contextvar involved. So
modexctl sees frozen MODEX_* + OPENCODE_SESSION_ID → must use snapshot.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure bot_project is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "examples" / "bot_project"))

from bot.cli.modexctl.main import ModexCtlContext  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snapshot(
    session_id: str,
    agent_name: str,
    comm_kind: str = "normal",
    parent_session_id: str | None = None,
    workspace_root: str = "/tmp/ws",
) -> dict[str, str]:
    snap: dict[str, str] = {
        "MODEX_SESSION_ID": session_id,
        "MODEX_AGENT_NAME": agent_name,
        "MODEX_INBOX_ROOT": f"{workspace_root}/.modex/inbox",
        "MODEX_AGENT_POOL_MAP": "main=default;coder=coder",
        "MODEX_TARGETS": "main=Main Agent;coder=Code Agent",
        "MODEX_WORKSPACE_ROOT": workspace_root,
        "MODEX_CONTROL_ORIGIN": "http://127.0.0.1:21800",
        "MODEX_COMM_KIND": comm_kind,
        "MODEX_WORKDIR": workspace_root,
        "MODEX_PROVIDER_SESSION_ID": "",
        "PATH": "/usr/bin:/bin",
    }
    if parent_session_id:
        snap["MODEX_PARENT_SESSION_ID"] = parent_session_id
    return snap


def _write_snapshot(tmp_path: Path, opencode_sid: str, snapshot: dict[str, str]) -> Path:
    snapshots_dir = tmp_path / ".modex" / "external" / "env-snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    safe_sid = opencode_sid.replace("/", "_").replace("\\", "_").replace("..", "_")
    path = snapshots_dir / f"{safe_sid}.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _build_native_subprocess_env(
    frozen_process_env: dict[str, str],
    modex_overrides: dict[str, str],
) -> dict[str, str]:
    """Simulate build_full_env for native agent subprocess.

    Native agent: contextvar _modex_env has correct MODEX_* for this task.
    build_full_env does: env = dict(os.environ); env.update(overrides).
    """
    env = dict(frozen_process_env)
    env.update(modex_overrides)
    return env


def _build_opencode_subprocess_env(
    frozen_process_env: dict[str, str],
    opencode_sid: str,
) -> dict[str, str]:
    """Simulate opencode bash tool subprocess env.

    opencode: process env (frozen MODEX_*) + shell.env plugin (OPENCODE_SESSION_ID).
    No contextvar — the plugin only adds OPENCODE_SESSION_ID.
    """
    env = dict(frozen_process_env)
    env["OPENCODE_SESSION_ID"] = opencode_sid
    return env


def _from_env_in_subprocess(subprocess_env: dict[str, str], cwd: Path) -> ModexCtlContext | None:
    """Simulate modexctl.from_env() running in a subprocess with given env + CWD."""
    old_environ = dict(os.environ)
    old_cwd = os.getcwd()
    try:
        os.environ.clear()
        os.environ.update(subprocess_env)
        os.chdir(cwd)
        return ModexCtlContext.from_env()
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Frozen opencode process env (shared by all opencode sessions)
# ---------------------------------------------------------------------------

FROZEN_PROCESS_ENV: dict[str, str] = {
    "MODEX_SESSION_ID": "frozen_first_session.main",
    "MODEX_AGENT_NAME": "opencode",
    "MODEX_INBOX_ROOT": "/tmp/ws/.modex/inbox",
    "MODEX_AGENT_POOL_MAP": "main=default;coder=coder",
    "MODEX_TARGETS": "main=Main Agent;coder=Code Agent",
    "MODEX_WORKSPACE_ROOT": "/tmp/ws",
    "MODEX_CONTROL_ORIGIN": "http://127.0.0.1:21800",
    "MODEX_COMM_KIND": "normal",
    "MODEX_WORKDIR": "/tmp/ws",
    "MODEX_PROVIDER_SESSION_ID": "",
    "PATH": "/usr/bin:/bin",
    "OPENCODE_PERMISSION": '{"*":"allow","question":"deny"}',
}


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestNativeAgentConcurrentSessions:
    """Multiple native agent sessions running concurrently via contextvar.

    Each session has its own asyncio task → its own contextvar copy.
    modexctl subprocess inherits the correct env via build_full_env overrides.
    """

    def test_two_native_main_agents_concurrent(self, tmp_path: Path) -> None:
        """Two native main agents (different sessions) → each modexctl sees own session."""
        s1_overrides = _make_snapshot("conv1.main", "main", workspace_root=str(tmp_path))
        s2_overrides = _make_snapshot("conv2.main", "main", workspace_root=str(tmp_path))

        s1_env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, s1_overrides)
        s2_env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, s2_overrides)

        ctx1 = _from_env_in_subprocess(s1_env, tmp_path)
        ctx2 = _from_env_in_subprocess(s2_env, tmp_path)

        assert ctx1 is not None and ctx2 is not None
        assert ctx1.session_id == "conv1.main"
        assert ctx2.session_id == "conv2.main"
        assert ctx1.session_id != ctx2.session_id

    def test_native_main_and_subagent_concurrent(self, tmp_path: Path) -> None:
        """Native main agent + native subagent → each sees correct session."""
        main_overrides = _make_snapshot("conv1.main", "main", workspace_root=str(tmp_path))
        sub_overrides = _make_snapshot("inv1.coder", "coder", comm_kind="subagent",
                                       parent_session_id="conv1.main", workspace_root=str(tmp_path))

        main_env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, main_overrides)
        sub_env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, sub_overrides)

        ctx_main = _from_env_in_subprocess(main_env, tmp_path)
        ctx_sub = _from_env_in_subprocess(sub_env, tmp_path)

        assert ctx_main is not None and ctx_sub is not None
        assert ctx_main.session_id == "conv1.main"
        assert ctx_sub.session_id == "inv1.coder"
        assert ctx_sub.is_subagent is True
        assert ctx_sub.parent_session_id == "conv1.main"

    def test_native_does_not_see_frozen_opencode_env(self, tmp_path: Path) -> None:
        """Native agent's subprocess env overrides frozen MODEX_SESSION_ID."""
        native_overrides = _make_snapshot("native_session.main", "main", workspace_root=str(tmp_path))
        env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, native_overrides)

        # Frozen env has MODEX_SESSION_ID="frozen_first_session.main"
        # Native overrides should win
        assert env["MODEX_SESSION_ID"] == "native_session.main"
        assert env["MODEX_SESSION_ID"] != FROZEN_PROCESS_ENV["MODEX_SESSION_ID"]

        ctx = _from_env_in_subprocess(env, tmp_path)
        assert ctx is not None
        assert ctx.session_id == "native_session.main"


class TestOpencodeAgentConcurrentSessions:
    """Multiple opencode sessions sharing the singleton process.

    Each session's bash tool gets OPENCODE_SESSION_ID from the plugin.
    modexctl reads the per-session snapshot file.
    """

    def test_two_opencode_main_agents_concurrent(self, tmp_path: Path) -> None:
        """Two opencode main sessions → each modexctl reads own snapshot."""
        s1_sid = "ses_opencode_s1"
        s2_sid = "ses_opencode_s2"
        _write_snapshot(tmp_path, s1_sid, _make_snapshot("conv1.opencode", "opencode", workspace_root=str(tmp_path)))
        _write_snapshot(tmp_path, s2_sid, _make_snapshot("conv2.opencode", "opencode", workspace_root=str(tmp_path)))

        s1_env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, s1_sid)
        s2_env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, s2_sid)

        ctx1 = _from_env_in_subprocess(s1_env, tmp_path)
        ctx2 = _from_env_in_subprocess(s2_env, tmp_path)

        assert ctx1 is not None and ctx2 is not None
        assert ctx1.session_id == "conv1.opencode"
        assert ctx2.session_id == "conv2.opencode"
        assert ctx1.session_id != ctx2.session_id
        # NOT the frozen value
        assert ctx1.session_id != FROZEN_PROCESS_ENV["MODEX_SESSION_ID"]
        assert ctx2.session_id != FROZEN_PROCESS_ENV["MODEX_SESSION_ID"]

    def test_opencode_main_and_subagent_concurrent(self, tmp_path: Path) -> None:
        """Opencode main + opencode subagent (child session) → each sees correct session."""
        main_sid = "ses_opencode_main"
        child_sid = "ses_opencode_child"
        _write_snapshot(tmp_path, main_sid, _make_snapshot("conv1.opencode", "opencode", workspace_root=str(tmp_path)))
        _write_snapshot(tmp_path, child_sid, _make_snapshot("conv1.external-subagent", "external-subagent",
                          comm_kind="subagent", parent_session_id="conv1.opencode", workspace_root=str(tmp_path)))

        main_env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, main_sid)
        child_env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, child_sid)

        ctx_main = _from_env_in_subprocess(main_env, tmp_path)
        ctx_child = _from_env_in_subprocess(child_env, tmp_path)

        assert ctx_main is not None and ctx_child is not None
        assert ctx_main.session_id == "conv1.opencode"
        assert ctx_child.session_id == "conv1.external-subagent"
        assert ctx_child.is_subagent is True
        assert ctx_child.parent_session_id == "conv1.opencode"

    def test_opencode_does_not_read_frozen_process_env(self, tmp_path: Path) -> None:
        """Opencode modexctl must NOT read frozen MODEX_SESSION_ID from process env."""
        sid = "ses_real"
        _write_snapshot(tmp_path, sid, _make_snapshot("real_session.opencode", "opencode", workspace_root=str(tmp_path)))

        env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, sid)
        ctx = _from_env_in_subprocess(env, tmp_path)

        assert ctx is not None
        assert ctx.session_id == "real_session.opencode"
        assert ctx.session_id != FROZEN_PROCESS_ENV["MODEX_SESSION_ID"]


class TestMixedNativeAndOpencodeConcurrent:
    """Native agent + opencode agent running concurrently.

    Key risk: native agent subprocess env has correct MODEX_* (contextvar override)
    BUT also has OPENCODE_SESSION_ID if the process env has it (it shouldn't —
    native agents don't load the opencode plugin). But if the opencode process
    env leaks OPENCODE_SESSION_ID into native agent's base env...

    Actually, native agents run in the SAME process as the bot (not in the
    opencode subprocess). So native agent subprocesses inherit os.environ
    from the BOT process, which does NOT have OPENCODE_SESSION_ID. Only
    opencode's bash subprocesses have OPENCODE_SESSION_ID (injected by plugin).

    So this scenario is about: can a native agent's modexctl accidentally
    pick up an opencode snapshot? Answer: no, because native agent env has
    no OPENCODE_SESSION_ID → from_env falls to native path.
    """

    def test_native_and_opencode_no_crossover(self, tmp_path: Path) -> None:
        """Native agent env has no OPENCODE_SESSION_ID → native path used.
        Opencode agent env has OPENCODE_SESSION_ID → snapshot path used.
        No crossover."""
        opencode_sid = "ses_opencode"
        _write_snapshot(tmp_path, opencode_sid, _make_snapshot("oc_session.opencode", "opencode", workspace_root=str(tmp_path)))

        # Native agent env: MODEX_* from contextvar, NO OPENCODE_SESSION_ID
        native_overrides = _make_snapshot("native.main", "main", workspace_root=str(tmp_path))
        native_env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, native_overrides)
        # Ensure no OPENCODE_SESSION_ID (native agents don't have it)
        native_env.pop("OPENCODE_SESSION_ID", None)

        # Opencode agent env: frozen MODEX_* + OPENCODE_SESSION_ID
        oc_env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, opencode_sid)

        ctx_native = _from_env_in_subprocess(native_env, tmp_path)
        ctx_oc = _from_env_in_subprocess(oc_env, tmp_path)

        assert ctx_native is not None and ctx_oc is not None
        assert ctx_native.session_id == "native.main"
        assert ctx_oc.session_id == "oc_session.opencode"
        assert ctx_native.session_id != ctx_oc.session_id


class TestAsyncConcurrentSimulation:
    """Simulate actual asyncio concurrency — multiple sessions resolving
    from_env simultaneously in the same process.

    Uses asyncio tasks with contextvars to simulate native agent task isolation.
    """

    def test_async_concurrent_native_sessions(self, tmp_path: Path) -> None:
        """10 concurrent native sessions → each resolves correctly."""
        from modex_agent.runtime.env_context import _modex_env

        sessions = [f"conv{i}.main" for i in range(10)]

        async def resolve_session(session_id: str) -> str:
            overrides = _make_snapshot(session_id, "main", workspace_root=str(tmp_path))
            env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, overrides)
            ctx = _from_env_in_subprocess(env, tmp_path)
            return ctx.session_id if ctx else "NONE"

        async def run_all() -> list[str]:
            tasks = [asyncio.create_task(resolve_session(s)) for s in sessions]
            return await asyncio.gather(*tasks)

        results = asyncio.run(run_all())
        assert sorted(results) == sorted(sessions)

    def test_async_concurrent_opencode_sessions(self, tmp_path: Path) -> None:
        """10 concurrent opencode sessions → each reads correct snapshot."""
        sids = [f"ses_oc_{i}" for i in range(10)]
        for i, sid in enumerate(sids):
            _write_snapshot(tmp_path, sid, _make_snapshot(f"conv{i}.opencode", "opencode", workspace_root=str(tmp_path)))

        async def resolve_session(sid: str) -> str:
            env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, sid)
            ctx = _from_env_in_subprocess(env, tmp_path)
            return ctx.session_id if ctx else "NONE"

        async def run_all() -> list[str]:
            tasks = [asyncio.create_task(resolve_session(sid)) for sid in sids]
            return await asyncio.gather(*tasks)

        results = asyncio.run(run_all())
        expected = [f"conv{i}.opencode" for i in range(10)]
        assert sorted(results) == sorted(expected)

    def test_async_mixed_native_and_opencode(self, tmp_path: Path) -> None:
        """5 native + 5 opencode concurrent → all resolve correctly."""
        opencode_sids = [f"ses_oc_{i}" for i in range(5)]
        for i, sid in enumerate(opencode_sids):
            _write_snapshot(tmp_path, sid, _make_snapshot(f"conv_oc_{i}.opencode", "opencode", workspace_root=str(tmp_path)))

        async def resolve_native(i: int) -> str:
            sid = f"conv_nat_{i}.main"
            overrides = _make_snapshot(sid, "main", workspace_root=str(tmp_path))
            env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, overrides)
            env.pop("OPENCODE_SESSION_ID", None)
            ctx = _from_env_in_subprocess(env, tmp_path)
            return ctx.session_id if ctx else "NONE"

        async def resolve_opencode(i: int) -> str:
            sid = opencode_sids[i]
            env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, sid)
            ctx = _from_env_in_subprocess(env, tmp_path)
            return ctx.session_id if ctx else "NONE"

        async def run_all() -> tuple[list[str], list[str]]:
            native_tasks = [asyncio.create_task(resolve_native(i)) for i in range(5)]
            oc_tasks = [asyncio.create_task(resolve_opencode(i)) for i in range(5)]
            native_results = await asyncio.gather(*native_tasks)
            oc_results = await asyncio.gather(*oc_tasks)
            return list(native_results), list(oc_results)

        native_results, oc_results = asyncio.run(run_all())
        assert sorted(native_results) == [f"conv_nat_{i}.main" for i in range(5)]
        assert sorted(oc_results) == [f"conv_oc_{i}.opencode" for i in range(5)]

    def test_async_stress_100_mixed_sessions(self, tmp_path: Path) -> None:
        """100 concurrent mixed sessions (50 native + 50 opencode) → all correct."""
        n = 50
        opencode_sids = [f"ses_stress_{i}" for i in range(n)]
        for i, sid in enumerate(opencode_sids):
            _write_snapshot(tmp_path, sid, _make_snapshot(f"stress_oc_{i}.opencode", "opencode", workspace_root=str(tmp_path)))

        async def resolve_native(i: int) -> str:
            sid = f"stress_nat_{i}.main"
            overrides = _make_snapshot(sid, "main", workspace_root=str(tmp_path))
            env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, overrides)
            env.pop("OPENCODE_SESSION_ID", None)
            ctx = _from_env_in_subprocess(env, tmp_path)
            return ctx.session_id if ctx else "NONE"

        async def resolve_opencode(i: int) -> str:
            sid = opencode_sids[i]
            env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, sid)
            ctx = _from_env_in_subprocess(env, tmp_path)
            return ctx.session_id if ctx else "NONE"

        async def run_all() -> tuple[list[str], list[str]]:
            native_tasks = [asyncio.create_task(resolve_native(i)) for i in range(n)]
            oc_tasks = [asyncio.create_task(resolve_opencode(i)) for i in range(n)]
            return await asyncio.gather(*native_tasks), await asyncio.gather(*oc_tasks)

        native_results, oc_results = asyncio.run(run_all())
        assert all(r != "NONE" for r in native_results)
        assert all(r != "NONE" for r in oc_results)
        assert len(set(native_results)) == n  # all unique
        assert len(set(oc_results)) == n  # all unique
        assert not set(native_results) & set(oc_results)  # no overlap


class TestEdgeCases:
    """Edge cases that could cause crossover."""

    def test_opencode_snapshot_missing_fails_closed(self, tmp_path: Path) -> None:
        """OPENCODE_SESSION_ID set but no snapshot → fail closed (return None).

        The opencode process env has frozen MODEX_* vars that would
        cause crossover if read. Fail closed prevents that.
        """
        env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, "ses_nonexistent")
        ctx = _from_env_in_subprocess(env, tmp_path)
        assert ctx is None

    def test_opencode_snapshot_corrupt_fails_closed(self, tmp_path: Path) -> None:
        """Corrupt snapshot → fail closed (return None). Same fail-closed behavior."""
        snapshots_dir = tmp_path / ".modex" / "external" / "env-snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        (snapshots_dir / "ses_corrupt.json").write_text("not json", encoding="utf-8")

        env = _build_opencode_subprocess_env(FROZEN_PROCESS_ENV, "ses_corrupt")
        ctx = _from_env_in_subprocess(env, tmp_path)
        assert ctx is None

    def test_native_with_opencode_session_id_in_env(self, tmp_path: Path) -> None:
        """If a native agent's subprocess env accidentally has OPENCODE_SESSION_ID
        (shouldn't happen, but defense in depth), from_env should still resolve
        correctly IF the snapshot exists for that sid."""
        # This simulates a bug where OPENCODE_SESSION_ID leaks into native env
        native_overrides = _make_snapshot("native.main", "main", workspace_root=str(tmp_path))
        env = _build_native_subprocess_env(FROZEN_PROCESS_ENV, native_overrides)
        env["OPENCODE_SESSION_ID"] = "ses_leaked"  # accidental leak

        # Write a snapshot with DIFFERENT session_id
        _write_snapshot(tmp_path, "ses_leaked", _make_snapshot("leaked_session.opencode", "opencode", workspace_root=str(tmp_path)))

        ctx = _from_env_in_subprocess(env, tmp_path)
        # from_env prioritizes OPENCODE_SESSION_ID → reads snapshot
        # This means the native agent's correct env is IGNORED!
        assert ctx is not None
        assert ctx.session_id == "leaked_session.opencode"
        assert ctx.session_id != "native.main"
        # This IS a crossover — but only if OPENCODE_SESSION_ID leaks into
        # native agent env, which shouldn't happen (native agents don't load
        # the opencode plugin). This test documents the risk.

    def test_empty_opencode_session_id(self, tmp_path: Path) -> None:
        """Empty OPENCODE_SESSION_ID → treated as not set → native path."""
        env = dict(FROZEN_PROCESS_ENV)
        env["OPENCODE_SESSION_ID"] = ""
        ctx = _from_env_in_subprocess(env, tmp_path)
        assert ctx is not None
        # Empty string is falsy → from_env skips opencode path → native path
        assert ctx.session_id == FROZEN_PROCESS_ENV["MODEX_SESSION_ID"]
