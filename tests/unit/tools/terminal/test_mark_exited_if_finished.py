from __future__ import annotations

from modex_agent.tools.terminal.poll_loop import PollOutcome, mark_exited_if_finished
from modex_agent.tools.terminal.process_registry import ProcessRegistry
from modex_agent.tools.terminal.types import ProcessStatus


def _running_session(registry: ProcessRegistry) -> str:
    return registry.create(command="read -p x", terminal="default", cwd=None, pid=None).id


def test_prompt_detected_marks_completed() -> None:
    registry = ProcessRegistry()
    proc_id = _running_session(registry)

    mark_exited_if_finished(registry, proc_id, PollOutcome.PROMPT_DETECTED)

    assert registry.get_running(proc_id) is None
    finished = registry.get_finished(proc_id)
    assert finished is not None
    assert finished.status is ProcessStatus.COMPLETED


def test_process_exit_marks_completed() -> None:
    registry = ProcessRegistry()
    proc_id = _running_session(registry)

    mark_exited_if_finished(registry, proc_id, PollOutcome.PROCESS_EXIT)

    assert registry.get_running(proc_id) is None
    finished = registry.get_finished(proc_id)
    assert finished is not None
    assert finished.status is ProcessStatus.COMPLETED


def test_live_outcomes_keep_session_running() -> None:
    live_outcomes = [
        PollOutcome.INPUT_WAIT,
        PollOutcome.TIMED_OUT,
    ]
    for outcome in live_outcomes:
        registry = ProcessRegistry()
        proc_id = _running_session(registry)

        mark_exited_if_finished(registry, proc_id, outcome)

        assert registry.get_running(proc_id) is not None, (
            f"{outcome} must keep the session RUNNING (interaction still live)"
        )
        assert registry.get_finished(proc_id) is None
