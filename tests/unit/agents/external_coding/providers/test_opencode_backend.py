"""Unit tests for ``OpenCodeBackend`` (T7).

Every test uses a :class:`FakeProcess` double — no real ``opencode``
binary is ever spawned. The OS-layer functions (``resolve_executable``,
``spawn_process_group``) are monkey-patched so the backend module
calls the test fakes instead of the real subprocess layer.

Coverage shape:

- ABC adherence (:class:`StreamingProviderBackend`).
- ``_build_args`` — flag ordering for every ExecOptions combination.
- Full ``execute_streaming`` — stdout parsing, emission forwarding,
  ``PWD`` injection, stdin closing, returncode mapping.
- Session-id capture from the parser's out-of-band channel.
- Stale-session detection (stderr-driven).
- ``@pytest.mark.manual`` smoke-test documentation for operators.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from modex_agent.agents.external_coding import (
    Emission,
    ExecOptions,
    ExternalCodingEvent,
)
from modex_agent.agents.external_coding.agent import (
    StaleSessionError,
    StreamingProviderBackend,
)
from modex_agent.agents.external_coding.os_layer import ResolvedExecutable
from modex_agent.agents.external_coding.providers.opencode_backend import (
    OpenCodeBackend,
)

# ---------------------------------------------------------------------------
# Fake subprocess doubles
# ---------------------------------------------------------------------------


class _FakeStdout:
    """Mimics ``asyncio.StreamReader`` for ``async for line in proc.stdout``."""

    def __init__(self, lines: list[str]) -> None:
        self._lines: list[bytes] = [line.encode("utf-8") for line in lines]

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    """Mimics ``asyncio.StreamReader.read()`` for stderr capture."""

    def __init__(self, data: str | bytes = b"") -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data


class FakeProcess:
    """Minimal double for ``asyncio.subprocess.Process``."""

    def __init__(
        self,
        stdout_lines: list[str] | None = None,
        stderr: str | bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeStdout(stdout_lines or [])
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


class _BlockedStdout:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.released = asyncio.Event()

    def __aiter__(self) -> _BlockedStdout:
        return self

    async def __anext__(self) -> bytes:
        self.started.set()
        await self.released.wait()
        raise StopAsyncIteration


class _ActiveFakeProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.stdout = _BlockedStdout()
        self.returncode: int | None = None
        self.group_terminated = False
        self.reaped = False

    async def terminate_group(self) -> None:
        self.group_terminated = True
        self.returncode = -15
        self.stdout.released.set()
        await self.wait()

    async def wait(self) -> int:
        await self.stdout.released.wait()
        self.reaped = True
        assert self.returncode is not None
        return self.returncode


def _make_collector() -> tuple[list[Emission], Callable[[Emission], Awaitable[None]]]:
    """Return ``(sink, callback)`` — callback appends each emission to sink."""
    sink: list[Emission] = []

    async def _collect(emission: Emission) -> None:
        sink.append(emission)

    return sink, _collect


def _oc_line(payload: dict[str, object]) -> str:
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_os_layer(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch ``resolve_executable`` + ``spawn_process_group`` in opencode_backend.

    Returns a state dict. Tests set ``state["process"]`` to a
    :class:`FakeProcess` before calling ``execute_streaming``. The
    dict also captures the spawn call's ``args``, ``cwd``, ``env``,
    and ``stdin`` for post-call assertions.
    """
    state: dict[str, object] = {}

    def fake_resolve(
        name: str,
        logger: object = None,
    ) -> ResolvedExecutable:
        return ResolvedExecutable(argv0=name)

    async def fake_spawn(
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        stdin: int | None,
    ) -> FakeProcess:
        state["args"] = list(args)
        state["cwd"] = cwd
        state["env"] = dict(env)
        state["stdin"] = stdin
        proc = state.get("process")
        if proc is None:
            raise RuntimeError(
                "Test setup error: set state['process'] before calling execute_streaming"
            )
        assert isinstance(proc, FakeProcess)
        return proc

    monkeypatch.setattr(
        "modex_agent.agents.external_coding.providers.opencode_backend.resolve_executable",
        fake_resolve,
    )
    monkeypatch.setattr(
        "modex_agent.agents.external_coding.providers.opencode_backend.spawn_process_group",
        fake_spawn,
    )
    return state


# ---------------------------------------------------------------------------
# ABC adherence
# ---------------------------------------------------------------------------


