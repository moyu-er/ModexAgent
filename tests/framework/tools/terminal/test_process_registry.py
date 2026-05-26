from __future__ import annotations

import time

from framework.tools.terminal.config import TerminalRuntimeConfig
from framework.tools.terminal.process_registry import ProcessRegistry
from framework.tools.terminal.types import ProcessStatus


def test_append_output_tracks_pending_aggregated_and_tail() -> None:
    registry = ProcessRegistry(config=TerminalRuntimeConfig(max_output_chars=20, pending_max_output_chars=10))
    session = registry.create(command="echo hello", terminal="default", cwd="C:\\repo", pid=123)

    registry.append_output(session.id, "stdout", "hello ")
    registry.append_output(session.id, "stdout", "world")

    drained = registry.drain_pending(session.id)
    current = registry.get_running(session.id)

    assert drained.stdout == "ello world"
    assert drained.stderr == ""
    assert current is not None
    assert current.aggregated == "hello world"
    assert current.tail == "hello world"
    assert current.truncated is True


def test_drain_pending_output_is_not_repeated() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="npm run dev", terminal="web", cwd=None, pid=222)
    registry.append_output(session.id, "stdout", "ready\n")

    first = registry.drain_pending(session.id)
    second = registry.drain_pending(session.id)

    assert first.stdout == "ready\n"
    assert second.stdout == ""


def test_waiting_for_input_is_idle_and_stdin_writable_hint() -> None:
    registry = ProcessRegistry(config=TerminalRuntimeConfig(input_wait_idle_ms=1000))
    session = registry.create(command="ssh host", terminal="remote", cwd=None, pid=333)
    session.stdin_writable = True
    session.last_output_at = time.time() - 2

    runtime = registry.running_runtime(session.id)

    assert runtime is not None
    assert runtime.waiting_for_input is True
    assert runtime.idle_ms >= 1000


def test_mark_exited_moves_session_to_finished() -> None:
    registry = ProcessRegistry()
    session = registry.create(command="python script.py", terminal="default", cwd=None, pid=444)

    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)

    assert registry.get_running(session.id) is None
    assert registry.get_finished(session.id) is not None
    assert registry.get_finished(session.id).status is ProcessStatus.COMPLETED


def test_prune_finished_sessions_removes_expired_records() -> None:
    registry = ProcessRegistry(config=TerminalRuntimeConfig(finished_ttl_ms=10))
    session = registry.create(command="echo done", terminal="default", cwd=None, pid=555)
    registry.mark_exited(session.id, exit_code=0, exit_signal=None, status=ProcessStatus.COMPLETED)

    finished = registry.get_finished(session.id)
    assert finished is not None
    finished.ended_at = time.time() - 1
    registry.prune_finished()

    assert registry.get_finished(session.id) is None
