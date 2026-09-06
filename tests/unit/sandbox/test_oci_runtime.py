"""Tests for OciContainerRuntime — the oci-family container runtime (Ticket 07).

Covers (docs/design/sandbox-integration/tickets.md §07):

- create-argv compilation snapshots: policy × network × protected subpaths ×
  writable_roots × engine name (docker / podman)
- configHash: input stability (same config → same hash) and sensitivity
  (policy/image/network changes → different hash)
- container lifecycle via mocked ``docker inspect``: missing → create;
  hash match + running → reuse (no run/rm/start); hash match + stopped →
  start; mismatch + hot → honest error demanding recreate; mismatch + cold
  → rm -f + rebuild; inspect parse failure / CLI timeout → honest degrade
- per-name serialization: two concurrent resolves create exactly one container
- mount-consistency probe: file invisible in container → HOST degrade with
  reason; host-side probe file cleaned up
- resolve() result shape: shell argv (``exec -it -w <ws> <ctr> bash``),
  one-shot prefix, mount table, enforcement=FULL
- Windows drive normalization (``windows_host_to_engine``)

All docker/podman CLI traffic goes through the ``oci_runtime._run_cli`` seam —
these tests run on every platform and never touch a real engine.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modex_agent.sandbox import oci_runtime
from modex_agent.sandbox.exceptions import SandboxConfigurationError, SandboxPermissionError
from modex_agent.sandbox.oci_runtime import (
    OciContainerRuntime,
    windows_host_to_engine,
)
from modex_agent.sandbox.oci_support import CliResult
from modex_agent.sandbox.runtime import ResolvedSandbox
from modex_agent.sandbox.selection import OciEngine
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings, WriteSurface
from modex_agent.sandbox.types import EnforcementLevel

_WS = Path("/ws/project")
_IMAGE = "debian:bookworm-slim"

_WIN_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _ws_src(workspace: Path) -> str:
    """Expected mount source, spelled independently of the product code:
    POSIX verbatim, Windows drive → ``//drive/rest``."""
    raw = str(workspace)
    match = _WIN_DRIVE.match(raw)
    if match is None:
        return raw
    drive, rest = match.groups()
    return f"//{drive.lower()}/{rest.replace(chr(92), '/')}"


def _ws_flag(workspace: Path, suffix: str = "", mode: str = "rw") -> str:
    """Expected ``-v`` flag with source/target spelled independently
    (identical on POSIX; drive-shifted target on a Windows host)."""
    src = _ws_src(workspace)
    target = src if _WIN_DRIVE.match(str(workspace)) is None else "/" + src.lstrip("/")
    return f"{src}{suffix}:{target}{suffix}:{mode}"


# ---------------------------------------------------------------------------
# Fake CLI — routes canned responses by argv shape, records every call
# ---------------------------------------------------------------------------


class FakeCli:
    """Stand-in for ``oci_runtime._run_cli`` (module seam)."""

    def __init__(self, engine: str = "docker") -> None:
        self.engine = engine
        self.calls: list[list[str]] = []
        # inspect: None → "No such object" (rc 1); str → stdout payload
        self.inspect_stdout: str | None = None
        self.inspect_error: str | None = None
        self.exec_results: dict[tuple[str, ...], CliResult] = {}
        self.exec_default: CliResult = CliResult(returncode=0)
        self.run_result: CliResult = CliResult(returncode=0)
        self.slow_run_seconds: float = 0.0

    async def __call__(self, argv: list[str], timeout: float = 30.0) -> CliResult:
        self.calls.append(list(argv))
        if argv[0] != self.engine:
            return CliResult(returncode=127, stderr=f"unknown engine {argv[0]}")
        sub = argv[1]
        if sub == "inspect":
            if self.inspect_error is not None:
                return CliResult(returncode=None, error=self.inspect_error)
            if self.inspect_stdout is None:
                return CliResult(returncode=1, stderr="Error: No such object: ...")
            return CliResult(returncode=0, stdout=self.inspect_stdout)
        if sub == "exec":
            return self.exec_results.get(tuple(argv[2:]), self.exec_default)
        if sub == "run":
            if self.slow_run_seconds:
                await asyncio.sleep(self.slow_run_seconds)
            return self.run_result
        return CliResult(returncode=0)  # start / rm

    def calls_with(self, sub: str) -> list[list[str]]:
        return [c for c in self.calls if len(c) > 1 and c[1] == sub]


