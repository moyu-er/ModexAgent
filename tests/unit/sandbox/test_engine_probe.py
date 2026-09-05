"""Tests for sandbox engine probes (Ticket 03).

CLI existence/version probing for bwrap / sandbox-exec / docker / podman with
a per-process result cache. Probes are async and use ``shutil.which`` +
``subprocess`` version checks; tests monkeypatch both layers. ``probe_bwrap``
additionally runs a real sandbox smoke after the version check (userns/mount
proof) — tests monkeypatch the smoke seam the same way, plus one test that
runs the real seams against a patched ``subprocess.run`` to prove the event
loop stays responsive while a CLI check is in flight.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from modex_agent.sandbox import engine_probe
from modex_agent.sandbox.engine_probe import (
    clear_probe_cache,
    probe_bwrap,
    probe_docker,
    probe_podman,
    probe_seatbelt,
)


class FakeVersionRunner:
    """Callable stand-in for an async CLI seam (version or smoke).

    Records the argv it was asked to run and returns a canned
    ``(returncode, output)`` per executable name.
    """

    def __init__(self, results: dict[str, tuple[int, str]]) -> None:
        self.results = results
        self.calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        return self.results.get(argv[0], (0, ""))


class ExplodingVersionRunner(FakeVersionRunner):
    """CLI seam that raises like a hung or unspawnable CLI."""

    def __init__(
        self,
        error: Exception,
        results: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        super().__init__(results or {})
        self.error = error

    async def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        raise self.error


@pytest.fixture
def patch_probe_layers(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., FakeVersionRunner]:
    """Patch shutil.which + the version-run and smoke seams; returns the version runner.

    ``smoke_results`` accepts the same shapes as ``version_results`` and
    defaults to a succeeding smoke so bwrap tests control the smoke leg too.
    """

    def _patch(
        which_map: dict[str, str | None],
        version_results: dict[str, tuple[int, str]] | FakeVersionRunner,
        smoke_results: dict[str, tuple[int, str]] | FakeVersionRunner | None = None,
    ) -> FakeVersionRunner:
        monkeypatch.setattr(
            "modex_agent.sandbox.engine_probe._which", lambda name: which_map.get(name)
        )
        if isinstance(version_results, FakeVersionRunner):
            runner = version_results
        else:
            runner = FakeVersionRunner(version_results)
        monkeypatch.setattr("modex_agent.sandbox.engine_probe._run_version", runner)
        smoke_runner = (
            smoke_results
            if isinstance(smoke_results, FakeVersionRunner)
            else FakeVersionRunner(smoke_results or {})
        )
        monkeypatch.setattr("modex_agent.sandbox.engine_probe._run_smoke", smoke_runner)
        return runner

    return _patch


class TestProbeNonBlocking:
    """CLI subprocesses run off the event loop — the loop stays responsive."""

    async def test_version_check_does_not_block_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow CLI check must not starve the loop while it runs."""

        def slow_run(argv: list[str] | str, **kwargs: object) -> subprocess.CompletedProcess[str]:
            time.sleep(0.3)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="Docker version 29.1.2", stderr=""
            )

        monkeypatch.setattr(engine_probe.subprocess, "run", slow_run)
        monkeypatch.setattr(engine_probe, "_which", lambda name: "/usr/bin/docker")

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.02)

        probe_task = asyncio.create_task(probe_docker())
        ticker_task = asyncio.create_task(ticker())
        try:
            result = await asyncio.wait_for(probe_task, timeout=10)
        finally:
            ticker_task.cancel()

        assert result.available is True
        assert ticks >= 5, f"event loop starved during CLI check: only {ticks} ticks"


class TestProbeAvailable:
    """Each probe reports availability when the CLI exists and versions OK."""

    async def test_bwrap_available(self, patch_probe_layers: Any) -> None:
        patch_probe_layers(
            {"bwrap": "/usr/bin/bwrap"},
            {"bwrap": (0, "bwrap 0.10.0")},
        )
        result = await probe_bwrap()
        assert result.available is True
        assert "0.10.0" in result.detail

    async def test_seatbelt_available(self, patch_probe_layers: Any) -> None:
        patch_probe_layers(
            {"sandbox-exec": "/usr/bin/sandbox-exec"},
            {"sandbox-exec": (0, "sandbox-exec 1")},
        )
        result = await probe_seatbelt()
        assert result.available is True

    async def test_docker_available(self, patch_probe_layers: Any) -> None:
        patch_probe_layers(
            {"docker": "/usr/bin/docker"},
            {"docker": (0, "Docker version 29.1.2")},
        )
        result = await probe_docker()
        assert result.available is True
        assert "29.1.2" in result.detail

    async def test_podman_available(self, patch_probe_layers: Any) -> None:
        patch_probe_layers(
            {"podman": "/usr/bin/podman"},
            {"podman": (0, "podman version 5.0.0")},
        )
        result = await probe_podman()
        assert result.available is True