class TestOpenCodeBackendABC:
    def test_is_streaming_provider_backend(self) -> None:
        assert isinstance(OpenCodeBackend(), StreamingProviderBackend)


# ---------------------------------------------------------------------------
# _build_args
# ---------------------------------------------------------------------------


class TestOpenCodeBackendBuildArgs:
    """``_build_args`` — flag ordering for every ExecOptions combination."""

    def test_minimal_args(self, tmp_path: Path) -> None:
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="hello", workdir=tmp_path)
        args = backend._build_args(opts)
        assert args == [
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            "--thinking",
            "--dir",
            str(tmp_path),
            "hello",
        ]

    def test_args_with_model(self, tmp_path: Path) -> None:
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="hello", workdir=tmp_path, model="claude-3.5")
        args = backend._build_args(opts)
        assert "--model" in args
        assert args[args.index("--model") + 1] == "claude-3.5"

    def test_system_prompt_is_not_passed_as_flag(self, tmp_path: Path) -> None:
        backend = OpenCodeBackend()
        opts = ExecOptions(
            prompt="hello",
            workdir=tmp_path,
            system_prompt="You are a coder.",
        )
        args = backend._build_args(opts)
        assert "--prompt" not in args
        assert "You are a coder." not in args

    def test_args_with_resume_session(self, tmp_path: Path) -> None:
        backend = OpenCodeBackend()
        opts = ExecOptions(
            prompt="hello",
            workdir=tmp_path,
            resume_session_id="oc-sess-42",
        )
        args = backend._build_args(opts)
        assert "--session" in args
        idx = args.index("--session")
        assert args[idx + 1] == "oc-sess-42"

    def test_args_full_combination(self, tmp_path: Path) -> None:
        backend = OpenCodeBackend()
        opts = ExecOptions(
            prompt="do the thing",
            workdir=tmp_path,
            model="claude-3.5",
            system_prompt="Be helpful.",
            resume_session_id="oc-sess-1",
        )
        args = backend._build_args(opts)
        assert args == [
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            "--thinking",
            "--dir",
            str(tmp_path),
            "--model",
            "claude-3.5",
            "--session",
            "oc-sess-1",
            "do the thing",
        ]

    def test_prompt_is_last_positional(self, tmp_path: Path) -> None:
        backend = OpenCodeBackend()
        opts = ExecOptions(
            prompt="FINAL_PROMPT",
            workdir=tmp_path,
            model="m",
            system_prompt="s",
            resume_session_id="r",
        )
        args = backend._build_args(opts)
        assert args[-1] == "FINAL_PROMPT"


# ---------------------------------------------------------------------------
# execute_streaming — full lifecycle
# ---------------------------------------------------------------------------