def _inspect_payload(
    running: bool = True,
    started_at_epoch: float | None = None,
    config_hash: str = "0" * 16,
) -> str:
    """A docker-inspect JSON array shaped like the runtime expects (ns precision)."""
    epoch = time.time() if started_at_epoch is None else started_at_epoch
    iso = (
        datetime.fromtimestamp(epoch, tz=UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", ".123456789Z")
    )
    labels = {"modex.sandbox.configHash": config_hash} if config_hash else {}
    return json.dumps(
        [
            {
                "State": {"Running": running, "StartedAt": iso},
                "Config": {"Labels": labels},
            }
        ]
    )


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeCli:
    fake = FakeCli()
    monkeypatch.setattr(oci_runtime, "_run_cli", fake)
    return fake


def _settings(
    write_surface: WriteSurface = WriteSurface.WORKSPACE,
    network: bool = False,
    image: str = _IMAGE,
    protected: list[str] | None = None,
    writable_roots: list[Path] | None = None,
) -> SandboxSettings:
    exclusive: dict[str, object] = {"write_surface": write_surface.value}
    if protected is not None:
        exclusive["protected_subpaths"] = protected
    if writable_roots is not None:
        exclusive["writable_roots"] = writable_roots
    return SandboxSettings.model_validate(
        {
            "backend": SandboxBackend.OCI,
            "network": network,
            "image": image,
            "exclusive": exclusive,
        }
    )


async def _resolve(
    settings: SandboxSettings,
    workspace: Path,
    engine: OciEngine = OciEngine.DOCKER,
) -> ResolvedSandbox:
    return await OciContainerRuntime(engine=engine).resolve(settings, workspace)


def _run_call(cli: FakeCli) -> list[str]:
    runs = cli.calls_with("run")
    assert len(runs) == 1, f"expected exactly one run, got {len(runs)}"
    return runs[0]


# ---------------------------------------------------------------------------
# Engine constructor validation
# ---------------------------------------------------------------------------


class TestEngineValidation:
    def test_unknown_engine_literal_is_unrepresentable(self) -> None:
        """The engine is the typed OciEngine enum — an unknown literal
        cannot be constructed at all."""
        from modex_agent.sandbox.selection import OciEngine

        with pytest.raises(ValueError):
            OciEngine("nerdctl")

    def test_accepts_both_engines(self) -> None:
        OciContainerRuntime(engine=OciEngine.DOCKER)
        OciContainerRuntime(engine=OciEngine.PODMAN)

    def test_engine_property_is_typed(self) -> None:
        runtime = OciContainerRuntime(engine=OciEngine.PODMAN)
        assert runtime.engine is OciEngine.PODMAN


# ---------------------------------------------------------------------------
# Windows drive normalization
# ---------------------------------------------------------------------------


class TestWindowsDriveNormalization:
    def test_drive_to_engine_source(self) -> None:
        assert windows_host_to_engine(Path(r"F:\x\y")) == "//f/x/y"

    def test_lowercase_drive(self) -> None:
        assert windows_host_to_engine(Path(r"C:\WorkSpace")) == "//c/WorkSpace"

    def test_mount_spec_uses_transformed_source(self) -> None:
        specs = oci_runtime._sandbox_mounts(
            _settings(), Path(r"F:\ws")
        )
        ws_spec = specs[0]
        assert ws_spec.host_source == "//f/ws"
        assert ws_spec.sandbox_path == "/f/ws"

    def test_posix_paths_mount_verbatim(self) -> None:
        specs = oci_runtime._sandbox_mounts(_settings(), _WS)
        # POSIX absolute paths mount verbatim (forward slashes both sides)
        assert specs[0].host_source == "/ws/project"
        assert specs[0].sandbox_path == "/ws/project"

# ---------------------------------------------------------------------------
# Create-argv compilation snapshots
# ---------------------------------------------------------------------------


class TestCreateArgvCompilation:
    async def test_workspace_write_docker_snapshot(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        """The full hardened create argv, workspace-write defaults."""
        resolved = await _resolve(_settings(), tmp_path)
        assert resolved.enforcement is EnforcementLevel.FULL
        argv = _run_call(cli)
        name = argv[argv.index("--name") + 1]
        hash_label = argv[argv.index("--label") + 1]
        assert argv == [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--user",
            "1000:1000",
            "--network",
            "none",
            "-v",
            _ws_flag(tmp_path),
            "-v",
            _ws_flag(tmp_path, suffix="/.git", mode="ro"),
            "--label",
            hash_label,
            _IMAGE,
            "sleep",
            "infinity",
        ]
        assert name.startswith("modex-sbx-")
        assert hash_label.startswith("modex.sandbox.configHash=")

    async def test_podman_engine_name(self, cli: FakeCli, tmp_path: Path) -> None:
        cli.engine = "podman"
        await _resolve(_settings(), tmp_path, engine=OciEngine.PODMAN)
        assert _run_call(cli)[0] == "podman"

    async def test_read_only_mounts_workspace_ro_without_shadows(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        await _resolve(_settings(write_surface=WriteSurface.NONE), tmp_path)
        argv = _run_call(cli)
        volumes = [v for v in argv if v.startswith(f"{_ws_src(tmp_path)}:")]
        assert volumes == [_ws_flag(tmp_path, mode="ro")]

    async def test_network_true_uses_bridge(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        await _resolve(_settings(network=True), tmp_path)
        argv = _run_call(cli)
        assert argv[argv.index("--network") + 1] == "bridge"

    async def test_writable_roots_mounted_rw_with_shadows(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        extra = tmp_path / "cache"
        await _resolve(
            _settings(writable_roots=[Path(extra)], protected=[".git", "secrets"]),
            tmp_path,
        )
        argv = _run_call(cli)
        dash_v = [v for i, v in enumerate(argv) if i > 0 and argv[i - 1] == "-v"]
        assert dash_v == [
            _ws_flag(tmp_path),
            _ws_flag(tmp_path, suffix="/.git", mode="ro"),
            _ws_flag(tmp_path, suffix="/secrets", mode="ro"),
            _ws_flag(extra),
            _ws_flag(extra, suffix="/.git", mode="ro"),
            _ws_flag(extra, suffix="/secrets", mode="ro"),
        ]

    async def test_danger_full_access_rw_no_shadows(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        await _resolve(_settings(write_surface=WriteSurface.FULL), tmp_path)
        argv = _run_call(cli)
        volumes = [v for v in argv if v.startswith(f"{_ws_src(tmp_path)}:")]
        assert volumes == [_ws_flag(tmp_path)]

    async def test_default_image_is_modex_sandbox(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        settings = SandboxSettings(backend=SandboxBackend.OCI, image=None)
        await _resolve(settings, tmp_path)
        argv = _run_call(cli)
        assert argv[-3] == "modex-sandbox:latest"


# ---------------------------------------------------------------------------
# configHash
# ---------------------------------------------------------------------------


class TestConfigHash:
    def test_stable_for_same_inputs(self) -> None:
        mounts = oci_runtime._sandbox_mounts(_settings(), _WS)
        h1 = oci_runtime._config_hash("docker", _IMAGE, False, mounts)
        h2 = oci_runtime._config_hash("docker", _IMAGE, False, mounts)
        assert h1 == h2

    def test_sensitive_to_policy(self) -> None:
        ro = oci_runtime._sandbox_mounts(
            _settings(write_surface=WriteSurface.NONE), _WS
        )
        ww = oci_runtime._sandbox_mounts(_settings(), _WS)
        assert oci_runtime._config_hash(
            "docker", _IMAGE, False, ro
        ) != oci_runtime._config_hash("docker", _IMAGE, False, ww)

    def test_sensitive_to_image_and_network_and_engine(self) -> None:
        mounts = oci_runtime._sandbox_mounts(_settings(), _WS)
        base = oci_runtime._config_hash("docker", _IMAGE, False, mounts)
        assert base != oci_runtime._config_hash("docker", "other:1", False, mounts)
        assert base != oci_runtime._config_hash("docker", _IMAGE, True, mounts)
        assert base != oci_runtime._config_hash("podman", _IMAGE, False, mounts)

    async def test_label_matches_computed_hash(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        """Container already exists with the right hash → reuse, no run."""
        mounts = oci_runtime._sandbox_mounts(_settings(), tmp_path)
        expected = oci_runtime._config_hash("docker", _IMAGE, False, mounts)
        cli.inspect_stdout = _inspect_payload(config_hash=expected)
        resolved = await _resolve(_settings(), tmp_path)
        assert resolved.enforcement is EnforcementLevel.FULL
        assert cli.calls_with("run") == []
        assert cli.calls_with("rm") == []
        assert cli.calls_with("start") == []


# ---------------------------------------------------------------------------
# Lifecycle: hot/cold, reuse, recreate
# ---------------------------------------------------------------------------


class TestContainerLifecycle:
    async def test_missing_container_creates(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        await _resolve(_settings(), tmp_path)
        assert len(cli.calls_with("run")) == 1
        assert cli.calls_with("rm") == []

    async def test_hash_match_stopped_container_is_started(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        mounts = oci_runtime._sandbox_mounts(_settings(), tmp_path)
        expected = oci_runtime._config_hash("docker", _IMAGE, False, mounts)
        cli.inspect_stdout = _inspect_payload(running=False, config_hash=expected)
        await _resolve(_settings(), tmp_path)
        assert cli.calls_with("start"), "stopped-but-matching container must be started"
        assert cli.calls_with("run") == []

    async def test_hash_mismatch_hot_errors_demanding_recreate(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        """Running + started < 5 min ago + wrong hash → error, no rm behind the
        user's back (a hot container may be in active use)."""
        cli.inspect_stdout = _inspect_payload(
            running=True, started_at_epoch=time.time(), config_hash="f" * 16
        )
        with pytest.raises(SandboxConfigurationError, match="recreate"):
            await _resolve(_settings(), tmp_path)
        assert cli.calls_with("rm") == []
        assert cli.calls_with("run") == []

    async def test_hash_mismatch_old_running_container_is_not_killed(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        cli.inspect_stdout = _inspect_payload(
            running=True, started_at_epoch=time.time() - 3600, config_hash="f" * 16
        )
        with pytest.raises(SandboxConfigurationError, match="recreate"):
            await _resolve(_settings(), tmp_path)
        assert cli.calls_with("rm") == []
        assert cli.calls_with("run") == []

    async def test_hash_mismatch_stopped_is_cold(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        """A stopped container is cold regardless of StartedAt."""
        cli.inspect_stdout = _inspect_payload(
            running=False, started_at_epoch=time.time(), config_hash="f" * 16
        )
        await _resolve(_settings(), tmp_path)
        assert len(cli.calls_with("rm")) == 1
        assert len(cli.calls_with("run")) == 1

    async def test_inspect_parse_failure_degrades_honestly(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        cli.inspect_stdout = "<not json>"
        resolved = await _resolve(_settings(), tmp_path)
        assert resolved.backend is SandboxBackend.HOST
        assert resolved.degraded_reason is not None
        assert "inspect" in resolved.degraded_reason

    async def test_cli_timeout_degrades_honestly(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        cli.inspect_error = "docker timed out after 30.0s"
        resolved = await _resolve(_settings(), tmp_path)
        assert resolved.backend is SandboxBackend.HOST
        assert "timed out" in (resolved.degraded_reason or "")

    async def test_create_failure_degrades_with_stderr(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        cli.run_result = CliResult(returncode=125, stderr="docker: image not found")
        resolved = await _resolve(_settings(), tmp_path)
        assert resolved.backend is SandboxBackend.HOST
        assert "image not found" in (resolved.degraded_reason or "")


# ---------------------------------------------------------------------------
# Serialization per container name
# ---------------------------------------------------------------------------


class TestSerialization:
    async def test_concurrent_resolves_create_once(
        self, cli: FakeCli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two resolves racing on the same workspace → one container.

        The fake ``run`` sleeps so an unserialized second resolve would also
        see "No such object" and issue its own run. With the per-name lock,
        the second resolve re-inspects after the first finished and reuses.
        """
        original_run_result = cli.run_result
        assert original_run_result.returncode == 0
        # After the first successful run, later inspects report the matching
        # container. Determined lazily so the hash is whatever run emitted.
        created: dict[str, str | None] = {"hash": None}

        async def fake_run(argv: list[str], timeout: float = 30.0) -> CliResult:
            cli.calls.append(list(argv))
            if argv[1] == "run":
                await asyncio.sleep(0.05)
                created["hash"] = argv[argv.index("--label") + 1].split("=", 1)[1]
                return cli.run_result
            if argv[1] == "inspect":
                if created["hash"] is None:
                    return CliResult(returncode=1, stderr="no such object")
                return CliResult(
                    returncode=0,
                    stdout=_inspect_payload(
                        running=True, config_hash=created["hash"]
                    ),
                )
            if argv[1] == "exec":
                return cli.exec_default
            return CliResult(returncode=0)

        monkeypatch.setattr(oci_runtime, "_run_cli", fake_run)
        runtime = OciContainerRuntime()
        results = await asyncio.gather(
            runtime.resolve(_settings(), tmp_path),
            OciContainerRuntime().resolve(_settings(), tmp_path),
        )

        assert all(r.enforcement is EnforcementLevel.FULL for r in results)
        runs = [c for c in cli.calls if c[1] == "run"]
        assert len(runs) == 1, f"serialized resolves must create exactly one container, got {len(runs)}"


# ---------------------------------------------------------------------------
# Mount-consistency probe
# ---------------------------------------------------------------------------


class TestConsistencyProbe:
    async def test_probe_storage_unavailable_degrades_before_command(
        self, cli: FakeCli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import errno
        def no_space(*args, **kwargs):
            raise OSError(errno.ENOSPC, "probe storage full")
        monkeypatch.setattr(Path, "write_text", no_space)
        resolved = await OciContainerRuntime().resolve_available(_settings(), tmp_path)
        assert resolved.backend is SandboxBackend.HOST
        assert "probe storage full" in (resolved.degraded_reason or "")

    async def test_probe_permission_is_not_unavailability(
        self, cli: FakeCli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def denied(*args, **kwargs):
            raise PermissionError("probe denied")
        monkeypatch.setattr(Path, "write_text", denied)
        with pytest.raises(PermissionError, match="probe denied"):
            await OciContainerRuntime().resolve_available(_settings(), tmp_path)

    async def test_engine_permission_is_not_unavailability(self, cli: FakeCli, tmp_path: Path) -> None:
        cli.run_result = CliResult(returncode=125, stderr="permission denied")
        with pytest.raises(SandboxPermissionError, match="permission denied"):
            await _resolve(_settings(), tmp_path)

    async def test_invalid_engine_config_is_not_unavailability(self, cli: FakeCli, tmp_path: Path) -> None:
        cli.run_result = CliResult(returncode=125, stderr="invalid reference format")
        with pytest.raises(SandboxConfigurationError, match="invalid reference format"):
            await _resolve(_settings(), tmp_path)

    async def test_probe_failure_degrades_to_host(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        """Container created fine but the workspace file is not visible inside
        (drive not shared / path mangled) → honest HOST degrade."""
        probe_argv_seen: list[list[str]] = []

        async def failing_exec(argv: list[str], timeout: float = 30.0) -> CliResult:
            cli.calls.append(list(argv))
            if argv[1] == "inspect":
                return CliResult(returncode=1, stderr="no such object")
            if argv[1] == "exec":
                probe_argv_seen.append(argv)
                return CliResult(returncode=1, stderr="")
            return CliResult(returncode=0)

        orig = oci_runtime._run_cli
        oci_runtime._run_cli = failing_exec
        try:
            resolved = await _resolve(_settings(), tmp_path)
        finally:
            oci_runtime._run_cli = orig

        assert resolved.backend is SandboxBackend.HOST
        assert resolved.enforcement is EnforcementLevel.NONE
        assert "probe" in (resolved.degraded_reason or "")
        # the probe exec'd test -f at the same path inside the container
        assert any("test" in a for a in probe_argv_seen)
        # host-side probe file cleaned up
        leftovers = list(tmp_path.glob(".modex-sbx-probe-*"))
        assert leftovers == []

    async def test_probe_success_leaves_no_file(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        await _resolve(_settings(), tmp_path)
        assert list(tmp_path.glob(".modex-sbx-probe-*")) == []
        execs = cli.calls_with("exec")
        # visibility check + deletion-visibility check
        assert len(execs) == 2
        assert execs[0][3:5] == ["test", "-f"]
        assert execs[1][3:5] == ["test", "!"] or execs[1][3] == "test"


# ---------------------------------------------------------------------------
# resolve() result shape
# ---------------------------------------------------------------------------


class TestResolveResultShape:
    async def test_shell_argv_shape(self, cli: FakeCli, tmp_path: Path) -> None:
        resolved = await _resolve(_settings(), tmp_path)
        name = _run_call(cli)[_run_call(cli).index("--name") + 1]
        container_ws = "/" + _ws_src(tmp_path).lstrip("/")
        assert resolved.shell_argv == [
            "docker",
            "exec",
            "-it",
            "-w",
            container_ws,
            name,
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-i",
        ]

    async def test_one_shot_prefix_shape(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        resolved = await _resolve(_settings(), tmp_path)
        name = _run_call(cli)[_run_call(cli).index("--name") + 1]
        assert resolved.one_shot_command_argv_prefix == ["docker", "exec", name]

    async def test_mount_table_reflects_mounts(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        resolved = await _resolve(_settings(), tmp_path)
        table = resolved.mount_table
        assert table is not None
        src = _ws_src(tmp_path)
        container_ws = "/" + src.lstrip("/")
        assert [(m.host_path, m.sandbox_path, m.read_only) for m in table] == [
            (tmp_path, Path(container_ws), False),
            (tmp_path / ".git", Path(f"{container_ws}/.git"), True),
        ]

    async def test_backend_oci_enforcement_full(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        resolved = await _resolve(_settings(), tmp_path)
        assert resolved.backend is SandboxBackend.OCI
        assert resolved.enforcement is EnforcementLevel.FULL
        assert resolved.degraded_reason is None

    async def test_container_name_stable_across_resolves(
        self, cli: FakeCli, tmp_path: Path
    ) -> None:
        """Same workspace → same deterministic container name."""
        await _resolve(_settings(), tmp_path)
        name1 = _run_call(cli)[_run_call(cli).index("--name") + 1]
        runtime = OciContainerRuntime()
        # second resolve reuses (inspect miss → run again on fresh fake state)
        cli2 = FakeCli()
        oci_runtime._run_cli = cli2
        await runtime.resolve(_settings(), tmp_path)
        runs = cli2.calls_with("run")
        assert len(runs) == 1
        assert runs[0][runs[0].index("--name") + 1] == name1