class TestProbeUnavailable:
    """Missing CLI or failing version check reports unavailable with detail."""

    async def test_bwrap_missing(self, patch_probe_layers: Any) -> None:
        runner = patch_probe_layers({}, {})
        result = await probe_bwrap()
        assert result.available is False
        assert result.detail != ""
        assert runner.calls == []  # no subprocess spawn when which() misses

    async def test_docker_missing(self, patch_probe_layers: Any) -> None:
        patch_probe_layers({}, {})
        result = await probe_docker()
        assert result.available is False

    async def test_podman_missing(self, patch_probe_layers: Any) -> None:
        patch_probe_layers({}, {})
        result = await probe_podman()
        assert result.available is False

    async def test_seatbelt_missing(self, patch_probe_layers: Any) -> None:
        patch_probe_layers({}, {})
        result = await probe_seatbelt()
        assert result.available is False

    async def test_version_check_fails(self, patch_probe_layers: Any) -> None:
        patch_probe_layers(
            {"bwrap": "/usr/bin/bwrap"},
            {"bwrap": (1, "bwrap: error")},
        )
        result = await probe_bwrap()
        assert result.available is False
        assert "error" in result.detail or result.detail != ""

    async def test_version_check_timeout_reports_unavailable(self, patch_probe_layers: Any) -> None:
        """A hung CLI is an honest 'unavailable' fact — probes never raise."""
        patch_probe_layers(
            {"docker": "/usr/bin/docker"},
            ExplodingVersionRunner(
                subprocess.TimeoutExpired(cmd=["docker", "--version"], timeout=10)
            ),
        )
        result = await probe_docker()
        assert result.available is False
        assert "timed out" in result.detail

    async def test_version_check_oserror_reports_unavailable(self, patch_probe_layers: Any) -> None:
        """A removed CLI is an unavailable engine, not a policy refusal."""
        patch_probe_layers(
            {"podman": "/usr/bin/podman"},
            ExplodingVersionRunner(FileNotFoundError("engine removed")),
        )
        result = await probe_podman()
        assert result.available is False
        assert "engine removed" in result.detail

    @pytest.mark.parametrize("smoke", [False, True])
    @pytest.mark.parametrize("error_number", [1, 13])
    async def test_launch_permission_error_cannot_downgrade(
        self, monkeypatch: pytest.MonkeyPatch, smoke: bool, error_number: int
    ) -> None:
        def launch(argv: list[str], timeout: float) -> tuple[int, str, str]:
            if not smoke or "--version" not in argv:
                raise PermissionError(error_number, "launcher denied")
            return 0, "available", ""

        monkeypatch.setattr(engine_probe, "_which", lambda name: "/usr/bin/bwrap")
        monkeypatch.setattr(engine_probe, "_exec_cli", launch)

        with pytest.raises(PermissionError):
            await probe_bwrap()