class TestOpenCodeBackendExecute:
    @pytest.mark.asyncio
    async def test_execute_is_rejected_after_close(
        self,
        tmp_path: Path,
        mock_os_layer: dict[str, object],
    ) -> None:
        # Given a backend whose lifetime has ended.
        backend = OpenCodeBackend()
        await backend.close()

        # When execution is requested after close, then no child is spawned.
        with pytest.raises(RuntimeError, match="closed"):
            await backend.execute_streaming(
                ExecOptions(prompt="x", workdir=tmp_path),
                {},
                _make_collector()[1],
            )

    @pytest.mark.asyncio
    async def test_close_waits_for_blocked_spawn_to_be_owned_and_terminated(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Given an execution whose child spawn has started but cannot return yet.
        process = _ActiveFakeProcess()
        spawn_started = asyncio.Event()
        release_spawn = asyncio.Event()
        close_started = asyncio.Event()
        close_finished = asyncio.Event()

        async def blocked_spawn(
            args: list[str],
            cwd: Path,
            env: dict[str, str],
            stdin: int | None,
        ) -> FakeProcess:
            spawn_started.set()
            await release_spawn.wait()
            return process

        async def terminate_process(proc: FakeProcess) -> None:
            assert proc is process
            await process.terminate_group()

        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.resolve_executable",
            lambda name, logger=None: ResolvedExecutable(argv0=name),
        )
        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.spawn_process_group",
            blocked_spawn,
        )
        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.terminate_process_group",
            terminate_process,
        )
        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend._safe_terminate",
            terminate_process,
        )
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        execution = asyncio.create_task(
            backend.execute_streaming(opts, {}, _make_collector()[1])
        )
        await spawn_started.wait()

        async def close_backend() -> None:
            close_started.set()
            await backend.close()
            close_finished.set()

        # When close starts while spawn is still blocked.
        closing = asyncio.create_task(close_backend())
        await close_started.wait()

        try:
            # Then close cannot report success before the child is owned and terminated.
            assert not close_finished.is_set()
        finally:
            release_spawn.set()
            await process.stdout.started.wait()
            await closing
            if not execution.done():
                execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution

    @pytest.mark.asyncio
    async def test_close_waits_for_all_terminations_and_retries_failed_owner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Given two active children, one failing termination and one blocked termination.
        failed_process = _ActiveFakeProcess()
        settling_process = _ActiveFakeProcess()
        processes = [failed_process, settling_process]
        failed_attempts = 0
        failure_observed = asyncio.Event()
        sibling_termination_started = asyncio.Event()
        release_sibling_termination = asyncio.Event()
        sibling_termination_finished = asyncio.Event()
        close_finished = asyncio.Event()
        close_errors: list[RuntimeError] = []

        async def spawn_next(
            args: list[str],
            cwd: Path,
            env: dict[str, str],
            stdin: int | None,
        ) -> FakeProcess:
            return processes.pop(0)

        async def terminate_process(proc: FakeProcess) -> None:
            nonlocal failed_attempts
            if proc is failed_process:
                failed_attempts += 1
                if failed_attempts == 1:
                    failure_observed.set()
                    raise RuntimeError("tree termination failed")
                await failed_process.terminate_group()
                return
            assert proc is settling_process
            sibling_termination_started.set()
            await release_sibling_termination.wait()
            await settling_process.terminate_group()
            sibling_termination_finished.set()

        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.resolve_executable",
            lambda name, logger=None: ResolvedExecutable(argv0=name),
        )
        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.spawn_process_group",
            spawn_next,
        )
        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.terminate_process_group",
            terminate_process,
        )
        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend._safe_terminate",
            terminate_process,
        )
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        executions = [
            asyncio.create_task(backend.execute_streaming(opts, {}, _make_collector()[1]))
            for _ in processes
        ]
        await asyncio.gather(
            failed_process.stdout.started.wait(),
            settling_process.stdout.started.wait(),
        )

        async def close_backend() -> None:
            try:
                await backend.close()
            except RuntimeError as exc:
                close_errors.append(exc)
            finally:
                close_finished.set()

        # When close sees one termination fail while the sibling remains unsettled.
        closing = asyncio.create_task(close_backend())
        await asyncio.gather(failure_observed.wait(), sibling_termination_started.wait())
        returned_before_sibling_settled = close_finished.is_set()
        release_sibling_termination.set()
        await sibling_termination_finished.wait()
        await closing
        await backend.close()

        try:
            # Then close waits for the sibling and retains the failed owner for retry.
            assert not returned_before_sibling_settled
            assert len(close_errors) == 1
            assert failed_attempts == 2
            assert failed_process.group_terminated
            assert settling_process.group_terminated
        finally:
            for execution in executions:
                if not execution.done():
                    execution.cancel()
            await asyncio.gather(*executions, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_cancellation_terminates_and_reaps_active_process_group(
        self,
        tmp_path: Path,
        mock_os_layer: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Given an execution blocked while reading from an active child.
        process = _ActiveFakeProcess()
        mock_os_layer["process"] = process

        async def fake_safe_terminate(proc: FakeProcess) -> None:
            assert proc is process
            await process.terminate_group()

        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend._safe_terminate",
            fake_safe_terminate,
        )
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        execution = asyncio.create_task(
            backend.execute_streaming(opts, {}, _make_collector()[1])
        )
        await process.stdout.started.wait()

        # When the active execution is cancelled.
        execution.cancel()

        # Then cancellation propagates after the process group is terminated and reaped.
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert process.group_terminated
        assert process.reaped

    @pytest.mark.asyncio
    async def test_close_terminates_and_reaps_active_execution_child(
        self,
        tmp_path: Path,
        mock_os_layer: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Given a backend that owns an active per-turn child.
        process = _ActiveFakeProcess()
        mock_os_layer["process"] = process

        async def fake_terminate_process_group(proc: FakeProcess) -> None:
            assert proc is process
            await process.terminate_group()

        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.terminate_process_group",
            fake_terminate_process_group,
        )
        backend: StreamingProviderBackend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        execution = asyncio.create_task(
            backend.execute_streaming(opts, {}, _make_collector()[1])
        )
        await process.stdout.started.wait()

        try:
            # When the shared backend ownership contract is closed.
            await backend.close()

            # Then its active execution child is terminated and reaped.
            assert process.group_terminated
            assert process.reaped
        finally:
            if not execution.done():
                execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution

    @pytest.mark.asyncio
    async def test_close_failure_keeps_active_child_owned_for_retry(
        self,
        tmp_path: Path,
        mock_os_layer: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Given an active child whose first tree-termination attempt fails.
        process = _ActiveFakeProcess()
        mock_os_layer["process"] = process
        attempts = 0

        async def fail_once(proc: FakeProcess) -> None:
            nonlocal attempts
            assert proc is process
            attempts += 1
            if attempts == 1:
                raise RuntimeError("tree termination failed")
            await process.terminate_group()

        monkeypatch.setattr(
            "modex_agent.agents.external_coding.providers.opencode_backend.terminate_process_group",
            fail_once,
        )
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        execution = asyncio.create_task(
            backend.execute_streaming(opts, {}, _make_collector()[1])
        )
        await process.stdout.started.wait()

        try:
            # When close cannot terminate the owned process tree.
            with pytest.raises(RuntimeError, match="tree termination failed"):
                await backend.close()

            # Then a later close retries the still-owned child.
            await backend.close()
            assert attempts == 2
            assert process.group_terminated
            assert process.reaped
        finally:
            if not execution.done():
                execution.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await execution

    @pytest.mark.asyncio
    async def test_completed_with_emissions(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        lines = [
            _oc_line({"type": "step_start", "session_id": "oc-sid-1", "step_id": "s1"}),
            _oc_line({"type": "text", "content": "hello world"}),
            _oc_line({"type": "step_finish", "step_id": "s1"}),
        ]
        mock_os_layer["process"] = FakeProcess(stdout_lines=lines, returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="hello", workdir=tmp_path)
        sink, callback = _make_collector()

        result = await backend.execute_streaming(opts, {}, callback)

        assert result.status == "completed"
        assert result.error is None
        # One TEXT_DELTA from the "text" event.
        assert len(sink) == 1
        assert sink[0].event is ExternalCodingEvent.TEXT_DELTA
        assert sink[0].text == "hello world"

    @pytest.mark.asyncio
    async def test_stdin_closed_immediately(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert mock_os_layer["stdin"] == asyncio.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_cwd_set_to_workdir(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert mock_os_layer["cwd"] == tmp_path

    @pytest.mark.asyncio
    async def test_full_args_includes_resolved_argv0(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="hi", workdir=tmp_path, model="m1")
        await backend.execute_streaming(opts, {}, _make_collector()[1])
        args = mock_os_layer["args"]
        assert isinstance(args, list)
        assert args[0] == "opencode"
        assert "run" in args

    @pytest.mark.asyncio
    async def test_empty_stdout_completes(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(stdout_lines=[], returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        sink, callback = _make_collector()
        result = await backend.execute_streaming(opts, {}, callback)
        assert result.status == "completed"
        assert sink == []

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_failed(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(stdout_lines=[], stderr=b"crash", returncode=1)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.status == "failed"
        assert result.error == "crash"


# ---------------------------------------------------------------------------
# PWD injection
# ---------------------------------------------------------------------------


class TestOpenCodeBackendPwdInjection:
    @pytest.mark.asyncio
    async def test_pwd_injected_into_env(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        await backend.execute_streaming(opts, {}, _make_collector()[1])
        env = mock_os_layer["env"]
        assert isinstance(env, dict)
        assert env["PWD"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_pwd_overrides_existing_env_value(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        await backend.execute_streaming(opts, {"PWD": "/old/path"}, _make_collector()[1])
        env = mock_os_layer["env"]
        assert isinstance(env, dict)
        assert env["PWD"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_other_env_vars_preserved(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        env_in = {"MODEX_TARGETS": "a:b", "PATH": "/usr/bin"}
        await backend.execute_streaming(opts, env_in, _make_collector()[1])
        env = mock_os_layer["env"]
        assert isinstance(env, dict)
        assert env["MODEX_TARGETS"] == "a:b"
        assert env["PATH"] == "/usr/bin"
        assert env["PWD"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_env_not_mutated_in_place(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        original_env = {"EXISTING": "1"}
        await backend.execute_streaming(opts, original_env, _make_collector()[1])
        # The caller's dict must not gain a PWD key.
        assert "PWD" not in original_env


# ---------------------------------------------------------------------------
# Session-id capture from parser
# ---------------------------------------------------------------------------


class TestOpenCodeBackendSessionIdCapture:
    @pytest.mark.asyncio
    async def test_session_id_from_first_event(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        lines = [
            _oc_line({"type": "step_start", "session_id": "oc-minted-42", "step_id": "s"}),
            _oc_line({"type": "text", "content": "hi"}),
        ]
        mock_os_layer["process"] = FakeProcess(stdout_lines=lines, returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.session_id == "oc-minted-42"

    @pytest.mark.asyncio
    async def test_session_id_none_when_not_emitted(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        lines = [
            _oc_line({"type": "step_start", "step_id": "s"}),
            _oc_line({"type": "text", "content": "hi"}),
        ]
        mock_os_layer["process"] = FakeProcess(stdout_lines=lines, returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.session_id is None

    @pytest.mark.asyncio
    async def test_first_session_id_wins(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        lines = [
            _oc_line({"type": "step_start", "session_id": "oc-first", "step_id": "s1"}),
            _oc_line({"type": "text", "session_id": "oc-second", "content": "hi"}),
        ]
        mock_os_layer["process"] = FakeProcess(stdout_lines=lines, returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.session_id == "oc-first"

    @pytest.mark.asyncio
    async def test_session_id_from_text_event(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        lines = [
            _oc_line({"type": "step_start", "step_id": "s"}),
            _oc_line({"type": "text", "session_id": "oc-from-text", "content": "hi"}),
        ]
        mock_os_layer["process"] = FakeProcess(stdout_lines=lines, returncode=0)
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.session_id == "oc-from-text"


# ---------------------------------------------------------------------------
# Stale-session detection
# ---------------------------------------------------------------------------


class TestOpenCodeBackendStaleSession:
    @pytest.mark.asyncio
    async def test_stale_session_in_stderr_raises(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="Error: session not found: oc-old",
            returncode=1,
        )
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        with pytest.raises(StaleSessionError, match="stale"):
            await backend.execute_streaming(opts, {}, _make_collector()[1])

    @pytest.mark.asyncio
    async def test_non_stale_error_does_not_raise_stale(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="Error: rate limited",
            returncode=1,
        )
        backend = OpenCodeBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.status == "failed"
        assert "rate limited" in (result.error or "")

    def test_is_stale_session_patterns(self) -> None:
        from modex_agent.agents.external_coding.agent import _is_stale_session

        assert _is_stale_session("session does not exist")
        assert _is_stale_session("SESSION EXPIRED")
        assert not _is_stale_session("all good")


# ---------------------------------------------------------------------------
# Manual smoke test (operator-run, not CI)
# ---------------------------------------------------------------------------


class TestOpenCodeBackendManualSmoke:
    """``@pytest.mark.manual`` — documents the real-CLI verification path.

    These tests are always skipped in CI. An operator runs them
    manually after installing ``opencode`` on PATH to verify the
    backend against a real CLI.
    """

    @pytest.mark.manual
    def test_opencode_real_cli_smoke_documentation(self, tmp_path: Path) -> None:
        """SMOKE TEST — real OpenCode CLI verification (operator-run).

        Prerequisites:
        1. ``opencode`` installed and on ``PATH``.
        2. A writable workdir for the session.

        Steps:
        1. Construct ``OpenCodeBackend()``.
        2. Build ``ExecOptions(prompt="hello", workdir=<tmp>)``.
        3. Call ``await backend.execute_streaming(opts, env, on_emission)``
           with a collector callback.
        4. Verify the backend returns ``BackendResult(status="completed")``.
        5. Verify ``BackendResult.session_id`` is non-None (OpenCode
           mints and reports it in the first event).
        6. Verify at least one ``TEXT_DELTA`` emission was forwarded.

        Run with::

            pytest -m manual tests/unit/agents/external_coding/providers/test_opencode_backend.py -s
        """
        pytest.skip(
            "Manual smoke test — requires real 'opencode' CLI on PATH. "
            "Run with: pytest -m manual ..."
        )