class TestBwrapSmokeCheck:
    """``probe_bwrap`` proves userns/mount setup with a real sandbox smoke."""

    async def test_smoke_success_reports_available(self, patch_probe_layers: Any) -> None:
        smoke = FakeVersionRunner({"bwrap": (0, "")})
        patch_probe_layers({"bwrap": "/usr/bin/bwrap"}, {"bwrap": (0, "bwrap 0.9.0")}, smoke)
        result = await probe_bwrap()
        assert result.available is True
        assert "0.9.0" in result.detail
        assert len(smoke.calls) == 1

    async def test_smoke_userns_denial_is_actionable(self, patch_probe_layers: Any) -> None:
        """AppArmor/userns denial reports unavailable with the sysctl hint."""
        smoke = FakeVersionRunner({"bwrap": (1, "bwrap: setting up uid map: Permission denied")})
        patch_probe_layers({"bwrap": "/usr/bin/bwrap"}, {"bwrap": (0, "bwrap 0.9.0")}, smoke)
        result = await probe_bwrap()
        assert result.available is False
        assert "setting up uid map" in result.detail
        assert "kernel.apparmor_restrict_unprivileged_userns" in result.detail

    async def test_smoke_failure_without_denial_signature_gets_no_userns_hint(
        self, patch_probe_layers: Any
    ) -> None:
        smoke = FakeVersionRunner(
            {"bwrap": (1, "bwrap: execvp /bin/true: No such file or directory")}
        )
        patch_probe_layers({"bwrap": "/usr/bin/bwrap"}, {"bwrap": (0, "bwrap 0.9.0")}, smoke)
        result = await probe_bwrap()
        assert result.available is False
        assert "execvp" in result.detail
        assert "apparmor" not in result.detail.lower()

    async def test_smoke_timeout_reports_unavailable(self, patch_probe_layers: Any) -> None:
        smoke = ExplodingVersionRunner(subprocess.TimeoutExpired(cmd=["bwrap"], timeout=10))
        patch_probe_layers({"bwrap": "/usr/bin/bwrap"}, {"bwrap": (0, "bwrap 0.9.0")}, smoke)
        result = await probe_bwrap()
        assert result.available is False
        assert "timed out" in result.detail

    async def test_smoke_oserror_reports_unavailable(self, patch_probe_layers: Any) -> None:
        smoke = ExplodingVersionRunner(OSError("spawn failed"))
        patch_probe_layers({"bwrap": "/usr/bin/bwrap"}, {"bwrap": (0, "bwrap 0.9.0")}, smoke)
        result = await probe_bwrap()
        assert result.available is False
        assert "spawn failed" in result.detail

    async def test_smoke_argv_exercises_mount_primitives(self, patch_probe_layers: Any) -> None:
        """The smoke is a real minimal sandbox, not a re-run of --version."""
        smoke = FakeVersionRunner({"bwrap": (0, "")})
        patch_probe_layers({"bwrap": "/usr/bin/bwrap"}, {"bwrap": (0, "bwrap 0.9.0")}, smoke)
        await probe_bwrap()
        assert len(smoke.calls) == 1
        argv = smoke.calls[0]
        assert argv[0] == "bwrap"
        assert "--version" not in argv
        for flag in ("--ro-bind", "--tmpfs", "--dev", "--proc"):
            assert flag in argv

    async def test_version_failure_skips_smoke(self, patch_probe_layers: Any) -> None:
        smoke = FakeVersionRunner({"bwrap": (0, "")})
        runner = patch_probe_layers(
            {"bwrap": "/usr/bin/bwrap"}, {"bwrap": (1, "bwrap: error")}, smoke
        )
        result = await probe_bwrap()
        assert result.available is False
        assert runner.calls == [["bwrap", "--version"]]
        assert smoke.calls == []


class TestProbeCaching:
    """Results are cached per process — the second probe spawns no subprocess."""

    async def test_second_call_uses_cache(self, patch_probe_layers: Any) -> None:
        runner = patch_probe_layers(
            {"bwrap": "/usr/bin/bwrap"},
            {"bwrap": (0, "bwrap 0.10.0")},
        )
        first = await probe_bwrap()
        second = await probe_bwrap()
        assert first is second
        assert len(runner.calls) == 1

    async def test_bwrap_smoke_runs_once_then_caches(self, patch_probe_layers: Any) -> None:
        smoke = FakeVersionRunner({"bwrap": (0, "")})
        runner = patch_probe_layers(
            {"bwrap": "/usr/bin/bwrap"}, {"bwrap": (0, "bwrap 0.9.0")}, smoke
        )
        first = await probe_bwrap()
        second = await probe_bwrap()
        assert first is second
        assert len(runner.calls) == 1
        assert len(smoke.calls) == 1

    async def test_clear_cache_reprobes(self, patch_probe_layers: Any) -> None:
        runner = patch_probe_layers(
            {"docker": "/usr/bin/docker"},
            {"docker": (0, "Docker version 29.1.2")},
        )
        await probe_docker()
        clear_probe_cache()
        await probe_docker()
        assert len(runner.calls) == 2


class TestProbeResultModel:
    """ProbeResult is a frozen pydantic value."""

    async def test_frozen(self, patch_probe_layers: Any) -> None:
        patch_probe_layers(
            {"bwrap": "/usr/bin/bwrap"},
            {"bwrap": (0, "bwrap 0.10.0")},
        )
        result = await probe_bwrap()
        with pytest.raises(ValidationError):
            result.available = False  # type: ignore[misc]

    async def test_negative_result_is_cached_too(self, patch_probe_layers: Any) -> None:
        runner = patch_probe_layers({}, {})
        first = await probe_podman()
        second = await probe_podman()
        assert first.available is False
        assert first is second
        assert runner.calls == []
